#!/usr/bin/env bash
# windows-vm-network.sh — publish the host's model/memory APIs to the Windows VM
# worker over Tailscale Serve, while Docker stays bound to 127.0.0.1.
#
#   apply    forward CLIProxyAPI (:8317) and Honcho (:8000) over Tailscale Serve
#   status   verify the loopback endpoints AND the two Serve TCP forwarders
#   off      remove only those two Serve TCP forwarders
#
# Why this shape: the containers bind 127.0.0.1 so the host's own Hermes,
# `make doctor`, and every loopback probe reach them the same way. Tailscale
# Serve terminates TLS on the tailnet and forwards raw TCP to 127.0.0.1:<port>,
# so the VM reaches the same services privately without ever binding Docker to a
# routable address. Access is tailnet-only (Serve, not Funnel) and your
# Tailscale ACLs still apply. `--bg` persists the forwarder across reboots.
#
# It only ever touches the two --tcp listeners it manages; it never runs
# `tailscale serve reset` and never touches Funnel, so an unrelated Serve/Funnel
# config on this host is left exactly as it is.
#
# Test seams: TAILSCALE_BIN / CURL_BIN / SUDO_BIN override the binaries, ENV_FILE
# the dotenv path. It reads no secrets and prints none.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

ENV_FILE="${ENV_FILE:-.env}"
TS="${TAILSCALE_BIN:-tailscale}"
CURL="${CURL_BIN:-curl}"
SUDO="${SUDO_BIN:-sudo}"

# Resolve a config value: explicit environment override wins, else the dotenv
# file, else the default.
cfg() { local k="$1" d="$2"; printf '%s' "${!k:-$(env_file_get "$k" "$ENV_FILE" 2>/dev/null || printf '%s' "$d")}"; }

CLIPROXY_PORT="$(cfg CLIPROXY_PORT 8317)"
HONCHO_PORT="$(cfg HONCHO_PORT 8000)"
CLIPROXY_BIND_ADDR="$(cfg CLIPROXY_BIND_ADDR 127.0.0.1)"
HONCHO_BIND_ADDR="$(cfg HONCHO_BIND_ADDR 127.0.0.1)"

# The two forwarders this script manages: "port bind-var-value human-name".
services=(
  "$CLIPROXY_PORT|$CLIPROXY_BIND_ADDR|CLIProxyAPI|CLIPROXY_BIND_ADDR"
  "$HONCHO_PORT|$HONCHO_BIND_ADDR|Honcho|HONCHO_BIND_ADDR"
)

is_loopback() { # empty (compose default) or a loopback literal
  case "$1" in ""|127.0.0.1|localhost|::1|127.*) return 0;; *) return 1;; esac
}

require_loopback() {
  local rec s port bind name var
  for rec in "${services[@]}"; do
    IFS='|' read -r port bind name var <<<"$rec"
    is_loopback "$bind" || die "$var=$bind is not loopback. Tailscale Serve forwards to 127.0.0.1:$port and the host's own Hermes/\`make doctor\` probe 127.0.0.1, so Docker must stay on loopback. Set $var=127.0.0.1 in $ENV_FILE and \`make up\`, then re-run."
  done
}

