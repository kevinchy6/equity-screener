#!/usr/bin/env python3
"""Momentum screen (second tab of the equity screener).

Criteria (all must hold):
  * Price above the market's price threshold ($10 / HK$10)
  * Average Daily Range (ADR, 20-day) above 4%
  * RS Rating above 90 (1-99 percentile of IBD-style weighted 12-month
    performance across the WHOLE scanned universe, not just the passers)
  * Price above the 50-day EMA
  * 10-day EMA above the 20-day EMA
  * (% above the 52-week low is REPORTED, not filtered)

Shared by scan_us.py and scan_hk.py. Each scanner feeds every ticker it
downloaded into `rs_score()` (so the RS percentile is universe-wide), and runs
`analyze_momentum()` to test the non-RS criteria. `rank_rs()` then turns the
raw scores into 1-99 ratings and `select_momentum()` keeps rating > 90.
"""
from enrich import compute_extras, apply_history

ADR_MIN_PCT = 4.0
ADR_PERIOD = 20
RS_MIN = 90


def ema(values, period):
    """Standard EMA seeded with the SMA of the first `period` values."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _ret(closes, n):
    if len(closes) > n and closes[-1 - n]:
        return closes[-1] / closes[-1 - n] - 1
    return None


def rs_score(closes):
    """IBD-style weighted 12-month performance: the most recent quarter counts
    double (40%), the other three quarters 20% each. With ~1y of history the
    12-month leg falls back to the oldest available close."""
    if len(closes) < 130:
        return None
    r63 = _ret(closes, 63)
    r126 = _ret(closes, 126)
    r189 = _ret(closes, 189)
    r252 = _ret(closes, 252)
    if r252 is None and closes[0]:
        r252 = closes[-1] / closes[0] - 1
    if r189 is None:
        r189 = r252
    if r63 is None or r126 is None or r189 is None or r252 is None:
        return None
    return (2 * r63 + r126 + r189 + r252) / 5 * 100


def rank_rs(scores):
    """{ticker: score} -> {ticker: 1..99 percentile rating}."""
    items = [(t, s) for t, s in scores.items() if s is not None]
    n = len(items)
    if n < 2:
        return {t: None for t, _ in items}
    items.sort(key=lambda x: x[1])
    out = {}
    for rank, (t, _) in enumerate(items):
        out[t] = max(1, min(99, round(rank / (n - 1) * 98 + 1)))
    return out


def adr_pct(highs, lows, period=ADR_PERIOD):
    """Average Daily Range %: mean of (High/Low - 1) over the last `period` bars."""
    if len(highs) < period or len(lows) < period:
        return None
    vals = []
    for h, l in zip(highs[-period:], lows[-period:]):
        if h and l and l > 0:
            vals.append(h / l - 1)
    if len(vals) < period * 0.8:
        return None
    return sum(vals) / len(vals) * 100


def analyze_momentum(ticker, closes, highs, lows, volumes, price_threshold,
                     partial_last_bar=False):
    """Test every criterion except RS Rating (needs the whole universe).
    Returns a stock record or None."""
    if len(closes) < 130:
        return None
    price = closes[-1]
    if price <= price_threshold:
        return None

    e10 = ema(closes, 10)
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if e10 is None or e20 is None or e50 is None:
        return None
    if not (e10 > e20 and price > e50):
        return None

    # Range / volume statistics on completed sessions only when the last bar is
    # today's still-open session.
    h_c = highs[:-1] if partial_last_bar else highs
    l_c = lows[:-1] if partial_last_bar else lows
    v_c = volumes[:-1] if partial_last_bar else volumes
    c_c = closes[:-1] if partial_last_bar else closes
    adr = adr_pct(h_c, l_c)
    if adr is None or adr <= ADR_MIN_PCT:
        return None

    # Distance from the 52-week low: shown as a column, not a filter.
    low52 = min(x for x in lows[-252:] if x and x > 0)
    pct_above_low = (price / low52 - 1) * 100

    prev_close = closes[-2]
    change = price - prev_close
    change_pct = change / prev_close * 100 if prev_close else 0
    change_5d = (price / closes[-6] - 1) * 100 if len(closes) >= 6 and closes[-6] else 0
    recent_c, recent_v = c_c[-20:], v_c[-20:]
    avg_value = sum(c * v for c, v in zip(recent_c, recent_v)) / max(1, len(recent_c))

    out = {
        "ticker": ticker,
        "name": ticker,
        "sector": "",
        "marketCap": 0,
        "price": round(price, 2),
        "change": round(change, 2),
        "changePercent": round(change_pct, 2),
        "change5dPercent": round(change_5d, 2),
        "volume": int(v_c[-1]) if v_c else 0,
        "avgTradingValue": int(avg_value),
        "adr": round(adr, 2),
        "ema10": round(e10, 2),
        "ema20": round(e20, 2),
        "ema50": round(e50, 2),
        "low52": round(low52, 2),
        "pctAbove52wLow": round(pct_above_low, 1),
    }
    out.update(compute_extras(closes))
    return out


def select_momentum(candidates, ratings, scores, min_rs=RS_MIN):
    """Keep candidates whose universe-wide RS Rating is above `min_rs`."""
    out = []
    for s in candidates:
        r = ratings.get(s["ticker"])
        if r is None or r <= min_rs:
            continue
        s["rsRating"] = r
        sc = scores.get(s["ticker"])
        s["rsScore"] = round(sc, 1) if sc is not None else None
        out.append(s)
    out.sort(key=lambda x: (x["rsRating"], x.get("rsScore") or 0), reverse=True)
    return out


def finalize_momentum(mom, history_file, tz_name):
    """Track the momentum list day over day (NEW badge / streak days)."""
    apply_history(mom, history_file, tz_name, key="datesMom",
                  streak_field="streak", new_field="isNew")
    return len(mom)
