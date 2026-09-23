#!/usr/bin/env python3
"""
US Stock Screener — v2 (rate-limit resilient)

Pipeline:
  Phase 0 — Nasdaq screener API (1 HTTP call) returns ALL US stocks with
            market cap / price / volume / name / sector.
            Pre-filter: MCap >= $3B, Price > $10  →  ~7000 tickers reduced to ~600.
            This removes the need to fetch per-ticker metadata from Yahoo entirely.
  Phase 1 — yf.download 1y history ONLY for pre-filtered tickers,
            small chunks + pauses + exponential backoff on rate limit.
            Compute SMA trend / volume / trading value filters.
  Guard   — If the scan produced 0 results because of rate limiting,
            KEEP the previous data file and exit(1) so the workflow shows failure.

Filters:
  MCap > $3B, Price > $10, Vol > 500K, AvgVol 10/60/90d > 500K,
  AvgValue(20d) > $50M, SMA 10>20>50>100>200, Price > SMA30
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "yfinance", "-q"])
    import yfinance as yf

import pandas as pd

from enrich import compute_extras, finalize_lists, utc_now_iso, patch_last_bar
from momentum import (rs_returns, rs_ratings, analyze_momentum, select_momentum,
                      finalize_momentum)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_FILE = os.path.join(ROOT_DIR, "public", "data", "us.json")
UNIVERSE_FILE = os.path.join(SCRIPT_DIR, "us_universe.txt")

os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

MCAP_THRESHOLD = 3_000_000_000
PRICE_THRESHOLD = 10
VOL_THRESHOLD = 500_000
VALUE_THRESHOLD = 50_000_000

CHUNK_SIZE = 50          # small chunks like the breadth script (proven on Actions)
CHUNK_PAUSE = 2.0        # seconds between chunks
MAX_CHUNK_RETRIES = 4    # per-chunk retries with backoff


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ────────────────────────── Phase 0: universe + fundamentals ──────────────────────────

def fetch_nasdaq_screener():
    """One call to Nasdaq's screener API → every US-listed stock with
    marketCap / lastsale / volume / name / sector. Returns list of dicts or None."""
    url = ("https://api.nasdaq.com/api/screener/stocks"
           "?tableonly=true&limit=25&offset=0&download=true")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            rows = data["data"]["rows"]
            if rows and len(rows) > 1000:
                return rows
        except Exception as e:
            log(f"[Phase 0] Nasdaq API attempt {attempt+1} failed: {e}")
            time.sleep(10)
    return None


def build_prefiltered_universe():
    """Returns (candidates dict ticker→meta, total_universe_count, used_nasdaq)."""
    rows = fetch_nasdaq_screener()
    if rows is None:
        log("[Phase 0] Nasdaq API unavailable — falling back to static universe file")
        with open(UNIVERSE_FILE) as f:
            tickers = [line.strip() for line in f if line.strip()]
        return {t: None for t in tickers}, len(tickers), False

    total = len(rows)
    candidates = {}
    for r in rows:
        try:
            sym = r["symbol"].strip()
            # skip preferred/units (^, spaces); convert BRK/B → BRK-B for Yahoo
            if "^" in sym or " " in sym:
                continue
            sym = sym.replace("/", "-").replace(".", "-")
            mcap = float(r.get("marketCap") or 0)
            price = float((r.get("lastsale") or "$0").replace("$", "").replace(",", ""))
            if mcap < MCAP_THRESHOLD or price <= PRICE_THRESHOLD:
                continue
            candidates[sym] = {
                "name": (r.get("name") or sym)
                    .replace(" Common Stock", "").replace(" Class A", "")
                    .replace(" Ordinary Shares", "").replace(" Inc.", " Inc")
                    .strip(),
                "sector": r.get("sector") or "",
                "marketCap": int(mcap),
            }
        except Exception:
            continue
    log(f"[Phase 0] Nasdaq universe {total} → {len(candidates)} after MCap>${MCAP_THRESHOLD/1e9:.0f}B & Price>${PRICE_THRESHOLD}")
    return candidates, total, True


# ────────────────────────── Phase 1: history + technical filters ──────────────────────────

def calc_sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def is_partial_bar(last_index, tz="America/New_York", close_hm=(16, 5)):
    """True if the last daily bar is today's session and the market has not
    closed yet (intraday scan), i.e. its volume is incomplete."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        now = datetime.now(ZoneInfo(tz))
        bar_date = last_index.date() if hasattr(last_index, "date") else last_index
        return bar_date == now.date() and (now.hour, now.minute) < close_hm
    except Exception:
        return False


