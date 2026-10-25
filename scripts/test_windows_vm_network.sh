#!/usr/bin/env bash
# test_windows_vm_network.sh — behaviour guard for scripts/windows-vm-network.sh,
# the helper that publishes the host's CLIProxyAPI (:8317) and Honcho (:8000)
# over Tailscale Serve TCP forwarders while Docker stays bound to 127.0.0.1.
#
# This is a real execution test, not a source scan. It drives apply/status/off
# against STUB `tailscale`/`curl`/`sudo` binaries (injected through the script's
# TAILSCALE_BIN/CURL_BIN/SUDO_BIN overrides) and a throwaway .env, and asserts:
#   - apply issues the exact current serve syntax for both ports,
#   - apply is idempotent (skips a forward that already points at loopback),
#   - off removes only the two configured TCP listeners (no `serve reset`, no funnel),
#   - status verifies loopback endpoints AND the two serve forwards,
#   - non-loopback Docker binds in .env fail clearly,
#   - a missing or disconnected tailscale fails clearly,
#   - a loopback probe failure fails status,
#   - a `denied` serve write is retried through sudo,
#   - port env overrides are honoured.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
SCRIPT="scripts/windows-vm-network.sh"

pass=0; fail=0
ck() { if eval "$2"; then printf '  ok   %s\n' "$1"; pass=$((pass+1)); else printf '  FAIL %s\n' "$1"; fail=$((fail+1)); fi; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"

# --- stub tailscale --------------------------------------------------------
# Logs every invocation to $STUB_LOG. `status`/`status --json` reflect
# $STUB_STATE. `serve status --json` prints the file at $STUB_SERVE_JSON.
# A `serve` write is denied (exit 1, "Access denied") when $STUB_DENY=1 unless
# it arrived through the sudo stub ($STUB_VIA_SUDO=1).
cat > "$BIN/tailscale" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
if [[ "$1" == "status" ]]; then
  if [[ "${2:-}" == "--json" ]]; then printf '{"BackendState":"%s"}\n' "${STUB_STATE:-Running}"; exit 0; fi
  if [[ "${STUB_STATE:-Running}" == "Running" ]]; then echo "100.64.0.1 host linux -"; exit 0; fi
  echo "Tailscale is stopped."; exit 1
fi
if [[ "$1" == "serve" && "$2" == "status" ]]; then
  cat "${STUB_SERVE_JSON:-/dev/null}" 2>/dev/null || echo '{}'
  exit 0
fi
if [[ "$1" == "serve" ]]; then
  if [[ "${STUB_DENY:-0}" == "1" && "${STUB_VIA_SUDO:-0}" != "1" ]]; then
    echo "Access denied: serve config denied" >&2; exit 1
  fi
  exit 0
fi
exit 0
EOF
chmod +x "$BIN/tailscale"

# --- stub curl -------------------------------------------------------------
# Records probes; exits $STUB_CURL_RC (0 = reachable, 7 = connection refused).
cat > "$BIN/curl" <<'EOF'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$STUB_LOG"
exit "${STUB_CURL_RC:-0}"
EOF
chmod +x "$BIN/curl"

# --- stub sudo -------------------------------------------------------------
# Marks the invocation as elevated and execs the rest, so the tailscale stub
# can tell a sudo'd write from a bare one.
cat > "$BIN/sudo" <<'EOF'
#!/usr/bin/env bash
export STUB_VIA_SUDO=1
exec "$@"
EOF
chmod +x "$BIN/sudo"

# serve-status JSON fixtures.
present_json="$WORK/present.json"
cat > "$present_json" <<'EOF'
{"TCP":{"443":{"HTTPS":true},"8000":{"TCPForward":"127.0.0.1:8000"},"8317":{"TCPForward":"127.0.0.1:8317"}},
 "Web":{"host:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8080"}}}},
 "AllowFunnel":{"host:443":true}}
EOF
absent_json="$WORK/absent.json"
cat > "$absent_json" <<'EOF'
{"TCP":{"443":{"HTTPS":true}},
 "Web":{"host:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8080"}}}},
 "AllowFunnel":{"host:443":true}}
EOF

mkenv() { # writes a throwaway .env with the given bind + optional ports
  local f; f="$(mktemp "$WORK/env.XXXXXX")"
  {
    echo "CLIPROXY_BIND_ADDR=${1:-127.0.0.1}"
    echo "HONCHO_BIND_ADDR=${2:-127.0.0.1}"
    [[ -n "${3:-}" ]] && echo "CLIPROXY_PORT=$3"
    [[ -n "${4:-}" ]] && echo "HONCHO_PORT=$4"
  } > "$f"
  printf '%s' "$f"
}

# run ACTION SERVE_JSON [extra env assignments...]
run() {
  local action="$1" serve_json="$2"; shift 2
  local log="$WORK/log.$$.$RANDOM"; : > "$log"
  env -i PATH="$PATH" HOME="$HOME" \
      STUB_LOG="$log" STUB_SERVE_JSON="$serve_json" \
      TAILSCALE_BIN="$BIN/tailscale" CURL_BIN="$BIN/curl" SUDO_BIN="$BIN/sudo" \
      "$@" \
      bash "$SCRIPT" "$action" > "$WORK/out.txt" 2>&1
  RC=$?
  LOG_FILE="$log"
}

# ---------------------------------------------------------------------------
ck "script exists and is executable-able by bash" "[[ -f '$SCRIPT' ]]"

# apply on a clean tailnet issues the exact current serve syntax for both ports
ENVF="$(mkenv 127.0.0.1 127.0.0.1)"
run apply "$absent_json" ENV_FILE="$ENVF"
ck "apply exits 0 on a healthy host" "[[ $RC -eq 0 ]]"
ck "apply serves CLIProxyAPI :8317 with --bg --yes --tcp target" \
   "grep -qF 'serve --bg --yes --tcp=8317 tcp://127.0.0.1:8317' '$LOG_FILE'"
