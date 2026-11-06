#!/usr/bin/env bash
# test_hermes_profile.sh — end-to-end check of scripts/hermes-profile.sh against a
# stub `hermes` binary, a temp HERMES_HOME and a temp KNOWLEDGE_ROOT. Proves that
# `make hermes-profile NAME=strategy KNOWLEDGE_ROOT=<temp>` installs the SOUL and
# syncs the profile's knowledge corpus without needing a real Hermes install.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
ok()  { echo "  ok   $*"; }
bad() { echo "  FAIL $*"; fail=1; }

# Drop inherited settings hermes-profile.sh reads. Run from a Hermes session,
# an exported HERMES_HOME/HERMES_BIN would beat the temp env file and point the
# run at the operator's real profiles.
unset HERMES_HOME HERMES_BIN PROFILES_DIR KNOWLEDGE_SYNC_MODE
while IFS= read -r _var; do unset "$_var"; done < <(compgen -v PROFILE_ || true)
unset _var

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
home="$tmp/home"; mkdir -p "$home/.local/bin"

# Stub hermes: create the profile dir on `profile create`, no-op otherwise.
cat > "$home/.local/bin/hermes" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-} ${2:-}" == "profile create" ]]; then mkdir -p "$HERMES_HOME/profiles/${3:?}"; fi
echo "$*" >> "$HERMES_HOME/hermes-calls.log"
exit 0
STUB
chmod +x "$home/.local/bin/hermes"

# Temp dotenv with the keys hermes-profile.sh reads, and HERMES_HOME in temp.
envf="$tmp/env"
{ echo "CLIPROXY_API_KEY=test-key"; echo "HERMES_HOME=$home/.hermes"; } > "$envf"

# Temp knowledge root with a strategy corpus (nested + spaces).
kroot="$tmp/knowledge"; mkdir -p "$kroot/strategy/part one"
printf 'good strategy\n' > "$kroot/strategy/good-strategy.md"
printf 'nested\n'        > "$kroot/strategy/part one/notes.txt"
printf '%%PDF\n'         > "$kroot/strategy/scan.pdf"

HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" \
  bash "$ROOT/scripts/hermes-profile.sh" strategy >/dev/null 2>&1 || bad "hermes-profile.sh exited non-zero"

pdir="$home/.hermes/profiles/strategy"
[[ -f "$pdir/SOUL.md" ]]                        && ok "SOUL.md installed"                  || bad "SOUL.md missing"
grep -q 'strategy' "$pdir/SOUL.md" 2>/dev/null  && ok "SOUL.md is the strategy prompt"     || bad "SOUL.md content wrong"
[[ -f "$pdir/knowledge/good-strategy.md" ]]     && ok "knowledge .md synced"               || bad "knowledge .md missing"
[[ -f "$pdir/knowledge/part one/notes.txt" ]]   && ok "nested knowledge with spaces synced" || bad "nested knowledge missing"
[[ ! -e "$pdir/knowledge/scan.pdf" ]]           && ok "non-text book ignored"              || bad ".pdf leaked into knowledge"
[[ -f "$pdir/.env" ]]                           && ok "profile .env written"               || bad "profile .env missing"

# Rerun is idempotent and still succeeds (profile already exists path).
HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" \
  bash "$ROOT/scripts/hermes-profile.sh" strategy >/dev/null 2>&1 && ok "rerun succeeds" || bad "rerun failed"

# Missing knowledge source must warn, not fail profile creation.
out2="$(HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$tmp/empty" \
  bash "$ROOT/scripts/hermes-profile.sh" strategy 2>&1)" && ok "missing corpus is non-fatal" || bad "missing corpus failed the run"
grep -qi 'no knowledge source' <<<"$out2" && ok "missing corpus warns clearly" || bad "no clear warning for missing corpus"

# No SSH env: the terminal backend is left alone.
! grep -q 'config set terminal' "$home/.hermes/hermes-calls.log" && ok "no SSH env, no terminal change" || bad "terminal changed without PROFILE_SSH_HOST"

calls="$home/.hermes/hermes-calls.log"
! grep -q 'peer add' "$calls" && ok "no peer env, no peer registered" || bad "peer registered without PROFILE_PEER_URL"

