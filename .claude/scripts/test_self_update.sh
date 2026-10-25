#!/usr/bin/env bash
# test_self_update.sh — E2E functional tests for self-update.sh.
# Covers the no-manifest guidance (must print a runnable command, never
# <owner>/<repo> placeholders) and the manifest replay path, offline via a
# stub curl that serves a fake installer. Exit non-zero on any fail.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SU="$SCRIPT_DIR/self-update.sh"
[ -f "$SU" ] || { echo "FATAL: $SU not found"; exit 1; }

PASS=0 FAIL=0
ok() { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
no() { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else no "$1   [cond: $2]"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== no manifest: prints a runnable installer command =="
ROOT1="$TMP/bare-install"
mkdir -p "$ROOT1/.claude"
set +e
OUT1="$(bash "$SU" "$ROOT1" 2>&1)"
RC1=$?
set -e
assert "exits non-zero"                      '[ "$RC1" -ne 0 ]'
assert "mentions the missing manifest"       'grep -q "no manifest" <<<"$OUT1"'
assert "suggests a curl installer command"   'grep -q "curl -fsSL https://raw.githubusercontent.com/NorkzYT/agent-ops-kit/main/install.sh" <<<"$OUT1"'
assert "fills in --repo with a real repo"    'grep -q -- "--repo NorkzYT/agent-ops-kit" <<<"$OUT1"'
assert "fills in --dest with this root"      'grep -q -- "--dest $ROOT1" <<<"$OUT1"'
assert "no <owner>/<repo> placeholders"      '! grep -q "<owner>" <<<"$OUT1"'
assert "no legacy stack flags suggested"     '! grep -q -- "--with-" <<<"$OUT1"'

echo "== no manifest: canonical repo overridable for forks =="
set +e
OUT3="$(CCA_CANONICAL_REPO=someone/forked-kit bash "$SU" "$ROOT1" 2>&1)"
set -e
assert "override changes suggested repo"     'grep -q -- "--repo someone/forked-kit" <<<"$OUT3"'

echo "== no manifest: CCA_CANONICAL_REPO read from install-root .env =="
printf 'CCA_CANONICAL_REPO=envfile/fork-kit\n' >"$ROOT1/.env"
set +e
OUT3A="$(bash "$SU" "$ROOT1" 2>&1)"
OUT3B="$(CCA_CANONICAL_REPO=process/wins bash "$SU" "$ROOT1" 2>&1)"
OUT3C="$(CCA_CANONICAL_REPO= bash "$SU" "$ROOT1" 2>&1)"
set -e
assert ".env value used for suggested repo"  'grep -q -- "--repo envfile/fork-kit" <<<"$OUT3A"'
assert "env var takes precedence over .env"  'grep -q -- "--repo process/wins" <<<"$OUT3B"'
assert "empty env var falls back to .env"    'grep -q -- "--repo envfile/fork-kit" <<<"$OUT3C"'
rm -f "$ROOT1/.env"

echo "== lib.sh: env_file_get parses dotenv files =="
LIB="$SCRIPT_DIR/lib.sh"
DOTENV="$TMP/dotenv-fixture"
cat >"$DOTENV" <<'EOF'
# comment line
PLAIN=alpha
export EXPORTED=bravo
DQUOTED="charlie delta"
SQUOTED='echo foxtrot'
LAST=first
LAST=second
EOF
printf 'CRLF=golf\r\n' >>"$DOTENV"
lib_get() { bash -c ". '$LIB'; env_file_get \"\$1\" \"\$2\"" _ "$1" "$DOTENV"; }
assert "plain KEY=value"                     '[ "$(lib_get PLAIN)" = "alpha" ]'
assert "export prefix stripped"              '[ "$(lib_get EXPORTED)" = "bravo" ]'
assert "double quotes stripped"              '[ "$(lib_get DQUOTED)" = "charlie delta" ]'
assert "single quotes stripped"              '[ "$(lib_get SQUOTED)" = "echo foxtrot" ]'
assert "last assignment wins"                '[ "$(lib_get LAST)" = "second" ]'
assert "CRLF line ending stripped"           '[ "$(lib_get CRLF)" = "golf" ]'
assert "missing var returns non-zero"        '! lib_get NO_SUCH_VAR'
assert "missing file returns non-zero"       '! bash -c ". \"$LIB\"; env_file_get X /nonexistent-dotenv"'

echo "== manifest replay: re-runs installer with recorded flags =="
ROOT4="$TMP/recorded-install"
mkdir -p "$ROOT4/.claude"
cat >"$ROOT4/.claude/install.manifest" <<'EOF'
# agent-ops-kit install manifest (shell-sourceable).
CCA_REPO='NorkzYT/agent-ops-kit'
CCA_REF='main'
CCA_DEST='/tmp/somewhere'
CCA_BOOTSTRAP_LINUX='1'
CCA_NO_EXTRAS='0'
EOF
# Stub curl: serve a fake installer that just echoes its argv, so the replay
# is observable without touching the network.
BIN="$TMP/bin"
mkdir -p "$BIN"
cat >"$BIN/curl" <<'EOF'
#!/usr/bin/env bash
out=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    *) shift;;
  esac
done
[ -n "$out" ] || exit 1
printf '#!/usr/bin/env bash\nprintf "FAKE-INSTALLER:%%s\\n" "$*"\n' >"$out"
EOF
chmod +x "$BIN/curl"
set +e
OUT4="$(PATH="$BIN:$PATH" bash "$SU" "$ROOT4" 2>&1)"
RC4=$?
set -e
assert "replay exits zero"                   '[ "$RC4" -eq 0 ]'
assert "replays --repo from manifest"        'grep -q -- "--repo NorkzYT/agent-ops-kit" <<<"$OUT4"'
assert "replays --ref from manifest"         'grep -q -- "--ref main" <<<"$OUT4"'
assert "replays recorded --dest"             'grep -q -- "--dest /tmp/somewhere" <<<"$OUT4"'
assert "always passes --force"               'grep -q -- "--force" <<<"$OUT4"'
assert "replays --bootstrap-linux"           'grep -q -- "--bootstrap-linux" <<<"$OUT4"'
assert "omits flags recorded as 0"           '! grep -q -- "--no-extras" <<<"$OUT4"'
assert "installer actually executed"         'grep -q "FAKE-INSTALLER:" <<<"$OUT4"'

echo "== manifest replay: empty CCA_DEST falls back to install root =="
ROOT5="$TMP/no-dest-install"
mkdir -p "$ROOT5/.claude"
sed "s|^CCA_DEST=.*|CCA_DEST=''|" "$ROOT4/.claude/install.manifest" >"$ROOT5/.claude/install.manifest"
set +e
OUT5="$(PATH="$BIN:$PATH" bash "$SU" "$ROOT5" 2>&1)"
set -e
assert "empty dest replaced by root"         'grep -q -- "--dest $ROOT5" <<<"$OUT5"'

echo "== malformed manifest: missing CCA_REPO is rejected =="
ROOT6="$TMP/broken-install"
mkdir -p "$ROOT6/.claude"
printf "CCA_REF='main'\n" >"$ROOT6/.claude/install.manifest"
set +e
OUT6="$(bash "$SU" "$ROOT6" 2>&1)"
RC6=$?
set -e
assert "missing CCA_REPO exits non-zero"     '[ "$RC6" -ne 0 ]'
assert "missing CCA_REPO names the problem"  'grep -q "missing CCA_REPO" <<<"$OUT6"'

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
