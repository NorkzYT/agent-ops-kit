#!/usr/bin/env bash
# hermes-ui.sh {start|status|stop} — drive `hermes dashboard`, the web UI that
# manages Hermes config, API keys and sessions. Bound to loopback only: this
# surface can read/write credentials and start sessions, so it must never be
# exposed beyond 127.0.0.1. HERMES_UI_PORT overrides the port (default 9119) and
# is validated so it cannot smuggle extra hermes arguments or shell characters.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"
export PATH="$HOME/.local/bin:$PATH"

have hermes || die "hermes not installed. Run: make hermes-install"

# Point the UI at the kit's Hermes home, mirroring the other kit scripts.
if [[ -f .env ]]; then
  HERMES_HOME="$(env_file_get HERMES_HOME .env 2>/dev/null || printf '%s' "${HERMES_HOME:-$HOME/.hermes}")"
else
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
fi
export HERMES_HOME

case "${1:-start}" in
  start)
    port="${HERMES_UI_PORT:-9119}"
    # A bare integer only — reject "9119 --host 0.0.0.0", "$(cmd)", ";rm", etc.
    [[ "$port" =~ ^[0-9]+$ ]] || die "HERMES_UI_PORT must be a plain port number (got: $port)"
    [[ "$port" -ge 1 && "$port" -le 65535 ]] || die "HERMES_UI_PORT out of range 1-65535 (got: $port)"
    log "Hermes dashboard on http://127.0.0.1:${port}  (Ctrl-C to stop)"
    # Foreground + loopback. Ctrl-C reaches hermes directly (exec, no wrapper).
    exec hermes dashboard --host 127.0.0.1 --port "$port" --no-open
    ;;
  status) exec hermes dashboard --status ;;
  stop)   exec hermes dashboard --stop ;;
  *) die "usage: hermes-ui.sh {start|status|stop}" ;;
esac
