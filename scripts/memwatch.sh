#!/usr/bin/env bash
# Watch ha-airspace RSS while it runs, to verify the reference-DB refresh
# plateaus instead of climbing.
#
# Sums the process tree rooted at the service we launched -- `uv run` spawns
# python as a child, so measuring the launched PID alone reports the wrapper.
# Matching on the config path instead would silently double-count a second
# instance started from another terminal, which is exactly the mistake this
# script exists to avoid, so it also refuses to start alongside one.
#
# Usage: scripts/memwatch.sh [config.local.yaml] [interval_s]
set -uo pipefail

CONFIG="${1:-config.local.yaml}"
INTERVAL="${2:-10}"

# Look for a live service by inspecting argv directly rather than pgrep -f:
# the pattern would otherwise match any shell whose command line merely quotes
# this script (including the one that wrote it).
running_pids() {
    local pid args argv0 argv1
    for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
        [ "$pid" = "$$" ] && continue
        [ -r "/proc/$pid/cmdline" ] || continue
        args=$( { tr '\0' '\n' < "/proc/$pid/cmdline"; } 2>/dev/null ) || continue
        argv0=$(printf '%s' "$args" | sed -n 1p)
        argv1=$(printf '%s' "$args" | sed -n 2p)
        case "${argv0##*/}:${argv1##*/}" in
            ha-airspace:*|*:ha-airspace) ;;
            *) continue ;;
        esac
        printf '%s\n' "$args" | grep -qF -- "$CONFIG" && printf '%s %s\n' "$pid" "$argv0"
    done
}

existing=$(running_pids)
if [ -n "$existing" ]; then
    echo "error: an ha-airspace is already running against ${CONFIG}." >&2
    echo "       measuring now would sum both instances. Stop it first:" >&2
    printf '%s\n' "$existing" | sed 's/^/         /' >&2
    exit 1
fi

LOG="$(mktemp -t airspace-memwatch-XXXX.log)"
echo "config: $CONFIG"
echo "log:    $LOG"
echo

uv run ha-airspace -c "$CONFIG" >"$LOG" 2>&1 &
SVC=$!
trap 'pkill -P $SVC 2>/dev/null; kill $SVC 2>/dev/null; echo; echo "stopped. full log: $LOG"' EXIT INT TERM

# All PIDs in the tree rooted at $1, depth-first.
tree_pids() {
    local pid=$1 child
    printf '%s\n' "$pid"
    for child in $(pgrep -P "$pid" 2>/dev/null); do tree_pids "$child"; done
}

peak=0
while kill -0 $SVC 2>/dev/null; do
    rss=$(for p in $(tree_pids $SVC); do
              awk '/^VmRSS/{print $2}' "/proc/$p/status" 2>/dev/null
          done | awk '{s+=$1} END {printf "%.0f", s/1024}')
    [ -z "$rss" ] && rss=0
    [ "$rss" -gt "$peak" ] && peak=$rss
    db=$(grep -c db_refreshed "$LOG" 2>/dev/null); db=${db:-0}
    printf '%s  %5s MB   peak %5s MB   db_refreshed=%s\n' \
        "$(date +%H:%M:%S)" "$rss" "$peak" "$db"
    sleep "$INTERVAL"
done
