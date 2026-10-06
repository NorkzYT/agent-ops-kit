#!/usr/bin/env bash
# test_knowledge_refresh.sh — offline check of scripts/knowledge-refresh.sh with a
# temp book library, temp profile seeds, a temp HERMES_HOME and a stub converter.
# Proves inventory is read-only, refresh stages + syncs and reports
# updated/current/not-installed, missing and uncategorized books are reported
# with exact manifest lines (never assigned), nothing is deleted, the library is
# never written, and /learn is printed but never run.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
ok()  { echo "  ok   $*"; }
bad() { echo "  FAIL $*"; fail=1; }

tmp="$(mktemp -d)"; trap 'chmod -R u+w "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT
books="$tmp/books"; kroot="$tmp/knowledge"; seeds="$tmp/profiles"; hh="$tmp/hermes"
mkdir -p "$books/edu/Book One" "$books/edu/New Book" "$books/edu/It's Quoted" "$books/edu/Covers Only" \
         "$books/text/Series A" "$books/text/Mixed" "$books/manga/Vol 1" "$books/.hidden"
printf 'epub\n'  > "$books/edu/Book One/book one.epub"
printf 'cover\n' > "$books/edu/Book One/cover.jpg"
printf 'epub\n'  > "$books/edu/New Book/new.epub"
printf 'pdf\n'   > "$books/edu/It's Quoted/q.pdf"
printf 'cover\n' > "$books/edu/Covers Only/cover.jpg"
printf 'vol 1\n' > "$books/text/Series A/vol 1.txt"
printf 'vol 2\n' > "$books/text/Series A/vol 2.md"
printf 'kept\n'  > "$books/text/Mixed/listed.md"
printf 'extra\n' > "$books/text/Mixed/extra.txt"
printf 'manga\n' > "$books/manga/Vol 1/ch1.txt"
printf 'dot\n'   > "$books/.hidden/x.md"
lib_hash() { (cd "$books" && find . -printf '%p %s %T@\n' | sort | md5sum); }
before="$(lib_hash)"
chmod -R a-w "$books"

mkdir -p "$seeds/alpha" "$seeds/beta" "$seeds/gamma" "$seeds/_template"
printf '# alpha\nedu/Book One\ntext/Mixed/listed.md\nedu/Gone Book\n' > "$seeds/alpha/knowledge-sources.txt"
printf 'text/Series A\n' > "$seeds/beta/knowledge-sources.txt"
printf 'text/Series A\n' > "$seeds/gamma/knowledge-sources.txt"
printf 'edu/New Book\n'  > "$seeds/_template/knowledge-sources.txt"
printf 'manga\n' > "$tmp/ignore.txt"
mkdir -p "$hh/profiles/alpha" "$hh/profiles/beta"      # gamma is not installed

stub="$tmp/doc2md.py"
cat > "$stub" <<'PY'
import os, sys
args = sys.argv[1:]
src, dest = args[args.index("--source") + 1], args[args.index("--dest") + 1]
rel = args[args.index("--") + 1]
for d, _, files in os.walk(os.path.join(src, rel)):
    for f in files:
        if f.endswith((".epub", ".pdf")):
            out = os.path.splitext(os.path.join(dest, os.path.relpath(os.path.join(d, f), src)))[0] + ".md"
            if not os.path.exists(out):
                os.makedirs(os.path.dirname(out), exist_ok=True)
                open(out, "w").write("# converted\n")
PY
export BOOKS_ROOT="$books" KNOWLEDGE_ROOT="$kroot" PROFILES_DIR="$seeds" HERMES_HOME="$hh" \
       KNOWLEDGE_IGNORE="$tmp/ignore.txt" DOC2MD="$stub" TMPDIR="$tmp"
run() { bash "$ROOT/scripts/knowledge-refresh.sh" "$@" 2>&1; }
row() { grep -E "^$1 +$2( |$)" <<<"$out" >/dev/null; }

# --- inventory: read-only report -------------------------------------------
out="$(run --inventory)"; rc=$?
[[ $rc -eq 0 ]] && ok "inventory exits 0" || bad "inventory exited $rc: $out"
[[ ! -e "$kroot" ]] && ok "inventory writes nothing" || bad "inventory created $kroot"
grep -q '^Uncategorized (3)' <<<"$out" && ok "three uncategorized books" || bad "uncategorized count wrong: $out"
for u in "edu/New Book" "edu/It's Quoted" "text/Mixed/extra.txt"; do
  grep -qxF "  - $u" <<<"$out" && ok "reports uncategorized '$u'" || bad "missed '$u'"
