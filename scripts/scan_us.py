#!/usr/bin/env python3
"""
Hong Kong Stock Screener using yfinance.
Two-phase approach (same as US scanner):
  Phase 1 — Batch download 2y OHLCV for all tickers, compute SMAs/volume in bulk.
  Phase 2 — Only fetch metadata (market_cap, name, sector) for survivors (~30-50 tickers).
"""

import json
import sys
import os
import time
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_FILE = os.path.join(ROOT_DIR, "public", "data", "hk.json")
UNIVERSE_FILE = os.path.join(SCRIPT_DIR, "hk_universe.txt")

os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

# Market cap threshold: HK$1 billion
MCAP_THRESHOLD = 1_000_000_000


def calc_sma(prices, period):
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


_print_lock = threading.Lock()
_pass_count = [0]
_fail_count = [0]


def analyze_from_batch(ticker, closes, volumes):
    """
    Phase 1: Analyze a ticker using pre-downloaded close/volume arrays.
    Returns dict with technical data if all SMA/volume filters pass, else None.
    """
    if len(closes) < 200:
        return None

    last_price = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else last_price

    if last_price < 10:
        return None

    sma10 = calc_sma(closes, 10)
    sma20 = calc_sma(closes, 20)
    sma30 = calc_sma(closes, 30)
    sma50 = calc_sma(closes, 50)
    sma100 = calc_sma(closes, 100)
    sma200 = calc_sma(closes, 200)

    if not all([sma10, sma20, sma30, sma50, sma100, sma200]):
        return None

    # SMA trend alignment: 10 > 20 > 50 > 100 > 200
    if sma10 <= sma20 or sma20 <= sma50 or sma50 <= sma100 or sma100 <= sma200:
        return None
    if last_price <= sma30:
        return None

    # Volume
    avg_vol_10 = calc_sma(volumes, 10)
    avg_vol_60 = calc_sma(volumes, 60)
    avg_vol_90 = calc_sma(volumes, 90)
    daily_volume = volumes[-1]

    if not all([avg_vol_10, avg_vol_60, avg_vol_90]):
        return None

    if daily_volume < 500_000:
        return None
    if avg_vol_10 < 500_000:
        return None
    if avg_vol_60 < 500_000:
        return None
    if avg_vol_90 < 500_000:
        return None

    # Average trading value (last 20 days)
    recent_closes = closes[-20:]
    recent_volumes = volumes[-20:]
    avg_trading_value = sum(c * v for c, v in zip(recent_closes, recent_volumes)) / len(recent_closes)

    if avg_trading_value < 50_000_000:
        return None

    change = last_price - prev_close
    change_pct = (change / prev_close * 100) if prev_close != 0 else 0

    if len(closes) >= 6:
        price_5d_ago = closes[-6]
        change_5d_pct = ((last_price - price_5d_ago) / price_5d_ago * 100) if price_5d_ago != 0 else 0
    else:
        change_5d_pct = 0

    return {
        "ticker": ticker,
        "name": ticker,  # filled in Phase 2
        "price": round(last_price, 2),
        "change": round(change, 2),
        "changePercent": round(change_pct, 2),
        "change5dPercent": round(change_5d_pct, 2),
        "marketCap": 0,  # filled in Phase 2
        "volume": int(daily_volume),
        "avgVolume10d": int(avg_vol_10),
        "avgVolume60d": int(avg_vol_60),
        "avgVolume90d": int(avg_vol_90),
        "avgTradingValue": int(avg_trading_value),
        "sma10": round(sma10, 2),
        "sma20": round(sma20, 2),
        "sma30": round(sma30, 2),
        "sma50": round(sma50, 2),
        "sma100": round(sma100, 2),
        "sma200": round(sma200, 2),
        "sector": "",
        "indices": [],
    }


def fetch_metadata(ticker_code, retries=3):
    """Phase 2: Fetch market cap, name, sector for a single ticker with retry."""
    for attempt in range(retries):
        try:
            tk = yf.Ticker(ticker_code)
            market_cap = 0
            name = ticker_code
            sector = ''

            try:
                fi = tk.fast_info
                market_cap = getattr(fi, 'market_cap', 0) or 0
            except:
                pass

            try:
                info = tk.info
                name = info.get('shortName', '') or info.get('longName', ticker_code)
                sector = info.get('sector', '') or ''
            except:
                pass

            # If we got at least market_cap or name, return
            if market_cap > 0 or name != ticker_code:
                return {"market_cap": market_cap, "name": name, "sector": sector}

            # If first attempt got nothing, retry
            if attempt < retries - 1:
                time.sleep(2)
                continue

            return {"market_cap": market_cap, "name": name, "sector": sector}
        except:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                return {"market_cap": 0, "name": ticker_code, "sector": ""}

    return {"market_cap": 0, "name": ticker_code, "sector": ""}


