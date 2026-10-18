#!/usr/bin/env python3
"""Phase-1 helpers shared by the HK and JP scanners.

These two scanners used to carry their own copy of everything here. The copies
had drifted to 473 vs 519 lines with only ~14 differing lines inside
`analyze_from_batch` — all of them thresholds. That is a trap: a filter change
applied to one market silently leaves the other on the old rule.

The market-specific numbers (price floor, volume floor, traded-value floor,
timezone, session close) are now parameters. `scan_us.py` keeps its own copy of
the US rules because it also carries the Nasdaq pre-filter and the 1-year
download strategy; only the pure helpers could be shared without dragging that
along.
"""
import sys
import time

import yfinance as yf

from enrich import compute_extras, patch_last_bar


def calc_sma(prices, period):
    """Simple moving average of the last `period` values, or None."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def is_partial_bar(last_index, tz, close_hm):
    """True if the last daily bar is today's session and the market has not
    closed yet (intraday scan), i.e. its volume is incomplete.

    `close_hm` is the local (hour, minute) after which the session counts as
    closed: HKEX (16, 15), TSE (15, 35).
    """
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(tz))
        bar_date = last_index.date() if hasattr(last_index, "date") else last_index
        return bar_date == now.date() and (now.hour, now.minute) < close_hm
    except Exception:
        return False


def analyze_from_batch(ticker, closes, volumes, price_min, vol_min, value_min,
                       partial_last_bar=False):
    """Phase 1: analyze one ticker from pre-downloaded close/volume arrays.

    Returns a stock record if every SMA / volume / traded-value filter passes,
    else None. `partial_last_bar=True` means the final bar is today's still-open
    session: price and SMAs use it (that is the point of an intraday scan), but
    every volume-based test uses completed sessions only.
    """
    if len(closes) < 200:
        return None

    last_price = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else last_price

    if last_price < price_min:
        return None

    sma10 = calc_sma(closes, 10)
    sma20 = calc_sma(closes, 20)
    sma30 = calc_sma(closes, 30)
    sma50 = calc_sma(closes, 50)
    sma100 = calc_sma(closes, 100)
    sma200 = calc_sma(closes, 200)

    if not all([sma10, sma20, sma30, sma50, sma100, sma200]):
        return None

    # Relaxed alignment: 10 > 20 > 50, 100 > 200, 50 > 200.
    # The 50 > 100 leg is recorded as `strict` so the frontend can toggle it.
    if sma10 <= sma20 or sma20 <= sma50 or sma100 <= sma200 or sma50 <= sma200:
        return None
    strict = sma50 > sma100
    if last_price <= sma30:
        return None

    # Volume
    vol_c = volumes[:-1] if partial_last_bar else volumes
    close_c = closes[:-1] if partial_last_bar else closes
    if len(vol_c) < 90:
        return None
    avg_vol_10 = calc_sma(vol_c, 10)
    avg_vol_60 = calc_sma(vol_c, 60)
    avg_vol_90 = calc_sma(vol_c, 90)
    daily_volume = vol_c[-1]

    if not all([avg_vol_10, avg_vol_60, avg_vol_90]):
        return None

    if daily_volume < vol_min:
        return None
    if avg_vol_10 < vol_min:
        return None
    if avg_vol_60 < vol_min:
        return None
    if avg_vol_90 < vol_min:
        return None

    # Average trading value (last 20 days)
    recent_closes = close_c[-20:]
    recent_volumes = vol_c[-20:]
    avg_trading_value = sum(c * v for c, v in zip(recent_closes, recent_volumes)) / len(recent_closes)

    if avg_trading_value < value_min:
        return None

    change = last_price - prev_close
    change_pct = (change / prev_close * 100) if prev_close != 0 else 0

    if len(closes) >= 6:
        price_5d_ago = closes[-6]
        change_5d_pct = ((last_price - price_5d_ago) / price_5d_ago * 100) if price_5d_ago != 0 else 0
    else:
        change_5d_pct = 0

    out = {
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
        "strict": strict,
    }
    out.update(compute_extras(closes))
    return out


def _fetch_metadata_live(ticker_code, retries=3):
    for attempt in range(retries):
        try:
            tk = yf.Ticker(ticker_code)
            market_cap = 0
            name = ticker_code
            sector = ''

            try:
                fi = tk.fast_info
                market_cap = getattr(fi, 'market_cap', 0) or 0
            except Exception:
                pass

            try:
                info = tk.info
                name = info.get('shortName', '') or info.get('longName', ticker_code)
                sector = info.get('sector', '') or ''
            except Exception:
                pass

            # If we got at least market_cap or name, return
            if market_cap > 0 or name != ticker_code:
                return {"market_cap": market_cap, "name": name, "sector": sector}

            # If first attempt got nothing, retry
            if attempt < retries - 1:
                time.sleep(2)
                continue

            return {"market_cap": market_cap, "name": name, "sector": sector}
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                return {"market_cap": 0, "name": ticker_code, "sector": ""}

    return {"market_cap": 0, "name": ticker_code, "sector": ""}


def fetch_metadata(ticker_code, meta_cache, retries=3):
    """Phase 2: market cap (Yahoo fast_info) + name/sector (Yahoo .info, falling
    back to the offline META_CACHE when .info is unavailable)."""
    out = _fetch_metadata_live(ticker_code, retries)
    c = meta_cache.get(ticker_code) or {}
    if (not out["name"] or out["name"] == ticker_code) and c.get("name"):
        out["name"] = c["name"]
    if not out["sector"] and c.get("sector"):
        out["sector"] = c["sector"]
    return out


def download_chunk_with_retry(chunk, tz_name, max_retries=4):
    """yf.download one chunk with backoff, then repair the newest (often NaN)
    daily bar. Returns a DataFrame or None."""
    delay = 30
    for attempt in range(max_retries):
        try:
            d = yf.download(chunk, period="1y", group_by="ticker", progress=False, threads=False)
            if d is not None and not d.empty:
                return patch_last_bar(d, chunk, tz_name,
                                      log=lambda m: print(m, file=sys.stderr))
            raise RuntimeError("empty dataframe")
        except Exception as e:
            msg = str(e)
            is_rate = "Rate" in msg or "429" in msg or "Too Many" in msg or "empty" in msg
            if attempt < max_retries - 1:
                wait = delay * (2 ** attempt) if is_rate else 10
                print(f"    retry {attempt+1} in {wait}s ({msg[:80]})", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"    chunk failed permanently: {msg[:120]}", file=sys.stderr)
    return None
