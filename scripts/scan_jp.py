#!/usr/bin/env python3
"""
Japan (TSE Prime) Stock Screener using yfinance.
Two-phase approach (same as US/HK scanners):
  Phase 1 — Batch download 1y OHLCV for all tickers, compute SMAs/volume in bulk.
  Phase 2 — Only fetch metadata (market_cap, name, sector) for survivors.

Universe: every "Prime Market (Domestic)" issue in JPX's monthly
"List of TSE-listed Issues" (data_e.xlsx). Downloaded at run time and cached
to scripts/jp_universe.txt (used as the fallback if JPX is unreachable).
Yahoo tickers are "<code>.T"; TradingView symbols are "TSE:<code>".
"""

import json
import sys
import os
import time
from collections import Counter
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "yfinance", "-q"])
    import yfinance as yf

try:
    import pandas as pd
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "pandas", "-q"])
    import pandas as pd

from enrich import finalize_lists, utc_now_iso
from scan_common import (calc_sma, is_partial_bar, analyze_from_batch,
                         fetch_metadata, download_chunk_with_retry)
from momentum import (rs_returns, rs_ratings, analyze_momentum, select_momentum,
                      finalize_momentum)

MOM_PRICE_THRESHOLD = 100.0   # JPY 100 for the momentum tab

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_FILE = os.path.join(ROOT_DIR, "public", "data", "jp.json")
UNIVERSE_FILE = os.path.join(SCRIPT_DIR, "jp_universe.txt")
JPX_LIST_URL = ("https://www.jpx.co.jp/english/markets/statistics-equities/misc/"
                "tvdivq0000001vg2-att/data_e.xlsx")
JPX_SECTOR = {}   # ticker -> JPX 33-sector name (fallback when Yahoo has none)

os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

# Market cap threshold: JPY 100 billion
MCAP_THRESHOLD = 100_000_000_000
PRICE_THRESHOLD = 100.0          # JPY
MIN_VOLUME = 100_000             # shares/day (TSE trades in 100-share units)
MIN_TRADING_VALUE = 1_000_000_000  # JPY 1B average daily value (20d)


_print_lock = threading.Lock()
_pass_count = [0]
_fail_count = [0]


META_CACHE_FILE = os.path.join(SCRIPT_DIR, "jp_meta.json")
try:
    with open(META_CACHE_FILE) as _f:
        META_CACHE = json.load(_f)   # ticker -> {"name", "sector"} (built offline; Yahoo's
except Exception:                     # quoteSummary/.info is blocked from GitHub runners)
    META_CACHE = {}


def load_universe():
    """TSE Prime (domestic) tickers from JPX's monthly list; cache to disk.
    Falls back to the cached file when the download/parse fails."""
    try:
        import io
        from curl_cffi import requests as _rq
        r = _rq.get(JPX_LIST_URL, impersonate="chrome", timeout=30)
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content), dtype=str)
        sec_col = next(c for c in df.columns if c.startswith("Section"))
        code_col = next(c for c in df.columns if "Code" in c and "Local" in c)
        sect_col = next((c for c in df.columns if "33 Sector(name)" in c), None)
        prime = df[df[sec_col].str.contains("Prime Market", na=False)
                   & ~df[sec_col].str.contains("Foreign", na=False)]
        tickers = []
        for _, row in prime.iterrows():
            code = str(row[code_col]).strip()
            if not code or code == "nan":
                continue
            t = f"{code}.T"
            tickers.append(t)
            if sect_col:
                JPX_SECTOR[t] = str(row[sect_col]).strip()
        if len(tickers) < 1000:
            raise RuntimeError(f"only {len(tickers)} Prime tickers parsed")
        with open(UNIVERSE_FILE, "w") as f:
            f.write("\n".join(tickers) + "\n")
        print(f"[JP Screener] Universe from JPX: {len(tickers)} Prime tickers", file=sys.stderr)
        return tickers
    except Exception as e:
        print(f"[JP Screener] JPX list unavailable ({str(e)[:80]}); using cached universe", file=sys.stderr)
        if not os.path.exists(UNIVERSE_FILE):
            return []
        with open(UNIVERSE_FILE) as f:
            return [line.strip() for line in f if line.strip()]