def analyze(ticker, closes, volumes, partial_last_bar=False):
    """`partial_last_bar=True` means the final bar is today's still-open session
    (intraday scan). Price / SMA use it (that is the point of scanning after the
    open), but every volume-based test uses only completed sessions so a stock
    is not rejected for having traded "too little" in the first minutes."""
    if len(closes) < 200:
        return None

    last_price = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else last_price
    if last_price <= PRICE_THRESHOLD:
        return None

    sma10 = calc_sma(closes, 10)
    sma20 = calc_sma(closes, 20)
    sma30 = calc_sma(closes, 30)
    sma50 = calc_sma(closes, 50)
    sma100 = calc_sma(closes, 100)
    sma200 = calc_sma(closes, 200)
    if not all([sma10, sma20, sma30, sma50, sma100, sma200]):
        return None

    # Relaxed alignment: 10 > 20 > 50, 100 > 200, and 50 > 200.
    # The 50 > 100 leg is recorded as `strict` so the frontend can toggle it.
    if not (sma10 > sma20 > sma50 and sma100 > sma200 and sma50 > sma200):
        return None
    strict = sma50 > sma100
    if last_price <= sma30:
        return None

    # Volume tests on completed sessions only.
    vol_c = volumes[:-1] if partial_last_bar else volumes
    close_c = closes[:-1] if partial_last_bar else closes
    if len(vol_c) < 90:
        return None
    daily_volume = vol_c[-1]
    avg_vol_10 = calc_sma(vol_c, 10)
    avg_vol_60 = calc_sma(vol_c, 60)
    avg_vol_90 = calc_sma(vol_c, 90)
    if not all([avg_vol_10, avg_vol_60, avg_vol_90]):
        return None
    if (daily_volume < VOL_THRESHOLD or avg_vol_10 < VOL_THRESHOLD
            or avg_vol_60 < VOL_THRESHOLD or avg_vol_90 < VOL_THRESHOLD):
        return None

    recent_c = close_c[-20:]
    recent_v = vol_c[-20:]
    avg_value = sum(c * v for c, v in zip(recent_c, recent_v)) / len(recent_c)
    if avg_value < VALUE_THRESHOLD:
        return None

    change = last_price - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0
    change_5d_pct = 0
    if len(closes) >= 6 and closes[-6]:
        change_5d_pct = (last_price - closes[-6]) / closes[-6] * 100

    out = {
        "ticker": ticker,
        "name": ticker,
        "price": round(last_price, 2),
        "change": round(change, 2),
        "changePercent": round(change_pct, 2),
        "change5dPercent": round(change_5d_pct, 2),
        "marketCap": 0,
        "volume": int(daily_volume),
        "avgVolume10d": int(avg_vol_10),
        "avgVolume60d": int(avg_vol_60),
        "avgVolume90d": int(avg_vol_90),
        "avgTradingValue": int(avg_value),
        "sma10": round(sma10, 2),
        "sma20": round(sma20, 2),
        "sma30": round(sma30, 2),
        "sma50": round(sma50, 2),
        "sma100": round(sma100, 2),
        "sma200": round(sma200, 2),
        "strict": strict,
        "sector": "",
        "indices": [],
    }
    out.update(compute_extras(closes))
    return out


def download_chunk(chunk):
    """yf.download with exponential backoff. Returns DataFrame or None."""
    delay = 30
    for attempt in range(MAX_CHUNK_RETRIES):
        try:
            data = yf.download(chunk, period="1y", group_by="ticker",
                               progress=False, threads=False, auto_adjust=True)
            if data is not None and not data.empty:
                return data
            raise RuntimeError("empty dataframe")
        except Exception as e:
            msg = str(e)
            is_rate = "Rate" in msg or "429" in msg or "Too Many" in msg or "empty" in msg
            if attempt < MAX_CHUNK_RETRIES - 1:
                wait = delay * (2 ** attempt) if is_rate else 10
                log(f"    retry {attempt+1} in {wait}s ({msg[:80]})")
                time.sleep(wait)
            else:
                log(f"    chunk failed permanently: {msg[:120]}")
    return None