def main():
    print("[HK Screener] Starting yfinance-based scan...", file=sys.stderr)
    start_time = time.time()

    if not os.path.exists(UNIVERSE_FILE):
        print(f"[HK Screener] ERROR: Universe file not found: {UNIVERSE_FILE}", file=sys.stderr)
        return

    with open(UNIVERSE_FILE) as f:
        tickers = [line.strip() for line in f if line.strip()]

    print(f"[HK Screener] Universe: {len(tickers)} tickers", file=sys.stderr)

    # ─── Phase 1: Batch download 2y history and compute all technicals ───
    print("[HK Screener] Phase 1: Batch downloading 2y history...", file=sys.stderr)

    chunk_size = 100
    technical_passers = []

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        chunk_num = i // chunk_size + 1
        total_chunks = (len(tickers) + chunk_size - 1) // chunk_size

        try:
            data = yf.download(chunk, period="2y", group_by="ticker", progress=False, threads=True)

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

                    result = analyze_from_batch(t, closes, volumes)
                    if result:
                        technical_passers.append(result)
                except:
                    pass

        except Exception as e:
            print(f"  [Batch Error] chunk {chunk_num}: {e}", file=sys.stderr)

        elapsed = time.time() - start_time
        print(f"  [Phase 1] Chunk {chunk_num}/{total_chunks}, {len(technical_passers)} technical passers ({elapsed:.0f}s)", file=sys.stderr)

    elapsed = time.time() - start_time
    print(f"[HK Screener] Phase 1 complete: {len(technical_passers)} pass all technical filters ({elapsed:.0f}s)", file=sys.stderr)

    # ─── Phase 2: Fetch metadata only for survivors ──────────────
    print(f"[HK Screener] Phase 2: Fetching metadata for {len(technical_passers)} stocks...", file=sys.stderr)

    passing = []

    # Use sequential with delays to avoid rate limiting (only ~30-50 tickers)
    for idx, item in enumerate(technical_passers):
        try:
            meta = fetch_metadata(item["ticker"])
            market_cap = meta["market_cap"]

            # Market cap filter — skip if 0 (likely ETF/fund) or below threshold
            if market_cap == 0:
                print(f"  [Skip] {item['ticker']} — no market cap data (likely ETF)", file=sys.stderr)
                continue
            if market_cap < MCAP_THRESHOLD:
                print(f"  [Skip] {item['ticker']} — MCap HK${market_cap/1e9:.1f}B < HK$1B", file=sys.stderr)
                continue

            item["marketCap"] = int(market_cap)
            item["name"] = meta["name"]
            item["sector"] = meta["sector"]
            passing.append(item)

            with _print_lock:
                _pass_count[0] += 1
                print(f"[Pass] {item['ticker']} ({meta['name']}) HK${item['price']:.2f} MCap={market_cap/1e9:.1f}B Sector={meta['sector']}", file=sys.stderr)
        except:
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
        print(f"[HK Screener] Phase 3: Retrying sectors for {len(no_sector)} stocks...", file=sys.stderr)
        for idx, stock in enumerate(no_sector):
            try:
                tk = yf.Ticker(stock["ticker"])
                info = tk.info
                stock["sector"] = info.get("sector", "") or ""
                if not stock["name"] or stock["name"] == stock["ticker"]:
                    stock["name"] = info.get("shortName", "") or info.get("longName", stock["ticker"])
            except:
                pass
            if (idx + 1) % 5 == 0:
                print(f"  [Phase 3] {idx+1}/{len(no_sector)} sectors retried", file=sys.stderr)
                time.sleep(2)
            else:
                time.sleep(0.8)

    # Sort by market cap descending
    passing.sort(key=lambda x: x["marketCap"], reverse=True)

    elapsed = time.time() - start_time
    print(f"\n[HK Screener] ═══════════════════════════════════════", file=sys.stderr)
    print(f"[HK Screener] Complete. {len(passing)} stocks pass all filters.", file=sys.stderr)
    print(f"[HK Screener] Total time: {elapsed:.0f}s ({elapsed/60:.1f}min)", file=sys.stderr)
    print(f"[HK Screener] Passed: {_pass_count[0]}, Failed: {_fail_count[0]}", file=sys.stderr)

    output = {
        "stocks": passing,
        "totalUniverse": len(tickers),
        "totalPassing": len(passing),
        "lastUpdated": datetime.now().isoformat(),
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f)
    print(f"[Done] Output: {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