done
for n in "edu/Book One" "manga" "Covers Only" ".hidden" "text/Series A" "text/Mixed/listed.md"; do
  grep -qF -- "- $n" <<<"$out" && bad "'$n' wrongly reported as uncategorized" || true
done
ok "listed, ignored, hidden and book-less paths are not reported"
grep -qE '^  alpha +edu/Gone Book$' <<<"$out" && ok "missing manifest entry reported" || bad "missing book not reported"
grep -q '_template' <<<"$out" && bad "_template treated as a profile" || ok "_template ignored"
grep -qF "<profile>/knowledge-sources.txt" <<<"$out" && ! grep -qE 'printf .*(alpha|beta|gamma)/knowledge-sources' <<<"$out" \
  && ok "suggestions leave the profile to the operator" || bad "a profile was guessed"
line="$(grep -F "It'\\''s Quoted" <<<"$out" | sed 's#>> .*#>> "$tmp/knowledge-sources.txt"#')"
( cd "$tmp" && eval "$line" ) 2>/dev/null
[[ "$(cat "$tmp/knowledge-sources.txt" 2>/dev/null)" == "edu/It's Quoted" ]] \
  && ok "suggested command appends the exact manifest line" || bad "suggested command broken: $line"

grep -qF 'make knowledge-classify' <<<"$out" && ok "inventory points at the AI proposal target" || bad "no knowledge-classify hint"