def main():
    print("[JP Screener] Starting yfinance-based scan...", file=sys.stderr)
    start_time = time.time()

    tickers = load_universe()
    if not tickers:
        print("[JP Screener] ERROR: empty universe", file=sys.stderr)
        sys.exit(1)

    print(f"[JP Screener] Universe: {len(tickers)} tickers", file=sys.stderr)

    # ─── Phase 1: Batch download 2y history and compute all technicals ───
    print("[JP Screener] Phase 1: Batch downloading 2y history...", file=sys.stderr)

    chunk_size = 50
    technical_passers = []
    rs_rets = {}          # ticker -> {ret21, ret63}, for EVERY ticker with enough history
    mom_candidates = []   # momentum tab passers (before the RS Rating test)
    failed_chunks = 0
    last_dates = Counter()  # last completed bar date per ticker -> trading session
    ticker_errors = 0        # per-ticker analysis failures (was silently swallowed)
    total_chunks = (len(tickers) + chunk_size - 1) // chunk_size

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        chunk_num = i // chunk_size + 1

        data = download_chunk_with_retry(chunk, "Asia/Tokyo")
        if data is None:
            failed_chunks += 1
            continue

        try:
            for t in chunk:
                try:
                    if len(chunk) == 1:
                        ticker_data = data
                    else:
                        if t not in data.columns.get_level_values(0):
                            continue
                        ticker_data = data[t]

                    ticker_data = ticker_data.dropna(subset=["Close", "Volume"])
                    if len(ticker_data) < 200:
                        continue

                    closes = ticker_data["Close"].tolist()
                    volumes = ticker_data["Volume"].tolist()
                    partial = is_partial_bar(ticker_data.index[-1], "Asia/Tokyo", (15, 35))
                    last_dates[ticker_data.index[-1].date()] += 1

                    # Momentum tab: universe-wide RS score + non-RS criteria.
                    rr = rs_returns(closes)
                    if rr is not None:
                        rs_rets[t] = rr
                    try:
                        highs = ticker_data["High"].fillna(ticker_data["Close"]).tolist()
                        lows = ticker_data["Low"].fillna(ticker_data["Close"]).tolist()
                        m = analyze_momentum(t, closes, highs, lows, volumes,
                                             MOM_PRICE_THRESHOLD, partial_last_bar=partial)
                        if m:
                            mom_candidates.append(m)
                    except Exception:
                        pass

                    result = analyze_from_batch(t, closes, volumes, PRICE_THRESHOLD, MIN_VOLUME,
                                               MIN_TRADING_VALUE, partial_last_bar=partial)
                    if result:
                        technical_passers.append(result)
                except Exception as e:
                    ticker_errors += 1
                    if ticker_errors <= 5:
                        print(f"    [ticker] {t}: {type(e).__name__}: {str(e)[:120]}",
                              file=sys.stderr)
        except Exception as e:
            print(f"  [Batch Error] chunk {chunk_num}: {e}", file=sys.stderr)

        elapsed = time.time() - start_time
        print(f"  [Phase 1] Chunk {chunk_num}/{total_chunks}, {len(technical_passers)} technical passers ({elapsed:.0f}s)", file=sys.stderr)
        time.sleep(2.0)

    elapsed = time.time() - start_time
    print(f"[JP Screener] Phase 1 complete: {len(technical_passers)} pass all technical filters ({elapsed:.0f}s, {ticker_errors} ticker errors)", file=sys.stderr)

    # ─── Phase 2: Fetch metadata only for survivors ──────────────
    print(f"[JP Screener] Phase 2: Fetching metadata for {len(technical_passers)} stocks...", file=sys.stderr)

    passing = []
    meta_cache = {}

    # Use sequential with delays to avoid rate limiting (only ~30-50 tickers)
    for idx, item in enumerate(technical_passers):
        try:
            meta = fetch_metadata(item["ticker"], META_CACHE)
            meta_cache[item["ticker"]] = meta
            market_cap = meta["market_cap"]

            # Market cap filter — skip if 0 (likely ETF/fund) or below threshold.
            # BUT: if metadata lookups are failing across the board (rate limit),
            # keep the stock rather than silently dropping everything.
            if market_cap == 0:
                if meta["name"] != item["ticker"]:
                    print(f"  [Skip] {item['ticker']} — no market cap data (likely ETF)", file=sys.stderr)
                    continue
                print(f"  [Keep] {item['ticker']} — metadata unavailable (rate limit?), keeping with MCap=0", file=sys.stderr)
                item["name"] = meta["name"]
                item["sector"] = meta["sector"] or JPX_SECTOR.get(item["ticker"], "")
                passing.append(item)
                continue
            if market_cap < MCAP_THRESHOLD:
                print(f"  [Skip] {item['ticker']} — MCap ¥{market_cap/1e9:.0f}B < ¥100B", file=sys.stderr)
                continue

            item["marketCap"] = int(market_cap)
            item["name"] = meta["name"]
            item["sector"] = meta["sector"] or JPX_SECTOR.get(item["ticker"], "")
            passing.append(item)

            with _print_lock:
                _pass_count[0] += 1
                print(f"[Pass] {item['ticker']} ({meta['name']}) ¥{item['price']:.0f} MCap=¥{market_cap/1e9:.0f}B Sector={item['sector']}", file=sys.stderr)
        except Exception:
            _fail_count[0] += 1

        # Small delay between metadata calls to avoid rate limits
        if (idx + 1) % 5 == 0:
            time.sleep(1.5)
        else:
            time.sleep(0.3)

        if (idx + 1) % 10 == 0:
            elapsed = time.time() - start_time
            print(f"  [Phase 2] {idx+1}/{len(technical_passers)} metadata fetched ({elapsed:.0f}s)", file=sys.stderr)

    # ─── Phase 3: Retry missing sectors ──────────────
    no_sector = [s for s in passing if not s.get("sector")]
    if no_sector:
        print(f"[JP Screener] Phase 3: Retrying sectors for {len(no_sector)} stocks...", file=sys.stderr)
        for idx, stock in enumerate(no_sector):
            try:
                tk = yf.Ticker(stock["ticker"])
                info = tk.info
                stock["sector"] = info.get("sector", "") or (META_CACHE.get(stock["ticker"]) or {}).get("sector", "") or JPX_SECTOR.get(stock["ticker"], "")
                if not stock["name"] or stock["name"] == stock["ticker"]:
                    stock["name"] = info.get("shortName", "") or info.get("longName", stock["ticker"])
            except Exception:
                pass
            if (idx + 1) % 5 == 0:
                print(f"  [Phase 3] {idx+1}/{len(no_sector)} sectors retried", file=sys.stderr)
                time.sleep(2)
            else:
                time.sleep(0.8)

    # Sort by market cap descending
    passing.sort(key=lambda x: x["marketCap"], reverse=True)
    # Session the data describes (most common last bar) — see scan_us.py.
    session = last_dates.most_common(1)[0][0] if last_dates else None
    if session:
        print(f"[Session] as_of {session} ({len(last_dates)} distinct last-bar dates)", file=sys.stderr)
    history_file = os.path.join(ROOT_DIR, "public", "data", "jp_history.json")
    n_strict = finalize_lists(passing, history_file, "Asia/Tokyo", as_of=session)
    print(f"[Lists] strict (50>100 required): {n_strict}, relaxed total: {len(passing)}", file=sys.stderr)

    # ── Momentum tab: RS Rating percentile across the whole universe ──
    ratings = rs_ratings(rs_rets)
    momentum = select_momentum(mom_candidates, ratings)
    print(f"[Momentum] {len(mom_candidates)} pass ADR/EMA, {len(momentum)} with RS > 90 on 1M/3M/1M+3M "
          f"(universe ranked: {len(rs_rets)}); fetching metadata...", file=sys.stderr)
    kept = []
    for idx, item in enumerate(momentum):
        try:
            meta = meta_cache.get(item["ticker"])
            if meta is None:
                meta = fetch_metadata(item["ticker"], META_CACHE)
                meta_cache[item["ticker"]] = meta
                time.sleep(1.5 if (idx + 1) % 5 == 0 else 0.3)
            market_cap = meta["market_cap"]
            if market_cap == 0 and meta["name"] != item["ticker"]:
                continue   # no market cap -> likely ETF/fund
            if 0 < market_cap < MCAP_THRESHOLD:
                continue
            item["marketCap"] = int(market_cap)
            item["name"] = meta["name"]
            item["sector"] = meta["sector"] or JPX_SECTOR.get(item["ticker"], "")
            kept.append(item)
        except Exception:
            pass
    momentum = kept
    finalize_momentum(momentum, history_file, "Asia/Tokyo", as_of=session)
    print(f"[Momentum] final list: {len(momentum)}", file=sys.stderr)

    elapsed = time.time() - start_time
    print(f"\n[JP Screener] ═══════════════════════════════════════", file=sys.stderr)
    print(f"[JP Screener] Complete. {len(passing)} stocks pass all filters.", file=sys.stderr)
    print(f"[JP Screener] Total time: {elapsed:.0f}s ({elapsed/60:.1f}min)", file=sys.stderr)
    print(f"[JP Screener] Passed: {_pass_count[0]}, Failed: {_fail_count[0]}", file=sys.stderr)

    # ── Empty-result guard: never overwrite good data with a rate-limited empty scan ──
    if len(passing) == 0 and failed_chunks > total_chunks * 0.3:
        print("[GUARD] 0 results AND >30% chunks failed → keeping previous data, exiting 1", file=sys.stderr)
        sys.exit(1)

    output = {
        "stocks": passing,
        "totalUniverse": len(tickers),
        "totalPassing": len(passing),
        "momentum": momentum,
        "momentumUniverse": len(rs_rets),
        "lastUpdated": utc_now_iso(),
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f)
    print(f"[Done] Output: {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
