#!/usr/bin/env bash
# knowledge-sync.sh <profile> <dest> — copy a profile's knowledge corpus from the
# shared knowledge root into a profile's knowledge/ folder.
#
#   Source:  $KNOWLEDGE_ROOT/<profile>/**/*.{md,txt}   (default data/knowledge)
#   Dest:    <dest>/**  (usually $HERMES_HOME/profiles/<profile>/knowledge)
#
# The knowledge root may be a local directory or an SMB/CIFS mount. Point at one
# with KNOWLEDGE_ROOT=/mnt/books/knowledge. Books stay where they are: this only
# ever COPIES, never moves or deletes the source.
#
# Modes (KNOWLEDGE_SYNC_MODE):
#   update  (default) additive copy — adds new files, refreshes changed ones,
#           and never deletes anything in the destination.
#   mirror  additive copy, then prune destination .md/.txt files that no longer
#           exist in the source. Only .md/.txt files are ever removed; unrelated
#           destination files are left untouched. Opt in when the knowledge root
#           is the single source of truth for this profile.
#
# Safety: rejects path-traversal profile names, rejects a source that resolves
# outside the knowledge root (hostile symlink), copies only regular .md/.txt
# files (symlinks and other file types are skipped), and treats a missing source
# as a warning — never a failure — so profile creation still succeeds.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

profile="${1:-}"
dest="${2:-}"
[[ -n "$profile" ]] || die "usage: knowledge-sync.sh <profile> <dest>"
[[ -n "$dest"    ]] || die "usage: knowledge-sync.sh <profile> <dest>"

# Reject anything that is not a plain profile name: no slashes, no `..`, no
# leading dash or dot. This blocks `../../etc` and absolute-path tricks.
if [[ ! "$profile" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || [[ "$profile" == *".."* ]]; then
  die "invalid profile name: '$profile' (letters, digits, dot, dash, underscore; no slashes or '..')"
fi

root="${KNOWLEDGE_ROOT:-$ROOT/data/knowledge}"
mode="${KNOWLEDGE_SYNC_MODE:-update}"
src="$root/$profile"

if [[ ! -d "$src" ]]; then
  warn "no knowledge source at $src — skipping (put .md/.txt books under \$KNOWLEDGE_ROOT/$profile to enable)"
  exit 0
fi

# Reject a source that resolves outside the knowledge root (e.g. profile dir is
# a symlink to /etc). realpath both sides and require a prefix match.
real_root="$(cd "$root" && pwd -P)"
real_src="$(cd "$src" && pwd -P)"
case "$real_src/" in
  "$real_root"/*) : ;;
  *) die "knowledge source '$src' resolves outside \$KNOWLEDGE_ROOT ($real_root) — refusing" ;;
esac

mkdir -p "$dest"
copied=0
# -type f matches only regular files (symlinks are -type l and skipped); without
# -L, find does not descend into symlinked directories, so a hostile symlink
# cannot pull in files from outside the tree. -print0 handles spaces/newlines.
while IFS= read -r -d '' file; do
  rel="${file#"$src"/}"
  mkdir -p "$dest/$(dirname "$rel")"
  cp -f "$file" "$dest/$rel"
  copied=$((copied + 1))
done < <(find "$src" -type f \( -name '*.md' -o -name '*.txt' \) -print0)

pruned=0
if [[ "$mode" == "mirror" ]]; then
  while IFS= read -r -d '' df; do
    rel="${df#"$dest"/}"
    if [[ ! -f "$src/$rel" ]]; then
      rm -f "$df"
      pruned=$((pruned + 1))
    fi
  done < <(find "$dest" -type f \( -name '*.md' -o -name '*.txt' \) -print0)
  # Drop directories left empty by pruning; keep the dest root itself.
  find "$dest" -mindepth 1 -type d -empty -delete 2>/dev/null || true
fi

if [[ "$mode" == "mirror" ]]; then
  log "knowledge: $profile — copied $copied file(s), pruned $pruned stale (mirror) from $src"
else
  log "knowledge: $profile — copied/updated $copied file(s) from $src"
fi
