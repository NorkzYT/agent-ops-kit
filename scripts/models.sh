#!/usr/bin/env bash
# models.sh — list the models each proxy exposes, distinguishing "not reachable"
# (connection failed) from "reachable but zero models" (up, not authed yet).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"
getenv() { env_file_get "$1" .env 2>/dev/null || printf '%s' "${2:-}"; }

# list URL BEARER HINT — print model ids, or a clear reachable/unreachable note.
list() {
  local url="$1" bearer="$2" hint="$3" out ids
  local -a auth=()
  [[ -n "$bearer" ]] && auth=(-H "Authorization: Bearer $bearer")
  if out="$(curl -fsS -m 10 "${auth[@]}" "$url" 2>/dev/null)"; then
    ids="$(printf '%s' "$out" | grep -o '"id":"[^"]*"' | sed 's/"id":"//; s/"$//')"
    if [[ -n "$ids" ]]; then printf '%s\n' "$ids" | sed 's/^/  /'
    else printf '  (reachable, but no models yet: %s)\n' "$hint"; fi
  else
    printf '  (not reachable: %s)\n' "$hint"
  fi
}

cp_port="$(getenv CLIPROXY_PORT 8317)"
cm_port="$(getenv CLAUDE_MAX_PROXY_PORT 3456)"

echo "== CLIProxyAPI (ChatGPT subscription) http://127.0.0.1:${cp_port}/v1"
list "http://127.0.0.1:${cp_port}/v1/models" "$(getenv CLIPROXY_API_KEY)" "make auth-codex"
echo
echo "== claude-max-proxy (Claude Max) http://127.0.0.1:${cm_port}/v1"
list "http://127.0.0.1:${cm_port}/v1/models" "$(getenv CLAUDE_MAX_PROXY_API_KEY)" "make auth-claude-proxy"