# --- --uncategorized: NUL-separated paths only (knowledge-classify input) ------
mapfile -d '' unc < <(bash "$ROOT/scripts/knowledge-refresh.sh" --uncategorized 2>/dev/null); rc=$?
[[ $rc -eq 0 && ${#unc[@]} -eq 3 && "${unc[0]}" == "edu/It's Quoted" && "${unc[1]}" == "edu/New Book" \
   && "${unc[2]}" == "text/Mixed/extra.txt" ]] && ok "--uncategorized lists exactly the uncategorized paths" \
  || bad "--uncategorized wrong: ${unc[*]}"
[[ ! -e "$kroot" ]] && ok "--uncategorized writes nothing" || bad "--uncategorized created $kroot"

# --- refresh all ------------------------------------------------------------
out="$(run)"; rc=$?
[[ $rc -eq 3 ]] && ok "missing book -> exit 3" || bad "refresh exited $rc: $out"
row alpha updated && row beta updated && ok "installed profiles updated" || bad "status wrong: $out"
row gamma not-installed && grep -q 'make hermes-profile NAME=gamma' <<<"$out" \
  && ok "uninstalled profile staged, not created" || bad "gamma handling wrong"
[[ ! -e "$hh/profiles/gamma" && -f "$kroot/gamma/Series A/vol 1.txt" ]] && ok "gamma staged only" || bad "gamma created or not staged"
[[ -f "$hh/profiles/alpha/knowledge/Book One/book one.md" && -f "$hh/profiles/alpha/knowledge/Mixed/listed.md" \
   && -f "$hh/profiles/beta/knowledge/Series A/vol 2.md" ]] && ok "profiles synced" || bad "profile knowledge missing"
[[ ! -e "$hh/profiles/alpha/knowledge/Mixed/extra.txt" ]] && ok "uncategorized book not assigned" || bad "extra.txt leaked"
grep -qF "/learn $hh/profiles/alpha/knowledge" <<<"$out" && grep -qF "/learn $hh/profiles/beta/knowledge" <<<"$out" \
  && ! grep -qF "/learn $hh/profiles/gamma" <<<"$out" && grep -q 'NOT run automatically' <<<"$out" \
  && ok "/learn printed for updated profiles only" || bad "/learn instructions wrong: $out"

# --- rerun is idempotent -----------------------------------------------------
out="$(run)"
row alpha current && row beta current && ok "rerun reports current" || bad "rerun not current: $out"
grep -q '/learn is not needed' <<<"$out" && ok "no /learn when nothing changed" || bad "spurious /learn"

[[ "$before" == "$(chmod -R u+w "$books"; lib_hash; chmod -R a-w "$books")" ]] \
  && ok "library unchanged by refresh" || bad "library modified by refresh"

# --- a new book refreshes one profile; nothing is deleted ---------------------
printf 'mine\n' > "$kroot/beta/stale.md"; printf 'mine\n' > "$hh/profiles/beta/knowledge/own.md"
chmod u+w "$books/text/Series A"; printf 'vol 3\n' > "$books/text/Series A/vol 3.txt"; chmod a-w "$books/text/Series A"
before="$(chmod -R u+w "$books"; lib_hash; chmod -R a-w "$books")"
out="$(run beta)"; rc=$?
[[ $rc -eq 0 ]] && row beta updated && ! grep -q '^alpha ' <<<"$out" && ok "NAME=beta refreshes only beta" || bad "single refresh wrong ($rc): $out"
grep -qE '^Missing manifest sources \(0\)' <<<"$out" && ok "missing list scoped to the selection" || bad "missing not scoped"
[[ -f "$hh/profiles/beta/knowledge/Series A/vol 3.txt" ]] && ok "new book synced" || bad "new book not synced"
[[ -f "$kroot/beta/stale.md" && -f "$hh/profiles/beta/knowledge/own.md" ]] && ok "nothing deleted" || bad "files were deleted"

# --- content-equivalent copy: already imported, not uncategorized ------------
# A library book whose text is staged in alpha under another name and path
# (converter front matter, different line breaks) is reported with its owner,
# never as uncategorized; the staged file has no manifest source (reconcile).
words="$(for i in $(seq 1 400); do printf 'w%d%s ' "$((i * 7919 % 1009))" "$([[ $((i % 13)) -eq 0 ]] && echo .)"; done)"
mkdir -p "$kroot/alpha/Legacy"
printf -- '---\nsource: "old/legacy.epub"\nconverter: "doc2md 1.0.0"\n---\n\n%s\n' "$words" > "$kroot/alpha/Legacy/orig.md"
chmod u+w "$books/text"; mkdir -p "$books/text/Legacy Copy"
fold -s -w 50 <<<"$words" > "$books/text/Legacy Copy/copy.txt"; chmod -R a-w "$books/text"
before="$(chmod -R u+w "$books"; lib_hash; chmod -R a-w "$books")"
k0="$(cd "$kroot" && find . -type f -printf '%p %s %T@\n' | sort | md5sum)"
out="$(run --inventory)"
grep -q '^Already imported, source not linked (1)' <<<"$out" && grep -qxF '  - text/Legacy Copy' <<<"$out" \
  && grep -qE '^      owners: alpha: Legacy/orig.md \(exact' <<<"$out" && ok "content copy reported as already imported with its owner" \
  || bad "already-imported section wrong: $out"
grep -q '^Uncategorized (3)' <<<"$out" && ! grep -qxF '  - text/Legacy Copy' <<<"$(sed -n '/^Uncategorized/,$p' <<<"$out")" \
  && ok "content copy is not uncategorized" || bad "content copy listed as uncategorized"
grep -qE '^  alpha +Legacy/orig.md \(no book-list entry' <<<"$out" && grep -qE '^  beta +stale.md' <<<"$out" \
  && grep -q 'make knowledge-reconcile' <<<"$out" && ok "staged files without a manifest source reported" || bad "orphans not reported: $out"
out="$(KNOWLEDGE_MATCH=0 run --inventory)"
grep -q '^Uncategorized (4)' <<<"$out" && ok "KNOWLEDGE_MATCH=0 skips the content check" || bad "KNOWLEDGE_MATCH=0 ignored"
[[ "$k0" == "$(cd "$kroot" && find . -type f -printf '%p %s %T@\n' | sort | md5sum)" ]] \
  && ok "inventory left data/knowledge untouched" || bad "inventory changed data/knowledge"

out="$(run ../x)"; [[ $? -eq 1 ]] && ok "bad profile name refused" || bad "bad name accepted"
out="$(run nope)"; [[ $? -eq 1 ]] && ok "profile without a book list refused" || bad "unknown profile accepted"

out="$(make -s -C "$ROOT" knowledge-inventory 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && grep -q 'Uncategorized (3)' <<<"$out" && ok "make knowledge-inventory" || bad "make target failed ($rc): $out"

chmod -R u+w "$books"
[[ "$before" == "$(lib_hash)" ]] && ok "book library unchanged" || bad "book library was modified"

echo
[[ "$fail" -eq 0 ]] && echo "PASS: knowledge-refresh" || echo "FAIL: knowledge-refresh"
exit "$fail"
