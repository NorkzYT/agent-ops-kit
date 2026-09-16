#!/usr/bin/env bash
set -euo pipefail

# Regression test for ../../install.sh — offline & hermetic.
#
# Covers:
#   F5  — user-level ~/.claude/CLAUDE.md is preserved via a MANAGED BLOCK
#         (no truncation of pre-existing user content; idempotent).
#   F6  — .claude/logs dir is created with private mode 700.
#   Idempotency — re-running the installer never duplicates the CLAUDE.md
#         managed block nor the .gitignore managed block.
#
# The installer is pointed at a local file:// tarball via CCA_TARBALL_URL so
# no network access is required.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- Build a minimal fake source tree (mirrors GitHub archive nesting) ---
SRC="$WORK/repo-main"
mkdir -p "$SRC/.claude/scripts" "$SRC/.claude/bin" "$SRC/.vscode"

cat > "$SRC/.claude/CLAUDE.md" <<'EOF'
# Fake bundle constitution (test fixture)
EOF
cat > "$SRC/.vscode/settings.json" <<'EOF'
{ "editor.formatOnSave": true }
EOF
# Placeholder wrappers the installer chmods (their absence is tolerated too).
: > "$SRC/.claude/bin/codex-local"
: > "$SRC/.claude/bin/wt"

# Pack as <tmp>/repo.tgz with a top-level dir like GitHub's <repo>-<ref>/.
TGZ="$WORK/repo.tgz"
tar -czf "$TGZ" -C "$WORK" repo-main

# --- Isolated HOME + DEST under the temp dir ---
TMPHOME="$WORK/home"
DEST="$WORK/dest"
mkdir -p "$TMPHOME/.claude" "$DEST"

# sudo shim early on PATH: just exec the command, so the /usr/local/bin editor
# copy can't hang on a password prompt (it may fail permission and warn — fine).
SHIMDIR="$WORK/shim"
mkdir -p "$SHIMDIR"
cat > "$SHIMDIR/sudo" <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF
chmod +x "$SHIMDIR/sudo"

# Pre-create the user's global CLAUDE.md with distinctive content.
PERSONAL="MY PERSONAL RULES - DO NOT DELETE"
cat > "$TMPHOME/.claude/CLAUDE.md" <<EOF
$PERSONAL
Keep this file intact.
EOF

run_install() {
  if ! CCA_TARBALL_URL="file://$TGZ" \
       HOME="$TMPHOME" \
       PATH="$SHIMDIR:$PATH" \
       bash "$INSTALL_SH" --repo x/y --ref main --dest "$DEST" --no-extras --force \
       > "$WORK/run.log" 2>&1; then
    echo "----- installer output -----" >&2
    cat "$WORK/run.log" >&2
    fail "installer exited non-zero"
  fi
}

USER_MD="$TMPHOME/.claude/CLAUDE.md"
START="# >>> agent-ops-kit managed block >>>"
GI_START="# >>> agent-ops-kit local agent state >>>"

echo "== First install =="
run_install

# (1) user content preserved
grep -qF "$PERSONAL" "$USER_MD" || fail "personal content lost after first install"
# (2) managed block markers + policy now present
grep -qF "$START" "$USER_MD" || fail "managed block start marker missing"
grep -qF "# <<< agent-ops-kit managed block <<<" "$USER_MD" || fail "managed block end marker missing"
grep -qF "Cost-optimized routing policy" "$USER_MD" || fail "policy body missing"

echo "== Second install (idempotency) =="
run_install

# (3) exactly one managed block, personal content still present
count_start="$(grep -cF "$START" "$USER_MD" || true)"
[[ "$count_start" -eq 1 ]] || fail "expected exactly 1 CLAUDE.md managed block, got $count_start"
grep -qF "$PERSONAL" "$USER_MD" || fail "personal content lost after second install"

# (4) logs dir mode 700
LOGS="$DEST/.claude/logs"
[[ -d "$LOGS" ]] || fail "logs dir missing: $LOGS"
mode="$(stat -c '%a' "$LOGS")"
[[ "$mode" == "700" ]] || fail "logs dir mode expected 700, got $mode"

# (5) .gitignore managed block appears exactly once after two runs
GI="$DEST/.gitignore"
[[ -f "$GI" ]] || fail ".gitignore not created: $GI"
gi_count="$(grep -cF "$GI_START" "$GI" || true)"
[[ "$gi_count" -eq 1 ]] || fail "expected exactly 1 .gitignore managed block, got $gi_count"

echo "PASS: install.sh regression tests (F5, F6, idempotency)"
