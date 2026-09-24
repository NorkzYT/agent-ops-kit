#!/usr/bin/env bash
# test_claude_cli_update.sh — exercise the real `make claude-cli-update` target
# offline with a stub claude in $HOME/.local/bin (never touches the real CLI).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAKE=(make -C "$ROOT" --no-print-directory)

pass=0; fail=0
ok() { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no() { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
stubbin="$tmp/.local/bin"; mkdir -p "$stubbin"; arglog="$tmp/args.log"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> %q\n' "$arglog" > "$stubbin/claude"
chmod +x "$stubbin/claude"

# --- help lists the target, distinct from claude-proxy-update -------------
h="$("${MAKE[@]}" help 2>&1)"
grep -q "claude-cli-update" <<<"$h" && grep -q "claude-proxy-update" <<<"$h" \
  && ok "help lists claude-cli-update and claude-proxy-update" || no "help missing claude-cli-update"

# --- runs `claude update` then `claude --version` from ~/.local/bin --------
out="$(HOME="$tmp" PATH="/usr/bin:/bin" "${MAKE[@]}" claude-cli-update 2>&1)"; rc=$?
[[ $rc -eq 0 && "$(cat "$arglog" 2>/dev/null)" == $'update\n--version' ]] \
  && ok "claude-cli-update: runs claude update, then --version (~/.local/bin on PATH)" \
  || no "claude-cli-update (rc=$rc, args=$(tr '\n' '|' < "$arglog" 2>/dev/null), out=$out)"

# --- no claude anywhere -> clear error, non-zero ---------------------------
out="$(HOME="$tmp/nohome" PATH="/usr/bin:/bin" "${MAKE[@]}" claude-cli-update 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && grep -q "claude CLI not found" <<<"$out" \
  && ok "claude-cli-update: missing CLI -> clear error, non-zero" || no "missing CLI (rc=$rc, out=$out)"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
