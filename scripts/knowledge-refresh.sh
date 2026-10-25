#!/usr/bin/env bash
# knowledge-refresh.sh [--inventory] [profile...] — refresh every book-backed
# profile after books are added under the library, and report what changed.
#
#   knowledge-refresh.sh               stage + sync every profile with a book list
#   knowledge-refresh.sh strategy      the same for one profile (or several)
#   knowledge-refresh.sh --inventory   read-only: library vs. book lists, no writes
#   knowledge-refresh.sh --uncategorized   read-only: uncategorized paths only,
#                                      NUL-terminated (for knowledge-classify.py)
#
# Per profile: scripts/knowledge-stage.sh builds $KNOWLEDGE_ROOT/<p> from
# hermes/profiles/<p>/knowledge-sources.txt, then scripts/knowledge-sync.sh copies
# it into $HERMES_HOME/profiles/<p>/knowledge when that profile is installed.
# The report lists each profile as updated/current and sorts the library books
# no book list includes by content (scripts/knowledge_match.py, normalized-text
# fingerprints, never file names):
#   already imported, source not linked   the text is staged in some profile under
#                                         another path; nothing to classify
#   uncategorized                         not imported anywhere; the exact line to append
# plus missing manifest sources (book-list lines not in the library, and staged
# files no book-list line produces; see make knowledge-reconcile). It never
# picks a profile for a book and never runs /learn (a model turn); it prints the
# /learn command for each updated profile.
#
# Env: BOOKS_ROOT        book library root (default /mnt/books, never written)
#      KNOWLEDGE_ROOT    staged corpora (default data/knowledge, never pruned)
#      HERMES_HOME       Hermes home (default: .env HERMES_HOME, else ~/.hermes)
#      KNOWLEDGE_IGNORE  library paths to leave out of the report
#                        (default hermes/knowledge-ignore.txt)
#      PROFILES_DIR      profile seeds (default hermes/profiles; tests)
#      KNOWLEDGE_CONVERT, DOC2MD   passed through to knowledge-stage.sh and the
#                        content check (EPUB/PDF text, temp folder, OCR off)
#      KNOWLEDGE_MATCH   0 skips the content check (all unlisted books count as
#                        uncategorized)
#
# Exit 0 when every profile staged cleanly, 3 when a book is missing or could
# not be converted, 1 on a usage or safety error. Uncategorized books do not
# change the exit code.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

inventory_only=0
porcelain=0
names=()
for a in "$@"; do
  case "$a" in
    --inventory) inventory_only=1 ;;
    --uncategorized) inventory_only=1; porcelain=1 ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $a" ;;
    *) names+=("$a") ;;
  esac
done

books="${BOOKS_ROOT:-/mnt/books}"
kroot="${KNOWLEDGE_ROOT:-$ROOT/data/knowledge}"
pdir_root="${PROFILES_DIR:-$ROOT/hermes/profiles}"
ignore_file="${KNOWLEDGE_IGNORE:-$ROOT/hermes/knowledge-ignore.txt}"
if [[ -z "${HERMES_HOME:-}" ]]; then
  HERMES_HOME="$(env_file_get HERMES_HOME "$ROOT/.env" 2>/dev/null || echo "$HOME/.hermes")"
fi
[[ -d "$books" ]] || die "book library not found at $books (set BOOKS_ROOT)"
books="$(cd "$books" && pwd -P)"