ck "apply serves Honcho :8000 with --bg --yes --tcp target" \
   "grep -qF 'serve --bg --yes --tcp=8000 tcp://127.0.0.1:8000' '$LOG_FILE'"
ck "apply never resets the whole serve config" "! grep -q 'serve reset' '$LOG_FILE'"
ck "apply never touches funnel" "! grep -qw 'funnel' '$LOG_FILE'"

# idempotent: forwards already pointing at loopback are left alone
run apply "$present_json" ENV_FILE="$ENVF"
ck "apply is idempotent when both forwards already exist (exit 0)" "[[ $RC -eq 0 ]]"
ck "apply does not re-issue a serve write when already present" \
   "! grep -qF 'serve --bg --yes --tcp=8317 tcp://127.0.0.1:8317' '$LOG_FILE'"

# off removes only the two configured listeners, with target omitted
run off "$present_json" ENV_FILE="$ENVF"
ck "off exits 0" "[[ $RC -eq 0 ]]"
ck "off removes :8317 with target omitted (current CLI)" \
   "grep -qF 'serve --tcp=8317 off' '$LOG_FILE'"
ck "off removes :8000 with target omitted" \
   "grep -qF 'serve --tcp=8000 off' '$LOG_FILE'"
ck "off does not pass a target to the off form" \
   "! grep -qE 'serve --tcp=[0-9]+ tcp://[^ ]+ off' '$LOG_FILE'"
ck "off never resets the whole serve config" "! grep -q 'serve reset' '$LOG_FILE'"
ck "off never touches the :443 https/funnel handler" "! grep -qE 'serve.*443' '$LOG_FILE'"

# off is a no-op for a listener that is not configured
run off "$absent_json" ENV_FILE="$ENVF"
ck "off skips ports that are not currently served (exit 0)" "[[ $RC -eq 0 ]]"
ck "off issues no serve write when nothing is configured" \
   "! grep -qE 'serve --tcp=[0-9]+ off' '$LOG_FILE'"

# status verifies loopback endpoints and both forwards
run status "$present_json" ENV_FILE="$ENVF" STUB_CURL_RC=0
ck "status passes when binds are loopback, endpoints answer, forwards exist" "[[ $RC -eq 0 ]]"
ck "status probes the loopback CLIProxyAPI port" "grep -qE 'curl .*127.0.0.1:8317' '$LOG_FILE'"

# status flags a forward that is missing
run status "$absent_json" ENV_FILE="$ENVF" STUB_CURL_RC=0
ck "status fails when a Tailscale Serve forward is missing" "[[ $RC -ne 0 ]]"

# status flags a loopback endpoint that will not answer (probe failure)
run status "$present_json" ENV_FILE="$ENVF" STUB_CURL_RC=7
ck "status fails when a loopback endpoint does not answer" "[[ $RC -ne 0 ]]"

# non-loopback Docker bind is rejected up front
ENVBAD="$(mkenv 100.73.77.34 127.0.0.1)"
run apply "$absent_json" ENV_FILE="$ENVBAD"
ck "apply fails clearly when CLIPROXY_BIND_ADDR is non-loopback" "[[ $RC -ne 0 ]]"
ck "the non-loopback failure names 127.0.0.1" "grep -qi '127.0.0.1' '$WORK/out.txt'"
ck "apply issued no serve write with a bad bind" "! grep -q 'serve --bg' '$LOG_FILE'"

ENVBAD2="$(mkenv 127.0.0.1 0.0.0.0)"
run apply "$absent_json" ENV_FILE="$ENVBAD2"
ck "apply fails clearly when HONCHO_BIND_ADDR is 0.0.0.0" "[[ $RC -ne 0 ]]"

# missing tailscale
run apply "$absent_json" ENV_FILE="$ENVF" TAILSCALE_BIN="$WORK/nope/tailscale"
ck "apply fails clearly when tailscale is not installed" "[[ $RC -ne 0 ]]"
ck "the missing-tailscale failure mentions tailscale" "grep -qi 'tailscale' '$WORK/out.txt'"

# tailscale present but not connected
run apply "$absent_json" ENV_FILE="$ENVF" STUB_STATE=Stopped
ck "apply fails clearly when tailscale is not connected" "[[ $RC -ne 0 ]]"

# a denied serve write is retried through sudo and explained
run apply "$absent_json" ENV_FILE="$ENVF" STUB_DENY=1
ck "apply succeeds by escalating a denied write through sudo" "[[ $RC -eq 0 ]]"
ck "apply still records the serve writes (via sudo)" \
   "grep -qF 'serve --bg --yes --tcp=8317 tcp://127.0.0.1:8317' '$LOG_FILE'"
ck "apply explains that sudo/operator is needed" "grep -qiE 'sudo|operator' '$WORK/out.txt'"

# port env overrides
ENVP="$(mkenv 127.0.0.1 127.0.0.1 9317 9000)"
run apply "$absent_json" ENV_FILE="$ENVP"
ck "apply honours CLIPROXY_PORT override" \
   "grep -qF 'serve --bg --yes --tcp=9317 tcp://127.0.0.1:9317' '$LOG_FILE'"
ck "apply honours HONCHO_PORT override" \
   "grep -qF 'serve --bg --yes --tcp=9000 tcp://127.0.0.1:9000' '$LOG_FILE'"

# never prints anything that could be a secret: the script must not read the key
ck "script does not read or echo CLIPROXY_API_KEY" "! grep -q 'CLIPROXY_API_KEY' '$SCRIPT'"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
