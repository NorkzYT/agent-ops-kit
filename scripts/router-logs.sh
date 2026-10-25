#!/usr/bin/env bash
# router-logs.sh — model_router lines from the Hermes agent log.
#   snapshot (default)  last LINES (default 20) matching lines
#   follow              snapshot, then live follow; tail -F survives log rotation
# Scope (2nd arg):
#   all (default)       every line mentioning model_router
#   specialists         only the plugin logger's own specialist-routing records
#                       ("specialists task=<hash> ... source= routes= reason=" and
#                       "specialist routing skipped"). Anchored on the timestamp,
#                       level and logger name, so a message another component
#                       logs that merely quotes those words (task text) never shows.
# Reads: HERMES_HOME (default ~/.hermes), LINES (positive integer).
set -euo pipefail

mode="${1:-snapshot}"
scope="${2:-all}"
lines="${LINES:-20}"
log="${HERMES_HOME:-$HOME/.hermes}/logs/agent.log"

case "$mode" in
  snapshot|follow) ;;
  *) echo "usage: router-logs.sh [snapshot|follow] [all|specialists]" >&2; exit 2 ;;
esac
case "$scope" in
  all) pattern="model_router"; match=(-F -- "$pattern") ;;
  specialists)
    pattern="specialist routing"
    match=(-E -- '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:,.]+ [A-Z]+ hermes\.plugin\.model_router: model_router: specialist(s task=| routing skipped)') ;;
  *) echo "usage: router-logs.sh [snapshot|follow] [all|specialists]" >&2; exit 2 ;;
esac
[[ "$lines" =~ ^[1-9][0-9]*$ ]] || { echo "LINES must be a positive integer (got '$lines')" >&2; exit 2; }

if [[ -f "$log" ]]; then
  matches="$(grep "${match[@]}" "$log" | tail -n "$lines")" || true
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
  tail -n 0 -F -- "$log" | grep --line-buffered "${match[@]}"
fi