valid_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && "$1" != *".."* ]]; }
# sq <s> -> s single-quoted for a copy-paste shell command.
sq() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
# entries <file> -> manifest lines, without comments, blanks or CR.
entries() {
  local e
  while IFS= read -r e || [[ -n "$e" ]]; do
    e="${e%$'\r'}"
    [[ -z "${e//[[:space:]]/}" || "$e" == \#* ]] && continue
    printf '%s\n' "$e"
  done < "$1"
}
safe_entry() {
  case "/$1/" in //*|*/../*|*/./*) return 1 ;; esac
  [[ "$1" == */* ]]
}
is_book() { [[ "${1,,}" =~ \.(epub|pdf|md|txt)$ ]]; }
has_books() {
  [[ -f "$1" ]] && { is_book "$1"; return; }
  [[ -n "$(find "$1" -type f \( -iname '*.epub' -o -iname '*.pdf' -o -iname '*.md' -o -iname '*.txt' \) -print -quit)" ]]
}

# Every profile with a book list; the selection is all of them or the named ones.
all=()
for m in "$pdir_root"/*/knowledge-sources.txt; do
  [[ -f "$m" ]] || continue
  p="$(basename "$(dirname "$m")")"
  [[ "$p" == _* ]] || all+=("$p")
done
[[ ${#all[@]} -gt 0 ]] || die "no book lists under $pdir_root/*/knowledge-sources.txt"
if [[ ${#names[@]} -eq 0 ]]; then
  names=("${all[@]}")
else
  for p in "${names[@]}"; do
    valid_name "$p" || die "invalid profile name: '$p'"
    [[ -f "$pdir_root/$p/knowledge-sources.txt" ]] || die "no book list at $pdir_root/$p/knowledge-sources.txt"
  done
fi

# Coverage map from ALL book lists (so a book listed elsewhere is not uncategorized).
declare -A owners=()
missing=()
for p in "${all[@]}"; do
  while IFS= read -r e; do
    safe_entry "$e" || continue      # knowledge-stage.sh refuses these loudly
    owners["$e"]+="${owners[$e]:+, }$p"
    [[ -e "$books/$e" || -L "$books/$e" ]] || missing+=("$p|$e")
  done < <(entries "$pdir_root/$p/knowledge-sources.txt")
done
if [[ -f "$ignore_file" ]]; then
  while IFS= read -r e; do owners["$e"]+="${owners[$e]:+, }(ignored)"; done < <(entries "$ignore_file")
fi

# covered <rel>: 0 when rel or an ancestor is listed, 2 when only something
# below it is listed (descend), 1 when nothing is.
covered() {
  local r="$1" e
  while :; do
    [[ -n "${owners[$r]+x}" ]] && return 0
    [[ "$r" == */* ]] || break
    r="${r%/*}"
  done
  for e in "${!owners[@]}"; do [[ "$e" == "$1"/* ]] && return 2; done
  return 1
}
uncategorized=()
scan() {   # scan <rel dir>: record the smallest uncovered book paths below it
  local child rel rc
  while IFS= read -r -d '' child; do
    rel="${child#"$books"/}"
    [[ -L "$child" ]] && continue
    has_books "$child" || continue
    rc=0; covered "$rel" || rc=$?
    case "$rc" in
      1) uncategorized+=("$rel") ;;
      2) [[ -d "$child" ]] && scan "$rel" ;;
    esac
  done < <(find "$books/$1" -mindepth 1 -maxdepth 1 ! -name '.*' -print0 | sort -z)
}
while IFS= read -r -d '' c; do
  [[ -L "$c" ]] || scan "$(basename "$c")"
done < <(find "$books" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -print0 | sort -z)
if [[ "$porcelain" -eq 1 ]]; then
  [[ ${#uncategorized[@]} -eq 0 ]] || printf '%s\0' "${uncategorized[@]}"
  exit 0
fi

# snap <dir> <out>: content hashes of the .md/.txt files under dir.
snap() {
  if [[ -d "$1" ]]; then
    (cd "$1" && find . -type f \( -name '*.md' -o -name '*.txt' \) -print0 | LC_ALL=C sort -z | xargs -0 -r md5sum) > "$2"
  else : > "$2"; fi
}
changed() { LC_ALL=C comm -13 <(LC_ALL=C sort "$1") <(LC_ALL=C sort "$2") | wc -l; }

# match_content: sort the uncategorized books by content against the staged
# corpora (after staging, so the report reflects this run). Read-only.
newbooks=() imported=() orphans=()
declare -A partial=()
match_content() {
  local out kind a b
  if [[ "${KNOWLEDGE_MATCH:-1}" != "1" ]]; then newbooks=("${uncategorized[@]}"); return; fi
  out="$(mktemp "${TMPDIR:-/tmp}/knowledge-match.XXXXXX")"
  if printf '%s\0' "${uncategorized[@]}" | BOOKS_ROOT="$books" KNOWLEDGE_ROOT="$kroot" PROFILES_DIR="$pdir_root" \
       python3 "$ROOT/scripts/knowledge_match.py" inventory > "$out"; then
    while IFS= read -r -d '' kind && IFS= read -r -d '' a && IFS= read -r -d '' b; do
      case "$kind" in
        imported) imported+=("$a|$b") ;;
        partial)  newbooks+=("$a"); partial[$a]="$b" ;;
        orphan)   orphans+=("$a|$b") ;;
        *)        newbooks+=("$a") ;;
      esac
    done < "$out"
  else
    warn "content check failed; every unlisted book is reported as uncategorized"
    newbooks=("${uncategorized[@]}")
  fi
  rm -f "$out"
}

declare -A status=() note=()
learn=()
worst=0
logs=""
if [[ "$inventory_only" -eq 0 ]]; then
  logs="$(mktemp -d "${TMPDIR:-/tmp}/knowledge-refresh.XXXXXX")"
  for p in "${names[@]}"; do
    log "refreshing '$p' (log: $logs/$p.log)"
    snap "$kroot/$p" "$logs/$p.k0"
    rc=0
    BOOKS_ROOT="$books" KNOWLEDGE_ROOT="$kroot" PROFILES_DIR="$pdir_root" \
      KNOWLEDGE_MANIFEST="$pdir_root/$p/knowledge-sources.txt" \
      bash "$ROOT/scripts/knowledge-stage.sh" "$p" > "$logs/$p.log" 2>&1 || rc=$?
    snap "$kroot/$p" "$logs/$p.k1"
    staged="$(changed "$logs/$p.k0" "$logs/$p.k1")"
    case "$rc" in
      0) : ;;
      3) note[$p]="some books missing or not converted (see log)"; worst=3 ;;
      *) status[$p]="error"; note[$p]="knowledge-stage.sh exit $rc: $(grep -m1 '\[error\]' "$logs/$p.log" || tail -n1 "$logs/$p.log")"
         worst=1; continue ;;
    esac
    pk="$HERMES_HOME/profiles/$p/knowledge"
    if [[ ! -d "$HERMES_HOME/profiles/$p" ]]; then
      status[$p]="not-installed"
      note[$p]="${note[$p]:+${note[$p]}; }$staged staged file(s) new/changed; install with: make hermes-profile NAME=$p"
      continue
    fi
    snap "$pk" "$logs/$p.p0"
    KNOWLEDGE_ROOT="$kroot" bash "$ROOT/scripts/knowledge-sync.sh" "$p" "$pk" >> "$logs/$p.log" 2>&1 \
      || { status[$p]="error"; note[$p]="knowledge-sync.sh failed (see log)"; worst=1; continue; }
    snap "$pk" "$logs/$p.p1"
    synced="$(changed "$logs/$p.p0" "$logs/$p.p1")"
    if [[ "$synced" -gt 0 ]]; then
      status[$p]="updated"; learn+=("$p")
      note[$p]="$synced file(s) new/changed in the profile${note[$p]:+; ${note[$p]}}"
    else
      status[$p]="current"
    fi
  done
fi

# ---- report -----------------------------------------------------------------
match_content
echo
echo "== knowledge refresh report =="
echo "library: $books   staged: $kroot   hermes: $HERMES_HOME"
if [[ "$inventory_only" -eq 0 ]]; then
  echo
  printf '%-28s %-14s %s\n' PROFILE STATUS NOTES
  for p in "${names[@]}"; do printf '%-28s %-14s %s\n' "$p" "${status[$p]:-?}" "${note[$p]:-}"; done
fi

echo
sel=" ${names[*]} "
nm=0
for m in "${missing[@]}"; do [[ "$sel" == *" ${m%%|*} "* ]] && nm=$((nm + 1)); done
echo "Missing manifest sources ($nm): listed in a book list, not found under $books"
for m in "${missing[@]}"; do
  [[ "$sel" == *" ${m%%|*} "* ]] && printf '  %-26s %s\n' "${m%%|*}" "${m#*|}"
done
no=0
for m in "${orphans[@]}"; do [[ "$sel" == *" ${m%%|*} "* ]] && no=$((no + 1)); done
echo "Staged without a manifest source ($no): files in $kroot/<profile> that no book-list line stages"
for m in "${orphans[@]}"; do
  [[ "$sel" == *" ${m%%|*} "* ]] && printf '  %-26s %s\n' "${m%%|*}" "${m#*|}"
done
[[ "$no" -eq 0 ]] || echo "  Link a library source (proposal first, applied only on your approval): make knowledge-reconcile"

echo
echo "Already imported, source not linked (${#imported[@]}): library books no book list includes whose text is already staged under another path"
if [[ ${#imported[@]} -gt 0 ]]; then
  echo "  Nothing to classify or re-ingest. Owners are reported, never chosen. Review: make knowledge-reconcile"
  for m in "${imported[@]}"; do
    echo "  - ${m%%|*}"
    echo "      owners: ${m#*|}"
  done
fi

echo
echo "Uncategorized (${#newbooks[@]}): books in the library that no book list includes and no profile has staged"
if [[ ${#newbooks[@]} -gt 0 ]]; then
  echo "  Pick the profile yourself (nothing is assigned automatically), append the line, rerun."
  echo "  For AI-assisted proposals (dry-run, nothing assigned): make knowledge-classify"
  echo "  Profiles with book lists: ${all[*]}"
  for u in "${newbooks[@]}"; do
    echo "  - $u"
    [[ -z "${partial[$u]:-}" ]] || echo "      partly staged already (review before assigning): ${partial[$u]}"
    echo "      printf '%s\\n' $(sq "$u") >> ${pdir_root#"$ROOT"/}/<profile>/knowledge-sources.txt"
  done
  echo "  To leave a book out on purpose, add the same line to ${ignore_file#"$ROOT"/}."
fi

if [[ "$inventory_only" -eq 0 ]]; then
  echo
  if [[ ${#learn[@]} -gt 0 ]]; then
    echo "Next: /learn is NOT run automatically. For each updated profile, run in its chat:"
    for p in "${learn[@]}"; do
      echo "  hermes -p $p chat      then:   /learn $HERMES_HOME/profiles/$p/knowledge"
    done
    echo "Or headless, one book per run and resumable: make knowledge-learn NAME=<profile> (progress: make knowledge-learn-status)"
  else
    echo "Next: no profile knowledge changed; /learn is not needed."
  fi
  echo "Logs: $logs"
else
  echo
  echo "Inventory only: nothing was staged or synced. Run: make knowledge-refresh"
fi
exit "$worst"
