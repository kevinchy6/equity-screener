#!/usr/bin/env python3
"""
Hong Kong Stock Screener using yfinance.
Fetches daily OHLCV data from Yahoo Finance for HK-listed stocks.
"""

import json
import sys
import os
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "yfinance", "-q"])
    import yfinance as yf

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
_skip_count = [0]


def process_ticker(ticker_code):
    """Process a single HK ticker."""
    try:
        tk = yf.Ticker(ticker_code)

        # Get 1.5 years of daily data
        hist = tk.history(period="2y")
        if hist is None or len(hist) < 200:
            with _print_lock:
                _skip_count[0] += 1
            return None

        closes = hist["Close"].tolist()
        volumes = hist["Volume"].tolist()

        if len(closes) < 200:
            with _print_lock:
                _skip_count[0] += 1
            return None

        last_price = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else last_price

        # Price filter
        if last_price < 10:
            return None

        # SMA calculations
        sma10 = calc_sma(closes, 10)
        sma20 = calc_sma(closes, 20)
        sma30 = calc_sma(closes, 30)
        sma50 = calc_sma(closes, 50)
        sma100 = calc_sma(closes, 100)
        sma200 = calc_sma(closes, 200)

        if not all([sma10, sma20, sma30, sma50, sma100, sma200]):
            return None

        # Volume averages
        avg_vol_10 = calc_sma(volumes, 10)
        avg_vol_60 = calc_sma(volumes, 60)
        avg_vol_90 = calc_sma(volumes, 90)
        daily_volume = volumes[-1]

        if not all([avg_vol_10, avg_vol_60, avg_vol_90]):
            return None

        # Average trading value (last 20 days)
        recent_closes = closes[-20:]
        recent_volumes = volumes[-20:]
        avg_trading_value = sum(c * v for c, v in zip(recent_closes, recent_volumes)) / len(recent_closes)

        # Get market cap and name from fast_info
        try:
            info = tk.fast_info
            market_cap = getattr(info, 'market_cap', 0) or 0
        except:
            market_cap = 0

        # Get company name
        try:
            name = tk.info.get('shortName', '') or tk.info.get('longName', ticker_code)
        except:
            name = ticker_code

        # 5-day change %
        if len(closes) >= 6:
            price_5d_ago = closes[-6]
            change_5d_pct = ((last_price - price_5d_ago) / price_5d_ago * 100) if price_5d_ago != 0 else 0
        else:
            change_5d_pct = 0

        # Get sector
        try:
            sector = tk.info.get('sector', '') or ''
        except:
            sector = ''

        # Apply filters
        # Market cap > HK$1B
        if market_cap > 0 and market_cap < MCAP_THRESHOLD:
            return None
        if last_price < 10:
            return None
        if daily_volume < 500_000:
            return None
        if avg_vol_10 < 500_000:
            return None
        if avg_vol_60 < 500_000:
            return None
        if avg_vol_90 < 500_000:
            return None
        if avg_trading_value < 50_000_000:
            return None
        if sma10 <= sma20:
            return None
        if sma20 <= sma50:
            return None
        if sma50 <= sma100:
            return None
        if sma100 <= sma200:
            return None
        if last_price <= sma30:
            return None

        change = last_price - prev_close
        change_pct = (change / prev_close * 100) if prev_close != 0 else 0

        with _print_lock:
            _pass_count[0] += 1
            print(f"[Pass] {ticker_code} ({name}) HK${last_price:.2f} MCap={market_cap/1e9:.1f}B", file=sys.stderr)

        return {
            "ticker": ticker_code,
            "name": name,
            "price": round(last_price, 2),
            "change": round(change, 2),
            "changePercent": round(change_pct, 2),
            "change5dPercent": round(change_5d_pct, 2),
            "marketCap": int(market_cap),
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
            "sector": sector,
            "indices": [],
        }

    except Exception as e:
        with _print_lock:
            _fail_count[0] += 1
        return None


def main():
    print("[HK Screener] Starting scan...", file=sys.stderr)
    start_time = time.time()

    # Load universe
    if not os.path.exists(UNIVERSE_FILE):
        print(f"[HK Screener] ERROR: Universe file not found: {UNIVERSE_FILE}", file=sys.stderr)
        return

    with open(UNIVERSE_FILE) as f:
        tickers = [line.strip() for line in f if line.strip()]

    print(f"[HK Screener] Universe: {len(tickers)} tickers", file=sys.stderr)

    # ─── Phase 1: Quick pre-filter via yfinance download (batch) ───
    print("[HK Screener] Phase 1: Batch download for pre-filtering...", file=sys.stderr)

    # Download recent data for all tickers at once (yfinance supports batch)
    # Split into chunks of 100 for reliability
    chunk_size = 100
    pre_filtered = []

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        try:
            data = yf.download(chunk, period="5d", group_by="ticker", progress=False, threads=True)
            for t in chunk:
                try:
                    if len(chunk) == 1:
                        ticker_data = data
                    else:
                        ticker_data = data[t] if t in data.columns.get_level_values(0) else None

                    if ticker_data is not None and len(ticker_data.dropna()) >= 2:
                        last_close = ticker_data["Close"].dropna().iloc[-1]
                        last_vol = ticker_data["Volume"].dropna().iloc[-1]
                        # Quick filter: price > HK$5 (loose), volume > 100K (loose)
                        if last_close > 5 and last_vol > 100_000:
                            pre_filtered.append(t)
                except:
                    pass
        except Exception as e:
            print(f"  [Batch Error] chunk {i//chunk_size+1}: {e}", file=sys.stderr)
            # On error, include all tickers from this chunk
            pre_filtered.extend(chunk)

        elapsed = time.time() - start_time
        print(f"  [Phase 1] Chunk {i//chunk_size+1}/{(len(tickers)+chunk_size-1)//chunk_size} done, {len(pre_filtered)} candidates ({elapsed:.0f}s)", file=sys.stderr)

    print(f"[HK Screener] Phase 1 complete: {len(pre_filtered)} candidates from {len(tickers)}", file=sys.stderr)

    # ─── Phase 2: Full analysis per ticker ────────────────────
    print(f"[HK Screener] Phase 2: Full analysis of {len(pre_filtered)} tickers...", file=sys.stderr)

    passing = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_ticker, t): t for t in pre_filtered}
        done = 0
        for future in as_completed(futures):
            done += 1
            if done % 20 == 0:
                elapsed = time.time() - start_time
                print(f"  [Phase 2] {done}/{len(pre_filtered)} processed, {_pass_count[0]} passing ({elapsed:.0f}s)", file=sys.stderr)
            try:
                result = future.result()
                if result:
                    passing.append(result)
            except:
                pass

    # Sort by market cap descending
    passing.sort(key=lambda x: x["marketCap"], reverse=True)

    elapsed = time.time() - start_time
    print(f"\n[HK Screener] ═══════════════════════════════════════", file=sys.stderr)
    print(f"[HK Screener] Complete. {len(passing)} stocks pass all filters.", file=sys.stderr)
    print(f"[HK Screener] Total time: {elapsed:.0f}s ({elapsed/60:.1f}min)", file=sys.stderr)
    print(f"[HK Screener] Passed: {_pass_count[0]}, Skipped: {_skip_count[0]}, Failed: {_fail_count[0]}", file=sys.stderr)

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
