#!/usr/bin/env bash
# knowledge-stage.sh <profile> — build a profile's knowledge corpus from its book
# list, hermes/profiles/<profile>/knowledge-sources.txt.
#
#   Books:   $BOOKS_ROOT/<collection>/<path>        (default /mnt/books, read-only)
#   Output:  $KNOWLEDGE_ROOT/<profile>/<path>       (default data/knowledge)
#
# Each manifest line is a folder or file relative to BOOKS_ROOT, for example
# `edu/Meditations - Marcus Aurelius`. Blank lines and `#` comments are ignored.
#   .md/.txt   copied as-is
#   .epub/.pdf converted to Markdown by the document-converter skill (doc2md.py)
# Anything else (covers, .opf, .db) is skipped. `make hermes-profile NAME=<p>`
# then syncs $KNOWLEDGE_ROOT/<p> into the profile, and `/learn` indexes it.
#
# Env: BOOKS_ROOT          book library root (default /mnt/books)
#      KNOWLEDGE_ROOT      output root (default data/knowledge, gitignored)
#      KNOWLEDGE_CONVERT   1 (default) converts EPUB/PDF; 0 copies text only
#      DOC2MD              converter path (default the document-converter skill)
#      KNOWLEDGE_MANIFEST  book list override (tests)
#      PROFILES_DIR        profile seeds (default hermes/profiles)
#      KNOWLEDGE_ASSIGNMENTS  override of <profile>/knowledge-assignments.json
#
# Canonical books: a manifest entry that hermes/profiles/<profile>/
# knowledge-assignments.json lists as shared (written by knowledge-apply) is
# NOT copied here; the profile reads it through refs/<name>.ref.md pointing at
# the one canonical copy under $KNOWLEDGE_CANONICAL. Such entries are counted
# and skipped so a refresh never duplicates the book text.
#
# Safety: copy-only, never writes under BOOKS_ROOT. Rejects bad profile names,
# absolute or `..` manifest paths, symlinked entries, entries that resolve
# outside BOOKS_ROOT, and an output root that overlaps the library. Exit 0 when
# everything staged, 3 when a book could not be converted, 1 on a usage or
# safety error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

profile="${1:-}"
[[ -n "$profile" ]] || die "usage: knowledge-stage.sh <profile>   (reads hermes/profiles/<profile>/knowledge-sources.txt)"
if [[ ! "$profile" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || [[ "$profile" == *".."* ]]; then
  die "invalid profile name: '$profile'"
fi
pdir_root="${PROFILES_DIR:-$ROOT/hermes/profiles}"
manifest="${KNOWLEDGE_MANIFEST:-$pdir_root/$profile/knowledge-sources.txt}"
[[ -f "$manifest" ]] || die "no book list at $manifest"
assignments="${KNOWLEDGE_ASSIGNMENTS:-$pdir_root/$profile/knowledge-assignments.json}"
declare -A canonical=()
if [[ -f "$assignments" ]]; then
  canon_list="$(python3 "$ROOT/scripts/knowledge_plan.py" canonical-entries "$assignments")" \
    || die "cannot read canonical assignments from $assignments"
  while IFS= read -r e; do [[ -n "$e" ]] && canonical["$e"]=1; done <<< "$canon_list"
fi

books="${BOOKS_ROOT:-/mnt/books}"
kroot="${KNOWLEDGE_ROOT:-$ROOT/data/knowledge}"
convert="${KNOWLEDGE_CONVERT:-1}"
doc2md="${DOC2MD:-$ROOT/hermes/skills/document-converter/scripts/doc2md.py}"

[[ -d "$books" ]] || die "book library not found at $books (set BOOKS_ROOT)"
real_books="$(cd "$books" && pwd -P)"
mkdir -p "$kroot/$profile"
real_out="$(cd "$kroot/$profile" && pwd -P)"
case "$real_out/" in "$real_books"/*) die "output $real_out is inside the book library — refusing" ;; esac
case "$real_books/" in "$real_out"/*) die "book library is inside the output $real_out — refusing" ;; esac

copied=0 current=0 pending=0 failed=0 entries=0 shared=0
while IFS= read -r entry || [[ -n "$entry" ]]; do
  entry="${entry%$'\r'}"
  [[ -z "${entry//[[:space:]]/}" || "$entry" == \#* ]] && continue
  entries=$((entries + 1))
  if [[ -n "${canonical[$entry]+x}" ]]; then
    shared=$((shared + 1))
    log "canonical: '$entry' is shared through refs/ (knowledge-apply); not copied here"
    continue
  fi
  case "/$entry/" in
    //*|*/../*|*/./*) die "unsafe manifest path: '$entry'" ;;
  esac
  [[ "$entry" == */* ]] || die "manifest path needs <collection>/<path>: '$entry'"
  collection="${entry%%/*}" rel="${entry#*/}"
  src="$books/$entry"
  [[ -L "$src" ]] && { warn "skipping symlink: $entry"; continue; }
  [[ -e "$src" ]] || { warn "missing in library: $entry"; failed=$((failed + 1)); continue; }
  real_src="$(realpath -e -- "$src")"
  case "$real_src/" in "$real_books"/*) : ;; *) die "'$entry' resolves outside $real_books — refusing" ;; esac

  # Text books: copy each regular .md/.txt, keeping its path below the collection.
  while IFS= read -r -d '' f; do
    out="$kroot/$profile/${f#"$books/$collection/"}"
    if [[ -f "$out" ]] && cmp -s "$f" "$out"; then current=$((current + 1)); continue; fi
    mkdir -p "$(dirname "$out")"
    cp -f -- "$f" "$out"
    copied=$((copied + 1))
  done < <(find "$src" -type f \( -iname '*.md' -o -iname '*.txt' \) -print0)

  # EPUB/PDF: hand the entry to the converter, which writes <rel>/<book>.md.
  n="$(find "$src" -type f \( -iname '*.epub' -o -iname '*.pdf' \) | wc -l)"
  [[ "$n" -gt 0 ]] || continue
  if [[ "$convert" != "1" ]]; then pending=$((pending + n)); continue; fi
  [[ -f "$doc2md" ]] || die "converter not found at $doc2md (set DOC2MD, or KNOWLEDGE_CONVERT=0 for text only)"
  rc=0
  python3 "$doc2md" --source "$books/$collection" --dest "$kroot/$profile" -- "$rel" || rc=$?
  case "$rc" in
    0) : ;;
    3) warn "some files in '$entry' could not be converted (see the report above)"; failed=$((failed + 1)) ;;
    *) die "converter failed on '$entry' (exit $rc)" ;;
  esac
done < "$manifest"

log "staged '$profile' from $entries book entries: $copied text copied, $current up to date, $pending EPUB/PDF waiting for conversion, $failed with problems, $shared canonical (referenced, not copied)"
log "next: make hermes-profile NAME=$profile   then /learn in that profile's chat"
[[ "$failed" -eq 0 ]] || exit 3
