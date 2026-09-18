#!/usr/bin/env python3
"""Momentum screen (second tab of the equity screener).

Criteria (all must hold):
  * Price above the market's price threshold ($10 / HK$10)
  * Average Daily Range (ADR, 20-day) above 4%
  * RS above 90 -- 1-99 percentile rank across the WHOLE scanned universe
    (not just the passers) of the 1-month (21d) return, the 3-month (63d)
    return, or the composite (average of both ranks, re-ranked). All three
    are stored so the frontend can switch between them, exactly like the
    Trend tab; a stock is kept if it is > 90 on ANY of the three.
  * Price above the 50-day EMA
  * 10-day EMA above the 20-day EMA
  * (% above the 52-week low is REPORTED, not filtered)

Shared by scan_us.py and scan_hk.py. Each scanner feeds every ticker it
downloaded into `rs_returns()` (so the RS percentile is universe-wide), and runs
`analyze_momentum()` to test the non-RS criteria. `rs_ratings()` then turns the
returns into 1-99 ranks and `select_momentum()` keeps stocks > 90 on any mode.
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


def rs_returns(closes):
    """1-month (21d) and 3-month (63d) returns in %, or None if too short."""
    if len(closes) < 64:
        return None
    r21 = _ret(closes, 21)
    r63 = _ret(closes, 63)
    if r21 is None or r63 is None:
        return None
    return {"ret21": r21 * 100, "ret63": r63 * 100}


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


def rs_ratings(returns):
    """{ticker: {"ret21", "ret63"}} -> {ticker: {"rs1m", "rs3m", "rsComp"}}.
    Each is a 1-99 percentile rank across the whole universe; rsComp is the
    re-ranked average of the 1M and 3M ranks (same recipe as the Trend tab)."""
    r1 = rank_rs({t: v["ret21"] for t, v in returns.items() if v})
    r3 = rank_rs({t: v["ret63"] for t, v in returns.items() if v})
    comp_in = {t: (r1[t] + r3[t]) / 2 for t in r1 if t in r3 and r1[t] is not None and r3[t] is not None}
    rc = rank_rs(comp_in)
    return {t: {"rs1m": r1.get(t), "rs3m": r3.get(t), "rsComp": rc.get(t)} for t in r1}


RS_MODES = (("rs1m", "datesMom1m", "streak1m", "isNew1m"),
            ("rs3m", "datesMom3m", "streak3m", "isNew3m"),
            ("rsComp", "datesMomComp", "streakComp", "isNewComp"))


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


def pct_above_52w_low(closes, lows):
    """% distance of the last close above the 52-week low (None if unavailable)."""
    try:
        low52 = min(x for x in lows[-252:] if x and x > 0)
        return (closes[-1] / low52 - 1) * 100
    except (ValueError, ZeroDivisionError, IndexError):
        return None


def market_stats(above_low, threshold=70.0):
    """Universe-wide breadth of distance from the 52-week low.
    above_low: {ticker: pct_above_52w_low}. Returns counts/percentages."""
    vals = sorted(v for v in above_low.values() if v is not None)
    n = len(vals)
    if n == 0:
        return {"universe": 0}
    def share(th):
        return round(sum(1 for v in vals if v > th) / n * 100, 1)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {
        "universe": n,
        "above70Count": sum(1 for v in vals if v > threshold),
        "above70Pct": share(threshold),
        "above100Pct": share(100.0),
        "above50Pct": share(50.0),
        "above30Pct": share(30.0),
        "medianPctAbove52wLow": round(median, 1),
    }


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


def select_momentum(candidates, ratings, min_rs=RS_MIN):
    """Keep candidates whose universe-wide RS is above `min_rs` on at least one
    of the three modes; store all three ranks so the UI can switch."""
    out = []
    for s in candidates:
        r = ratings.get(s["ticker"])
        if not r:
            continue
        vals = [r.get("rs1m"), r.get("rs3m"), r.get("rsComp")]
        if not any(v is not None and v > min_rs for v in vals):
            continue
        s["rs1m"], s["rs3m"], s["rsComp"] = vals
        out.append(s)
    out.sort(key=lambda x: ((x.get("rs3m") or 0), (x.get("ret63") or 0)), reverse=True)
    return out


def finalize_momentum(mom, history_file, tz_name, min_rs=RS_MIN):
    """Track each RS-mode list day over day (NEW badge / streak days).
    A stock not in a given mode's list gets None / False for that mode."""
    for rs_field, key, streak_field, new_field in RS_MODES:
        subset = [s for s in mom if (s.get(rs_field) or 0) > min_rs]
        for s in mom:
            s[streak_field] = None
            s[new_field] = False
        apply_history(subset, history_file, tz_name, key=key,
                      streak_field=streak_field, new_field=new_field)
    return len(mom)