def main():
    start = time.time()
    log("[US Screener v2] Starting...")

    candidates, total_universe, used_nasdaq = build_prefiltered_universe()
    tickers = list(candidates.keys())

    log(f"[Phase 1] Downloading 1y history for {len(tickers)} tickers "
        f"(chunks of {CHUNK_SIZE})...")

    passing = []
    rs_rets = {}          # ticker -> {ret21, ret63}, for EVERY ticker with enough history
    mom_candidates = []   # momentum screen passers (before the RS Rating test)
    failed_chunks = 0
    total_chunks = (len(tickers) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]
        chunk_num = i // CHUNK_SIZE + 1

        data = download_chunk(chunk)
        if data is None:
            failed_chunks += 1
            continue
        # Yahoo's daily row for the latest session is NaN for hours after the
        # close; repair it from hourly bars + official close so the scan never
        # silently describes the previous day.
        data = patch_last_bar(data, chunk, "America/New_York", log=log)

        for t in chunk:
            try:
                if len(chunk) == 1:
                    tdf = data
                else:
                    if t not in data.columns.get_level_values(0):
                        continue
                    tdf = data[t]
                tdf = tdf.dropna(subset=["Close", "Volume"])
                if len(tdf) < 200:
                    continue
                closes_l = tdf["Close"].tolist()
                volumes_l = tdf["Volume"].tolist()
                partial = is_partial_bar(tdf.index[-1])

                # Momentum tab: universe-wide RS score + non-RS criteria.
                rr = rs_returns(closes_l)
                if rr is not None:
                    rs_rets[t] = rr
                try:
                    highs_l = tdf["High"].fillna(tdf["Close"]).tolist()
                    lows_l = tdf["Low"].fillna(tdf["Close"]).tolist()
                    m = analyze_momentum(t, closes_l, highs_l, lows_l, volumes_l,
                                         PRICE_THRESHOLD, partial_last_bar=partial)
                    if m:
                        meta = candidates.get(t)
                        if meta:
                            m["name"] = meta["name"]
                            m["sector"] = meta["sector"]
                            m["marketCap"] = meta["marketCap"]
                        mom_candidates.append(m)
                except Exception:
                    pass

                result = analyze(t, closes_l, volumes_l, partial_last_bar=partial)
                if result:
                    meta = candidates.get(t)
                    if meta:
                        result["name"] = meta["name"]
                        result["sector"] = meta["sector"]
                        result["marketCap"] = meta["marketCap"]
                    passing.append(result)
            except Exception:
                pass

        if chunk_num % 3 == 0 or chunk_num == total_chunks:
            log(f"  [Phase 1] chunk {chunk_num}/{total_chunks}, "
                f"{len(passing)} passing, {failed_chunks} failed chunks "
                f"({time.time()-start:.0f}s)")
        time.sleep(CHUNK_PAUSE)

    # ── Fallback metadata fetch (only if Nasdaq API was unavailable) ──
    if not used_nasdaq and passing:
        log(f"[Phase 2] Fetching metadata for {len(passing)} survivors via yfinance...")
        kept = []
        for idx, item in enumerate(passing):
            try:
                tk = yf.Ticker(item["ticker"])
                mcap = 0
                try:
                    mcap = getattr(tk.fast_info, "market_cap", 0) or 0
                except Exception:
                    pass
                if mcap < MCAP_THRESHOLD:
                    continue
                item["marketCap"] = int(mcap)
                try:
                    info = tk.info
                    item["name"] = info.get("shortName") or info.get("longName") or item["ticker"]
                    item["sector"] = info.get("sector") or ""
                except Exception:
                    pass
                kept.append(item)
            except Exception:
                pass
            time.sleep(1.0 if (idx + 1) % 5 == 0 else 0.3)
        passing = kept

    passing.sort(key=lambda x: x["marketCap"], reverse=True)
    history_file = os.path.join(ROOT_DIR, "public", "data", "us_history.json")
    n_strict = finalize_lists(passing, history_file, "America/New_York")
    log(f"[Lists] strict (50>100 required): {n_strict}, relaxed total: {len(passing)}")

    # ── Momentum tab: RS Rating percentile across the whole scanned universe ──
    ratings = rs_ratings(rs_rets)
    momentum = select_momentum(mom_candidates, ratings)
    finalize_momentum(momentum, history_file, "America/New_York")
    log(f"[Momentum] {len(mom_candidates)} pass ADR/EMA, {len(momentum)} with RS > 90 on 1M/3M/1M+3M "
        f"(universe ranked: {len(rs_rets)})")
    elapsed = time.time() - start

    log(f"[US Screener v2] Complete: {len(passing)} stocks pass "
        f"({failed_chunks}/{total_chunks} chunks failed, {elapsed:.0f}s)")

    # ── Empty-result guard: never overwrite good data with a rate-limited empty scan ──
    if len(passing) == 0 and (failed_chunks > total_chunks * 0.3):
        log("[GUARD] 0 results AND >30% chunks failed → keeping previous data, exiting 1")
        sys.exit(1)

    output = {
        "stocks": passing,
        "totalUniverse": total_universe,
        "totalPassing": len(passing),
        "momentum": momentum,
        "momentumUniverse": len(rs_rets),
        "lastUpdated": utc_now_iso(),
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f)
    log(f"[Done] {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
