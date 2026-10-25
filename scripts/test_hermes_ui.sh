#!/usr/bin/env bash
# test_hermes_ui.sh — exercise scripts/hermes-ui.sh with a stub `hermes` binary.
# Covers: missing binary, start args, custom port, status, stop, port validation
# and shell-injection attempts. No real hermes, no network.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI="$ROOT/scripts/hermes-ui.sh"

pass=0; fail=0
ok()   { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no()   { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
# Stub hermes lives in $tmp/.local/bin, which hermes-ui.sh prepends to PATH.
stubbin="$tmp/.local/bin"; mkdir -p "$stubbin"
arglog="$tmp/args.log"
cat > "$stubbin/hermes" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$arglog"
exit 0
EOF
chmod +x "$stubbin/hermes"

# empty HOME (no hermes anywhere) for the missing-binary case
nohome="$tmp/nohermes"; mkdir -p "$nohome"

# run_with HOME EXTRA_ENV... -- args : invoke the UI with a minimal PATH so only
# the stub (via $HOME/.local/bin) can satisfy `have hermes`.
reset_log() { : > "$arglog"; }

# --- missing binary -------------------------------------------------------
out="$(HOME="$nohome" PATH="/usr/bin:/bin" bash "$UI" start 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && grep -q "hermes not installed" <<<"$out" \
  && ok "missing hermes -> clear error, non-zero exit" \
  || no "missing hermes (rc=$rc, out=$out)"

# --- start with defaults --------------------------------------------------
reset_log
HOME="$tmp" PATH="/usr/bin:/bin" bash "$UI" start >/dev/null 2>&1; rc=$?
line="$(cat "$arglog" 2>/dev/null)"
[[ $rc -eq 0 ]] && [[ "$line" == "dashboard --host 127.0.0.1 --port 9119 --no-open" ]] \
  && ok "start binds 127.0.0.1 port 9119 --no-open" \
  || no "start default args (rc=$rc, args=$line)"

# --- start with custom HERMES_UI_PORT ------------------------------------
reset_log
HOME="$tmp" PATH="/usr/bin:/bin" HERMES_UI_PORT=9200 bash "$UI" start >/dev/null 2>&1
line="$(cat "$arglog" 2>/dev/null)"
[[ "$line" == "dashboard --host 127.0.0.1 --port 9200 --no-open" ]] \
  && ok "HERMES_UI_PORT respected" || no "custom port (args=$line)"

# --- status / stop --------------------------------------------------------
reset_log; HOME="$tmp" PATH="/usr/bin:/bin" bash "$UI" status >/dev/null 2>&1
[[ "$(cat "$arglog")" == "dashboard --status" ]] && ok "status -> dashboard --status" || no "status ($(cat "$arglog"))"
reset_log; HOME="$tmp" PATH="/usr/bin:/bin" bash "$UI" stop >/dev/null 2>&1
[[ "$(cat "$arglog")" == "dashboard --stop" ]] && ok "stop -> dashboard --stop" || no "stop ($(cat "$arglog"))"

# --- port validation & injection: must be rejected, hermes never called ---
for bad in "abc" "99999" "0" "9119 --host 0.0.0.0" '9119; touch pwned' '$(touch pwned)' "-1" "80:80"; do
  reset_log; rm -f "$ROOT/pwned"
  out="$(HOME="$tmp" PATH="/usr/bin:/bin" HERMES_UI_PORT="$bad" bash "$UI" start 2>&1)"; rc=$?
  if [[ $rc -ne 0 && ! -s "$arglog" && ! -e "$ROOT/pwned" ]]; then
    ok "rejected HERMES_UI_PORT='$bad' (no hermes call, no injection)"
  else
    no "HERMES_UI_PORT='$bad' not safely rejected (rc=$rc, args=$(cat "$arglog"), pwned=$( [[ -e "$ROOT/pwned" ]] && echo yes || echo no ))"
  fi
done
rm -f "$ROOT/pwned"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
