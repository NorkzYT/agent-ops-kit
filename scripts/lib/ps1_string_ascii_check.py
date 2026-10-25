#!/usr/bin/env python3
"""Fail if a .ps1 file has a non-ASCII char inside a single-line double-quoted string.

Such a character is the trigger for the Windows PowerShell CP1252 smart-quote bug
(see scripts/test_windows_ps1_encoding.sh). Non-ASCII in `#` comments and in
`@"…"@` here-strings is safe and is intentionally ignored: comments carry no
quotes and here-strings are not closed by an embedded (curly or straight) quote.

Exit 0 = clean. Exit 1 = at least one offending location (printed to stderr).
"""
import re
import sys


def offenders(text: str):
    hits = []
    in_block = False   # <# … #>
    in_here = False    # @"  …  "@
    for n, line in enumerate(text.split("\n"), 1):
        if in_here:
            if re.match(r'\s*"@', line):
                in_here = False
            continue
        if in_block:
            if "#>" in line:
                in_block = False
            continue
        if "<#" in line and "#>" not in line:
            in_block = True
            continue
        if re.search(r'@"\s*$', line):
            in_here = True
            continue
        # Drop a trailing line comment so `#`-commented text is not scanned. This is
        # deliberately conservative: it strips from the first `#` that is not inside
        # a double-quoted span, which is enough for these scripts.
        scanned, in_str = [], False
        for ch in line:
            if ch == '"':
                in_str = not in_str
            if ch == "#" and not in_str:
                break
            scanned.append(ch)
        code = "".join(scanned)
        for m in re.finditer(r'"([^"]*)"', code):
            bad = [c for c in m.group(1) if ord(c) > 127]
            if bad:
                hits.append((n, bad, line.strip()[:80]))
    return hits


def main() -> int:
    path = sys.argv[1]
    text = open(path, encoding="utf-8-sig").read()  # tolerate the BOM we require
    hits = offenders(text)
    for n, bad, snippet in hits:
        sys.stderr.write(
            f"{path}:{n}: non-ASCII {bad!r} inside a double-quoted string -> {snippet}\n"
        )
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
