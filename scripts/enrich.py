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
            s["streak"] = None
            s["isNew"] = False
    add_rs_ranks(strict, field="rs", ret_field="ret63")
    add_rs_ranks(strict, field="rs1m", ret_field="ret21")
    add_rs_ranks(passing, field="rsAll", ret_field="ret63")
    add_rs_ranks(passing, field="rs1mAll", ret_field="ret21")
    apply_history(strict, history_file, tz_name, key="dates",
                  streak_field="streak", new_field="isNew")
    apply_history(passing, history_file, tz_name, key="datesAll",
                  streak_field="streakAll", new_field="isNewAll")
    return len(strict)
