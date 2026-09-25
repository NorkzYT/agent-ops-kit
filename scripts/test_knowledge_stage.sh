#!/usr/bin/env bash
# test_knowledge_stage.sh — offline check of scripts/knowledge-stage.sh against a
# temp book library, a temp knowledge root and a stub converter. Proves text is
# copied, EPUB/PDF goes to the converter, the library is never written, and
# unsafe manifest paths are refused. Also checks every tracked book list is
# well-formed and belongs to a profile with a SOUL.md.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
ok()  { echo "  ok   $*"; }
bad() { echo "  FAIL $*"; fail=1; }

tmp="$(mktemp -d)"; trap 'chmod -R u+w "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT
books="$tmp/books"; kroot="$tmp/knowledge"
mkdir -p "$books/edu/Book One" "$books/text/Series A"
printf 'epub\n'  > "$books/edu/Book One/book one.epub"
printf 'cover\n' > "$books/edu/Book One/cover.jpg"
printf 'vol 1\n' > "$books/text/Series A/vol 1.txt"
printf 'vol 2\n' > "$books/text/Series A/vol 2.md"
ln -s "$books/edu/Book One" "$books/edu/Linked"
before="$(cd "$books" && find . -printf '%p %s %T@\n' | sort | md5sum)"
chmod -R a-w "$books"

# Stub converter: records its args and writes <dest>/<rel>/<name>.md like doc2md.
stub="$tmp/doc2md.py"
cat > "$stub" <<'PY'
import os, sys
args = sys.argv[1:]
src, dest = args[args.index("--source") + 1], args[args.index("--dest") + 1]
rel = args[args.index("--") + 1]
open(os.environ["STUB_LOG"], "a").write(f"{src}|{dest}|{rel}\n")
for d, _, files in os.walk(os.path.join(src, rel)):
    for f in files:
        if f.endswith(".epub"):
            out = os.path.join(dest, os.path.relpath(os.path.join(d, f), src))[:-5] + ".md"
            os.makedirs(os.path.dirname(out), exist_ok=True)
            open(out, "w").write("# converted\n")
sys.exit(int(os.environ.get("STUB_RC", "0")))
PY

man="$tmp/manifest.txt"
printf '# comment\n\nedu/Book One\ntext/Series A\n' > "$man"
run() { BOOKS_ROOT="$books" KNOWLEDGE_ROOT="$kroot" DOC2MD="$stub" STUB_LOG="$tmp/log" \
          KNOWLEDGE_MANIFEST="$man" bash "$ROOT/scripts/knowledge-stage.sh" "${1:-demo}" 2>&1; }

out="$(run)"; rc=$?
[[ $rc -eq 0 ]] && ok "stage exits 0" || bad "stage exited $rc: $out"
[[ -f "$kroot/demo/Series A/vol 1.txt" && -f "$kroot/demo/Series A/vol 2.md" ]] \
  && ok "text books copied under the collection path" || bad "text books missing"
[[ -f "$kroot/demo/Book One/book one.md" ]] && ok "EPUB converted via doc2md" || bad "converted book missing"
[[ ! -e "$kroot/demo/Book One/cover.jpg" ]] && ok "non-book files skipped" || bad "cover.jpg leaked"
grep -qx "$books/edu|$kroot/demo|Book One" "$tmp/log" 2>/dev/null \
  && ok "converter gets --source <collection> --dest <profile> -- <path>" || bad "converter args wrong: $(cat "$tmp/log" 2>/dev/null)"
chmod -R u+w "$books"
after="$(cd "$books" && find . -printf '%p %s %T@\n' | sort | md5sum)"
[[ "$before" == "$after" ]] && ok "book library unchanged" || bad "book library was modified"
chmod -R a-w "$books"

out="$(run)"; grep -q '2 up to date' <<<"$out" && ok "rerun is idempotent" || bad "rerun recopied: $out"

out="$(KNOWLEDGE_CONVERT=0 run text-only)"
grep -q '1 EPUB/PDF waiting' <<<"$out" && [[ ! -e "$kroot/text-only/Book One" ]] \
  && ok "KNOWLEDGE_CONVERT=0 copies text only and reports pending" || bad "text-only mode wrong: $out"

out="$(STUB_RC=3 run partial)"; rc=$?
[[ $rc -eq 3 ]] && ok "unconverted book -> exit 3" || bad "partial conversion exit $rc"

for badline in '/etc' 'edu/../../etc' '../books/edu' 'edu' './edu/Book One'; do
  printf '%s\n' "$badline" > "$man"
  out="$(run evil)"; rc=$?
  [[ $rc -ne 0 ]] && ok "refused manifest path '$badline'" || bad "accepted '$badline'"
done
printf 'edu/Linked\n' > "$man"
out="$(run linked)"; grep -q 'skipping symlink' <<<"$out" && ok "symlinked entry skipped" || bad "symlink followed: $out"

printf 'edu/Book One\n' > "$man"
out="$(run '../x')"; [[ $? -ne 0 ]] && ok "bad profile name refused" || bad "bad profile name accepted"
out="$(BOOKS_ROOT="$books" KNOWLEDGE_ROOT="$books/edu" KNOWLEDGE_MANIFEST="$man" \
       bash "$ROOT/scripts/knowledge-stage.sh" demo 2>&1)"
[[ $? -ne 0 ]] && ok "output inside the library refused" || bad "wrote into the library"

# Tracked book lists: one per real profile, relative paths only.
for m in "$ROOT"/hermes/profiles/*/knowledge-sources.txt; do
  p="$(basename "$(dirname "$m")")"
  [[ -f "$ROOT/hermes/profiles/$p/SOUL.md" ]] || bad "$p has a book list and no SOUL.md"
  if grep -vE '^(#|[[:space:]]*$)' "$m" | grep -qE '^/|(^|/)\.\.(/|$)|^[^/]+$'; then
    bad "$p book list has an unsafe or collection-less path"
  fi
done
ok "tracked book lists are well-formed"

echo
[[ "$fail" -eq 0 ]] && echo "PASS: knowledge-stage" || echo "FAIL: knowledge-stage"
exit "$fail"
