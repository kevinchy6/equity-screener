#!/usr/bin/env python3
"""Shared enrichment helpers for the equity screener.

Adds per-stock extras (52-week-high distance, 3-month return, sparkline),
cross-sectional RS ranks, and list-history tracking (NEW / streak days).
"""
import json
import os
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def utc_now_iso():
    """Timezone-explicit UTC timestamp (so browsers parse it correctly)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_extras(closes):
    """Extras computed from a 1y daily close series (oldest → newest)."""
    last = closes[-1]
    high52 = max(closes)
    pct_from_high = (last / high52 - 1) * 100 if high52 else 0

    ret63 = None
    if len(closes) >= 64 and closes[-64]:
        ret63 = (last / closes[-64] - 1) * 100
    ret21 = None
    if len(closes) >= 22 and closes[-22]:
        ret21 = (last / closes[-22] - 1) * 100

    spark = [round(v, 2 if v < 1000 else 1) for v in closes[-30:]]

    return {
        "pctFrom52wHigh": round(pct_from_high, 1),
        "ret63": round(ret63, 2) if ret63 is not None else None,
        "ret21": round(ret21, 2) if ret21 is not None else None,
        "spark": spark,
    }


def add_rs_ranks(passing, field="rs", ret_field="ret63"):
    """RS 1-99: percentile rank of `ret_field` (default 3-month return) among
    the given stocks.

    Stocks not in `passing` are untouched; callers that rank a subset should
    pre-set the field to None on the excluded stocks.
    """
    vals = [(i, s.get(ret_field)) for i, s in enumerate(passing)
            if s.get(ret_field) is not None]
    n = len(vals)
    if n < 2:
        for s in passing:
            s[field] = None
        return
    order = sorted(vals, key=lambda x: x[1])
    for rank, (i, _) in enumerate(order):
        passing[i][field] = max(1, min(99, round(rank / (n - 1) * 98 + 1)))
    for s in passing:
        s.setdefault(field, None)


def add_composite_rs(passing, field="rsComp", a="rs", b="rs1m"):
    """Composite RS 1-99: percentile rank of the equal-weighted average of two
    RS ranks (default 3-month `rs` and 1-month `rs1m`). Re-ranking the average
    keeps the result on the same 1-99 percentile scale.
    """
    for s in passing:
        ra, rb = s.get(a), s.get(b)
        s["_comp"] = (ra + rb) / 2 if (ra is not None and rb is not None) else None
    add_rs_ranks(passing, field=field, ret_field="_comp")
    for s in passing:
        s.pop("_comp", None)


def apply_history(passing, history_file, tz_name, keep=40,
                  key="dates", streak_field="streak", new_field="isNew"):
    """Track daily lists; annotate each stock with streak / isNew.

    history JSON: {"dates": {"YYYY-MM-DD": ["TICK", ...], ...}, "datesAll": {...}}
    `key` selects which list is tracked (strict vs relaxed). Other keys in
    the file are preserved. Re-runs on the same date overwrite that date.
    """
    if ZoneInfo is not None:
        today = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    doc = {}
    if os.path.exists(history_file):
        try:
            with open(history_file) as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                doc = {}
        except Exception:
            doc = {}
    hist = doc.get(key, {}) if isinstance(doc.get(key), dict) else {}

    hist[today] = sorted(s["ticker"] for s in passing)
    dates = sorted(hist.keys())[-keep:]
    hist = {d: hist[d] for d in dates}

    prior = [d for d in dates if d < today]
    sets = {d: set(hist[d]) for d in dates}
    for s in passing:
        streak = 1
        for d in reversed(prior):
            if s["ticker"] in sets[d]:
                streak += 1
            else:
                break
        s[streak_field] = streak
        s[new_field] = bool(prior) and streak == 1

    doc[key] = hist
    with open(history_file, "w") as f:
        json.dump(doc, f)
    return today


def finalize_lists(passing, history_file, tz_name):
    """Rank + track both the strict list (SMA50 > SMA100 required) and the
    relaxed list (all stocks, incl. those where only SMA50 <= SMA100).

    Strict fields: rs (3M) / rs1m (1M) / streak / isNew (None for relaxed-only).
    Relaxed fields: rsAll / rs1mAll / streakAll / isNewAll (set for every stock).
    """
    strict = [s for s in passing if s.get("strict", True)]
    for s in passing:
        if not s.get("strict", True):
            s["rs"] = None
            s["rs1m"] = None
            s["rsComp"] = None
            s["streak"] = None
            s["isNew"] = False
    add_rs_ranks(strict, field="rs", ret_field="ret63")
    add_rs_ranks(strict, field="rs1m", ret_field="ret21")
    add_composite_rs(strict, field="rsComp", a="rs", b="rs1m")
    add_rs_ranks(passing, field="rsAll", ret_field="ret63")
    add_rs_ranks(passing, field="rs1mAll", ret_field="ret21")
    add_composite_rs(passing, field="rsCompAll", a="rsAll", b="rs1mAll")
    apply_history(strict, history_file, tz_name, key="dates",
                  streak_field="streak", new_field="isNew")
    apply_history(passing, history_file, tz_name, key="datesAll",
                  streak_field="streakAll", new_field="isNewAll")
    return len(strict)


# ─── Latest-bar repair ──────────────────────────────────────────────────────
def _official_closes(chunk, tz_name, workers=8):
    """{ticker: {"price", "volume", "date"}} from Yahoo's v8 chart meta
    (regularMarketPrice / regularMarketVolume / regularMarketTime). Best
    effort: any failure just leaves the ticker out."""
    import datetime as _dt
    import concurrent.futures as cf
    from zoneinfo import ZoneInfo
    try:
        from curl_cffi import requests as _rq
        mk = lambda: _rq.Session(impersonate="chrome")
    except Exception:
        import requests as _rq
        mk = lambda: _rq.Session()
    tz = ZoneInfo(tz_name)

    def one(t):
        try:
            r = mk().get(f"https://query2.finance.yahoo.com/v8/finance/chart/{t}",
                         params={"range": "1d", "interval": "1d"}, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
            res = r.json().get("chart", {}).get("result")
            if not res:
                return t, None
            m = res[0].get("meta", {})
            ts = m.get("regularMarketTime")
            if not ts or not m.get("regularMarketPrice"):
                return t, None
            return t, {"price": float(m["regularMarketPrice"]),
                       "volume": m.get("regularMarketVolume"),
                       "date": _dt.datetime.fromtimestamp(ts, tz).date()}
        except Exception:
            return t, None

    out = {}
    try:
        with cf.ThreadPoolExecutor(workers) as ex:
            for t, v in ex.map(one, chunk):
                if v:
                    out[t] = v
    except Exception:
        pass
    return out


def patch_last_bar(data, chunk, tz_name, log=None):
    """Repair the latest session in a `yf.download(..., interval='1d')` frame.

    For several hours after the close Yahoo's daily endpoint returns the new
    session row with Close = NaN (or omits the row entirely). `dropna` would
    then silently make the scan describe the PREVIOUS session (price, Chg%,
    SMA alignment all one day stale). Hourly bars for that session are
    already complete, so we aggregate them (O first / H max / L min / C last /
    V sum) and fill or append the daily row.

    Returns the (possibly new) DataFrame; on any failure returns `data`
    unchanged so the scan degrades to the old behaviour.
    """
    import math
    import pandas as pd
    import yfinance as yf

    def _say(msg):
        if log:
            log(msg)

    try:
        hourly = yf.download(chunk, period="5d", interval="60m", group_by="ticker",
                             progress=False, threads=False, auto_adjust=False)
    except Exception as e:
        _say(f"    [patch] hourly download failed: {str(e)[:80]}")
        return data
    if hourly is None or hourly.empty:
        return data

    single = len(chunk) == 1
    official = _official_closes(chunk, tz_name)
    patched = 0
    appended_rows = {}
    for t in chunk:
        try:
            tdf = data if single else (data[t] if t in data.columns.get_level_values(0) else None)
            hdf = hourly if single else (hourly[t] if t in hourly.columns.get_level_values(0) else None)
            if tdf is None or hdf is None or tdf.empty:
                continue
            hdf = hdf.dropna(subset=["Close"])
            if hdf.empty:
                continue
            idx = hdf.index
            idx = idx.tz_convert(tz_name) if idx.tz is not None else idx.tz_localize("UTC").tz_convert(tz_name)
            hdf = hdf.copy()
            hdf.index = idx
            last_day = idx[-1].date()
            day = hdf[[d.date() == last_day for d in idx]]
            if day.empty:
                continue
            bar = {
                "Open": float(day["Open"].iloc[0]),
                "High": float(day["High"].max()),
                "Low": float(day["Low"].min()),
                "Close": float(day["Close"].iloc[-1]),
                "Volume": float(day["Volume"].sum()),
            }
            if any(math.isnan(v) for v in bar.values()) or bar["Close"] <= 0:
                continue
            # Prefer Yahoo's official closing print / full-day volume when the
            # quote endpoint has it for the same session (hourly bars miss the
            # closing auction, typically ~0.1% off).
            off = official.get(t)
            if off and off["date"] == last_day:
                if off["price"]:
                    bar["Close"] = off["price"]
                    bar["High"] = max(bar["High"], off["price"])
                    bar["Low"] = min(bar["Low"], off["price"])
                if off["volume"]:
                    bar["Volume"] = float(off["volume"])

            valid = tdf.dropna(subset=["Close"])
            last_valid = valid.index[-1].date() if not valid.empty else None
            if last_valid is not None and last_valid >= last_day:
                continue  # daily data already covers this session

            row_idx = None
            for ix in tdf.index[-3:]:
                if ix.date() == last_day:
                    row_idx = ix
            if row_idx is not None:
                # Row exists with NaN Close: fill missing fields only, keep
                # whatever Yahoo did give (e.g. Volume) if it is non-NaN.
                for f, v in bar.items():
                    col = f if single else (t, f)
                    cur = data.loc[row_idx, col] if col in data.columns else float("nan")
                    if cur is None or (isinstance(cur, float) and math.isnan(cur)):
                        data.loc[row_idx, col] = v
            else:
                ts = pd.Timestamp(last_day)
                if tdf.index.tz is not None:
                    ts = ts.tz_localize(tdf.index.tz)
                appended_rows.setdefault(ts, {})[t] = bar
            patched += 1
        except Exception as e:
            _say(f"    [patch] {t}: {str(e)[:80]}")
            continue

    if appended_rows:
        for ts, per_t in appended_rows.items():
            if ts not in data.index:
                data.loc[ts] = float("nan")
            for t, bar in per_t.items():
                for f, v in bar.items():
                    col = f if single else (t, f)
                    if col in data.columns:
                        data.loc[ts, col] = v
        data = data.sort_index()
    if patched:
        _say(f"    [patch] filled latest session from hourly bars for {patched}/{len(chunk)} tickers")
    return data
