#!/usr/bin/env bash
# router-logs.sh — model_router lines from the Hermes agent log.
#   snapshot (default)  last LINES (default 20) matching lines
#   follow              snapshot, then live follow; tail -F survives log rotation
# Reads: HERMES_HOME (default ~/.hermes), LINES (positive integer).
set -euo pipefail

mode="${1:-snapshot}"
lines="${LINES:-20}"
pattern="model_router"
log="${HERMES_HOME:-$HOME/.hermes}/logs/agent.log"

case "$mode" in
  snapshot|follow) ;;
  *) echo "usage: router-logs.sh [snapshot|follow]" >&2; exit 2 ;;
esac
[[ "$lines" =~ ^[1-9][0-9]*$ ]] || { echo "LINES must be a positive integer (got '$lines')" >&2; exit 2; }

if [[ -f "$log" ]]; then
  matches="$(grep -F -- "$pattern" "$log" | tail -n "$lines")" || true
  if [[ -n "$matches" ]]; then
    printf '%s\n' "$matches"
  else
    echo "No $pattern lines in $log yet." >&2
  fi
else
  echo "No Hermes agent log at $log — is Hermes installed and has it run?" >&2
  [[ "$mode" == "snapshot" ]] && exit 0
fi

if [[ "$mode" == "follow" ]]; then
  echo "Following $log for new $pattern lines (Ctrl-C to stop)..." >&2
  tail -n 0 -F -- "$log" | grep --line-buffered -F -- "$pattern"
fi
