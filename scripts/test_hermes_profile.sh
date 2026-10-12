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

# Worker lane: PROFILE_SSH_* switches the profile's terminal to SSH.
HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" PROFILE_SSH_HOST=mac.tailnet PROFILE_SSH_USER=hermes \
  PROFILE_SSH_KEY=/k/id_ed25519 bash "$ROOT/scripts/hermes-profile.sh" mac-worker >/dev/null 2>&1 \
  && ok "mac-worker lane profile created" || bad "mac-worker lane failed"
calls="$home/.hermes/hermes-calls.log"
grep -qx -- '-p mac-worker config set terminal.backend ssh' "$calls"       && ok "lane backend set to ssh"   || bad "lane backend not set"
grep -qx -- '-p mac-worker config set terminal.ssh_host mac.tailnet' "$calls" && ok "lane ssh_host set"      || bad "lane ssh_host not set"
grep -qx -- '-p mac-worker config set terminal.cwd ~' "$calls"             && ok "lane cwd defaults to ~"    || bad "lane cwd not set"
grep -qx -- '-p mac-worker config set terminal.ssh_key /k/id_ed25519' "$calls" && ok "lane ssh_key set"      || bad "lane ssh_key not set"
! grep -q -- 'terminal.ssh_port' "$calls"                                   && ok "unset port left default"  || bad "port set without PROFILE_SSH_PORT"
HOME="$home" ENV_FILE="$envf" KNOWLEDGE_ROOT="$kroot" PROFILE_SSH_HOST=mac.tailnet \
  bash "$ROOT/scripts/hermes-profile.sh" mac-worker >/dev/null 2>&1 && bad "SSH host without user accepted" || ok "SSH host without user refused"

echo
[[ "$fail" -eq 0 ]] && echo "PASS: hermes-profile" || echo "FAIL: hermes-profile"
exit "$fail"
