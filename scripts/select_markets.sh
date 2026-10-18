#!/usr/bin/env bash
# Decide which markets the scan-and-deploy workflow should scan.
#
# Usage: select_markets.sh <schedule-cron> <requested-market>
#   <schedule-cron>     github.event.schedule — the cron that fired, empty for
#                       a manual/workflow_dispatch run
#   <requested-market>  github.event.inputs.market, empty treated as "both"
#
# Prints one of: us | hk | jp | us hk | hk jp | us hk jp
#
# Why this exists: the old logic picked the market from the wall-clock hour at
# START time. GitHub's scheduler routinely starts runs 2-4 hours late, so a
# 06:45 UTC JP run that started at 08:40 matched the 08:xx branch ("hk jp"),
# and a 00:00 all-markets run that started at 02:29 matched "*" ("us hk").
# JP was therefore almost never scanned. The cron that triggered the run is
# authoritative regardless of when it actually starts.
set -u

SCHEDULE_CRON="${1:-}"
REQUESTED="${2:-}"
[ -z "$REQUESTED" ] && REQUESTED="both"

# ── Explicit request (manual dispatch or watchdog) ────────────────────
case "$REQUESTED" in
  all) echo "us hk jp"; exit 0 ;;
  us|hk|jp) echo "$REQUESTED"; exit 0 ;;
  both) : ;;                       # fall through to auto-detection
  *) echo "unknown market '$REQUESTED'" >&2; exit 2 ;;
esac

# ── Scheduled run: trust the cron, not the clock ───────────────────────
if [ -n "$SCHEDULE_CRON" ]; then
  case "$SCHEDULE_CRON" in
    "0 21 * * 1-5")  echo "us";       exit 0 ;;   # US close
    "0 0 * * 1-5")   echo "us hk jp"; exit 0 ;;   # pre-open, all markets
    "45 6 * * 1-5")  echo "jp";       exit 0 ;;   # TSE close
    "30 8 * * 1-5")  echo "hk jp";    exit 0 ;;   # HK close
    "30 13 * * 1-5"|"0 14 * * 1-5"|"30 14 * * 1-5"|"0 15 * * 1-5")
                     echo "us";       exit 0 ;;   # US open / intraday
    "0 16,17,18,19,20 * * 1-5")
                     echo "us";       exit 0 ;;   # hourly US session
  esac
  echo "unmapped cron '$SCHEDULE_CRON', falling back to the clock" >&2
fi

# ── Fallback: dispatches start immediately, so the clock is fine here ──
HOUR=$(date -u +%H)
HOUR=$((10#$HOUR))
case "$HOUR" in
  6|7)        echo "jp" ;;
  8)          echo "hk jp" ;;
  0)          echo "us hk jp" ;;
  *)          echo "us" ;;        # US session and everything else
esac