require_tailscale() {
  have "$TS" || die "tailscale not found (${TS}). Install Tailscale on the host and join the tailnet, then re-run."
  local state
  state="$("$TS" status --json 2>/dev/null | tr -d ' \n' | grep -o '"BackendState":"[^"]*"' | head -1 | sed 's/.*:"//; s/"$//')"
  if [[ -z "$state" ]]; then
    # No JSON (older CLI or parse failure): fall back to the plain status exit.
    "$TS" status >/dev/null 2>&1 || die "tailscale is installed but not connected. Run \`tailscale up\` and re-run."
  elif [[ "$state" != "Running" ]]; then
    die "tailscale is not connected (BackendState=$state). Run \`tailscale up\` and re-run."
  fi
}

# Print the current Serve TCP-forward target for a port, empty if none.
forward_target() {
  "$TS" serve status --json 2>/dev/null | python3 -c '
import sys, json
port = sys.argv[1]
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
t = (d.get("TCP") or {}).get(port) or {}
v = t.get("TCPForward")
if v:
    print(v)
' "$1"
}

# Run a `tailscale serve` write, escalating through sudo when the operator is
# not set (Tailscale returns "Access denied" for serve writes then).
serve_write() {
  local out rc
  out="$("$TS" serve "$@" 2>&1)"; rc=$?
  if [[ $rc -ne 0 ]] && grep -qi 'denied' <<<"$out"; then
    if have "$SUDO"; then
      warn "tailscale serve needs elevation on this host (Tailscale operator not set). Re-running through sudo — run \`sudo tailscale set --operator=${USER:-$(id -un 2>/dev/null || echo you)}\` once to avoid this."
      out="$("$SUDO" "$TS" serve "$@" 2>&1)"; rc=$?
    fi
  fi
  [[ $rc -eq 0 ]] || die "tailscale serve $* failed: $out"
}

do_apply() {
  require_loopback
  require_tailscale
  local rec port bind name var target cur
  for rec in "${services[@]}"; do
    IFS='|' read -r port bind name var <<<"$rec"
    target="127.0.0.1:$port"
    cur="$(forward_target "$port")"
    if [[ "$cur" == "$target" ]]; then
      log "$name :$port already forwarded over Tailscale Serve → $target (skip)"
      continue
    fi
    [[ -n "$cur" ]] && warn "$name :$port serve forward currently points at $cur; repointing to $target"
    serve_write --bg --yes --tcp="$port" "tcp://127.0.0.1:$port"
    log "$name :$port now forwarded over Tailscale Serve → $target (tailnet-only, persists across reboots)"
  done
  log "Done. Point the Windows worker at http://<host-tailnet>:$CLIPROXY_PORT/v1 (Honcho: :$HONCHO_PORT). Access is tailnet-only; your Tailscale ACLs still apply. Existing Serve/Funnel config on this host was not touched."
}

do_off() {
  require_tailscale
  local rec port bind name var cur
  for rec in "${services[@]}"; do
    IFS='|' read -r port bind name var <<<"$rec"
    cur="$(forward_target "$port")"
    if [[ -z "$cur" ]]; then
      log "$name :$port has no Tailscale Serve TCP forward (skip)"
      continue
    fi
    serve_write --tcp="$port" off
    log "$name :$port Tailscale Serve TCP forward removed"
  done
  log "Removed only the two managed TCP forwarders. Any other Serve/Funnel config on this host is unchanged."
}

do_status() {
  require_loopback
  require_tailscale
  local err=0 rec port bind name var target cur
  for rec in "${services[@]}"; do
    IFS='|' read -r port bind name var <<<"$rec"
    target="127.0.0.1:$port"
    # 1. Docker/service bind is loopback (required for the forward to work).
    if is_loopback "$bind"; then printf '  [OK]   %s bind %s is loopback\n' "$name" "${bind:-127.0.0.1}"
    else printf '  [FAIL] %s bind %s is not loopback\n' "$name" "$bind"; err=1; fi
    # 2. The loopback endpoint actually answers.
    if "$CURL" -fsS -o /dev/null -m 5 "http://127.0.0.1:$port/" >/dev/null 2>&1 \
       || "$CURL" -sS -o /dev/null -m 5 "http://127.0.0.1:$port/" >/dev/null 2>&1; then
      printf '  [OK]   %s answers on 127.0.0.1:%s\n' "$name" "$port"
    else
      printf '  [FAIL] %s not answering on 127.0.0.1:%s (is the stack up?)\n' "$name" "$port"; err=1
    fi
    # 3. Tailscale Serve forwards the tailnet port to that loopback endpoint.
    cur="$(forward_target "$port")"
    if [[ "$cur" == "$target" ]]; then printf '  [OK]   Tailscale Serve forwards :%s → %s (tailnet-only)\n' "$port" "$target"
    elif [[ -n "$cur" ]]; then printf '  [FAIL] Tailscale Serve forwards :%s → %s, expected %s\n' "$port" "$cur" "$target"; err=1
    else printf '  [FAIL] no Tailscale Serve forward on :%s (run: make windows-vm-network)\n' "$port"; err=1; fi
  done
  if [[ $err -eq 0 ]]; then log "Windows VM network path OK: loopback services up, Tailscale Serve forwarders live."; else warn "Windows VM network path has problems (see [FAIL] above)."; fi
  return $err
}

case "${1:-}" in
  apply|on|"") do_apply;;
  off|remove|down) do_off;;
  status|verify|check) do_status;;
  *) die "usage: $0 {apply|status|off}";;
esac