# Worker lane relay: PROFILE_PEER_* registers the worker's own Hermes as the peer.
peer_key="peer-key-$RANDOM-do-not-log"
HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" PROFILE_PEER_URL=http://mac.tailnet:8642 \
  PROFILE_PEER_KEY="$peer_key" bash "$ROOT/scripts/hermes-profile.sh" mac-worker >"$tmp/peer.out" 2>&1 \
  && ok "mac-worker relay profile created" || bad "mac-worker relay failed"
menv="$home/.hermes/profiles/mac-worker/.env"
grep -qx -- '-p mac-worker peer add mac --url http://mac.tailnet:8642 --note kanban-workflow lane mac-worker' "$calls" \
  && ok "peer 'mac' registered on the lane profile" || bad "peer not registered"
grep -qx "HERMES_PEER_MAC_KEY=$peer_key" "$menv"                         && ok "peer key in the profile .env" || bad "peer key missing from .env"
[[ "$(stat -c %a "$menv")" == 600 ]]                                    && ok "profile .env is 0600"          || bad "profile .env mode"
! grep -q -- "$peer_key" "$calls" "$tmp/peer.out"                       && ok "peer key never in argv or output" || bad "peer key leaked"
grep -qx -- '-p mac-worker config set terminal.backend local' "$calls"  && ok "relay terminal is local"       || bad "relay backend not local"
HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" PROFILE_PEER_URL=http://win.tailnet:8642 PROFILE_PEER_KEY=k2 \
  bash "$ROOT/scripts/hermes-profile.sh" windows-worker >/dev/null 2>&1
grep -q -- '-p windows-worker peer add windows ' "$calls" && grep -qx 'HERMES_PEER_WINDOWS_KEY=k2' "$home/.hermes/profiles/windows-worker/.env" \
  && ok "windows-worker gets peer 'windows'" || bad "windows peer default wrong"
HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" PROFILE_PEER_URL=http://mac.tailnet:8642 \
  bash "$ROOT/scripts/hermes-profile.sh" mac-worker >/dev/null 2>&1 && bad "peer URL without key accepted" || ok "peer URL without key refused"
HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" PROFILE_PEER_URL=http://m:8642 PROFILE_PEER_KEY=k \
  PROFILE_SSH_HOST=mac.tailnet PROFILE_SSH_USER=hermes bash "$ROOT/scripts/hermes-profile.sh" mac-worker >/dev/null 2>&1 \
  && bad "peer and SSH together accepted" || ok "peer and SSH together refused"

# Optional SSH administration: PROFILE_SSH_* switches a profile's terminal to SSH.
HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" PROFILE_SSH_HOST=box.tailnet PROFILE_SSH_USER=hermes \
  PROFILE_SSH_KEY=/k/id_ed25519 bash "$ROOT/scripts/hermes-profile.sh" coder >/dev/null 2>&1 \
  && ok "SSH profile created" || bad "SSH profile failed"
grep -qx -- '-p coder config set terminal.backend ssh' "$calls"          && ok "backend set to ssh"   || bad "backend not set"
grep -qx -- '-p coder config set terminal.ssh_host box.tailnet' "$calls" && ok "ssh_host set"         || bad "ssh_host not set"
grep -qx -- '-p coder config set terminal.cwd ~' "$calls"                && ok "cwd defaults to ~"    || bad "cwd not set"
grep -qx -- '-p coder config set terminal.ssh_key /k/id_ed25519' "$calls" && ok "ssh_key set"         || bad "ssh_key not set"
! grep -q -- 'terminal.ssh_port' "$calls"                                && ok "unset port left default" || bad "port set without PROFILE_SSH_PORT"
HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" PROFILE_SSH_HOST=box.tailnet \
  bash "$ROOT/scripts/hermes-profile.sh" coder >/dev/null 2>&1 && bad "SSH host without user accepted" || ok "SSH host without user refused"

echo
[[ "$fail" -eq 0 ]] && echo "PASS: hermes-profile" || echo "FAIL: hermes-profile"
exit "$fail"
