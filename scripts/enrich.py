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

    spark = [round(v, 2 if v < 1000 else 1) for v in closes[-30:]]

    return {
        "pctFrom52wHigh": round(pct_from_high, 1),
        "ret63": round(ret63, 2) if ret63 is not None else None,
        "spark": spark,
    }


def add_rs_ranks(passing):
    """RS 1-99: percentile rank of 3-month return among passing stocks."""
    vals = [(i, s.get("ret63")) for i, s in enumerate(passing)
            if s.get("ret63") is not None]
    n = len(vals)
    if n < 2:
        for s in passing:
            s["rs"] = None
        return
    order = sorted(vals, key=lambda x: x[1])
    for rank, (i, _) in enumerate(order):
        passing[i]["rs"] = max(1, min(99, round(rank / (n - 1) * 98 + 1)))
    for s in passing:
        s.setdefault("rs", None)


def apply_history(passing, history_file, tz_name, keep=40):
    """Track daily lists; annotate each stock with streak / isNew.

    history JSON: {"dates": {"YYYY-MM-DD": ["TICK", ...], ...}}
    Re-runs on the same date overwrite that date's entry.
    """
    if ZoneInfo is not None:
        today = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    hist = {}
    if os.path.exists(history_file):
        try:
            with open(history_file) as f:
                hist = json.load(f).get("dates", {})
        except Exception:
            hist = {}

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
        s["streak"] = streak
        s["isNew"] = bool(prior) and streak == 1

    with open(history_file, "w") as f:
        json.dump({"dates": hist}, f)
    return today
