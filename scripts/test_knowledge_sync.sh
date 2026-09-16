#!/usr/bin/env bash
# test_knowledge_sync.sh — behaviour of scripts/knowledge-sync.sh with temp dirs.
# Covers: nested copies, spaces, updates, unrelated-dest preservation, ignored
# non-text files, profile-name/traversal rejection, symlink escape rejection,
# missing-source warning (non-fatal), rerun idempotency and opt-in mirror prune.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYNC="$ROOT/scripts/knowledge-sync.sh"

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
fail=0
ok()   { echo "  ok   $*"; }
bad()  { echo "  FAIL $*"; fail=1; }

root="$tmp/knowledge"
mkdir -p "$root/strategy/chapters" "$root/strategy/notes with spaces"
printf 'top\n'    > "$root/strategy/book.md"
printf 'nested\n' > "$root/strategy/chapters/ch1.md"
printf 'plain\n'  > "$root/strategy/notes.txt"
printf 'spaced\n' > "$root/strategy/notes with spaces/a note.md"
printf '%%PDF\n'  > "$root/strategy/scan.pdf"          # non-text, must be ignored
printf 'binary\n' > "$root/strategy/cover.jpg"         # non-text, must be ignored

# --- nested copy, spaces, file-type filtering -------------------------------
dest="$tmp/dest1"
KNOWLEDGE_ROOT="$root" bash "$SYNC" strategy "$dest" >/dev/null 2>&1 || bad "sync exited non-zero"
[[ -f "$dest/book.md" ]]                       && ok "top-level .md copied"        || bad "top-level .md missing"
[[ -f "$dest/chapters/ch1.md" ]]               && ok "nested .md copied"           || bad "nested .md missing"
[[ -f "$dest/notes.txt" ]]                     && ok ".txt copied"                 || bad ".txt missing"
[[ -f "$dest/notes with spaces/a note.md" ]]   && ok "paths with spaces copied"    || bad "spaced path missing"
[[ ! -e "$dest/scan.pdf" ]]                    && ok ".pdf ignored"                || bad ".pdf leaked into dest"
[[ ! -e "$dest/cover.jpg" ]]                   && ok ".jpg ignored"                || bad ".jpg leaked into dest"

# --- update refreshes changed content ---------------------------------------
printf 'top v2\n' > "$root/strategy/book.md"
KNOWLEDGE_ROOT="$root" bash "$SYNC" strategy "$dest" >/dev/null 2>&1
grep -q 'top v2' "$dest/book.md" && ok "changed file refreshed" || bad "update did not refresh"

# --- unrelated destination files are preserved (additive default) -----------
printf 'user\n' > "$dest/user-added.md"
printf 'keep\n' > "$dest/local-only.txt"
KNOWLEDGE_ROOT="$root" bash "$SYNC" strategy "$dest" >/dev/null 2>&1
[[ -f "$dest/user-added.md" && -f "$dest/local-only.txt" ]] && ok "unrelated dest files preserved" || bad "additive sync deleted unrelated files"

# --- rerun idempotency (same file set, no error) ----------------------------
before="$(cd "$dest" && find . -type f | sort)"
KNOWLEDGE_ROOT="$root" bash "$SYNC" strategy "$dest" >/dev/null 2>&1
after="$(cd "$dest" && find . -type f | sort)"
[[ "$before" == "$after" ]] && ok "rerun idempotent" || bad "rerun changed the file set"

# --- missing source: warn, exit 0, do not fail ------------------------------
dest2="$tmp/dest2"
if KNOWLEDGE_ROOT="$root" bash "$SYNC" no-such-profile "$dest2" >/dev/null 2>&1; then
  ok "missing source exits 0 (non-fatal)"
else
  bad "missing source failed instead of warning"
fi

# --- profile-name / traversal rejection -------------------------------------
for bad_name in "../etc" "a/b" ".." "-x"; do
  if KNOWLEDGE_ROOT="$root" bash "$SYNC" "$bad_name" "$tmp/destx" >/dev/null 2>&1; then
    bad "accepted invalid profile name '$bad_name'"
  else
    ok "rejected invalid profile name '$bad_name'"
  fi
done

# --- symlink escape: profile dir points outside the root --------------------
secret="$tmp/secret"; mkdir -p "$secret"; printf 'sekret\n' > "$secret/leak.md"
ln -s "$secret" "$root/escape"
dest3="$tmp/dest3"
if KNOWLEDGE_ROOT="$root" bash "$SYNC" escape "$dest3" >/dev/null 2>&1; then
  bad "followed symlink escaping the knowledge root"
else
  ok "rejected symlink escaping the knowledge root"
fi
[[ ! -e "$dest3/leak.md" ]] && ok "no file copied from outside the root" || bad "leaked file from outside the root"

# --- symlinked file inside the tree is skipped (only regular files copied) ---
ln -s "$secret/leak.md" "$root/strategy/link-to-secret.md"
dest4="$tmp/dest4"
KNOWLEDGE_ROOT="$root" bash "$SYNC" strategy "$dest4" >/dev/null 2>&1
[[ ! -e "$dest4/link-to-secret.md" ]] && ok "symlinked file inside tree skipped" || bad "copied a symlinked file"

# --- opt-in mirror prunes stale knowledge, keeps unrelated files ------------
destm="$tmp/destm"
KNOWLEDGE_ROOT="$root" bash "$SYNC" strategy "$destm" >/dev/null 2>&1
printf 'stale\n' > "$destm/removed-from-source.md"   # a knowledge file with no source
printf 'keep\n'  > "$destm/notes-local.txt.keep"     # unrelated non-knowledge file
printf 'k\n'     > "$destm/handmade.log"             # unrelated non-md/txt file
KNOWLEDGE_SYNC_MODE=mirror KNOWLEDGE_ROOT="$root" bash "$SYNC" strategy "$destm" >/dev/null 2>&1
[[ ! -e "$destm/removed-from-source.md" ]] && ok "mirror pruned stale knowledge file" || bad "mirror kept stale knowledge file"
[[ -f "$destm/book.md" ]]                  && ok "mirror kept live knowledge file"   || bad "mirror removed a live file"
[[ -f "$destm/handmade.log" ]]             && ok "mirror kept unrelated non-text file" || bad "mirror deleted unrelated file"

echo
[[ "$fail" -eq 0 ]] && echo "PASS: knowledge-sync" || echo "FAIL: knowledge-sync"
exit "$fail"
