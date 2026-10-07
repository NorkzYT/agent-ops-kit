#!/usr/bin/env python3
"""config-get — read one value from a Hermes config.yaml without PyYAML.

Usage: config-get.py FILE dotted.key
Prints the value (scalars as-is, lists as ``a,b``) and exits 0; exits 1 when
the file or key is missing. ``null``/``~`` print nothing and exit 0, so the
caller can tell "set to null" from "absent".

Supports the subset Hermes and the kit write: block mappings, block lists
(``- item``), flow lists (``[a, b]``), quoted/plain scalars and ``#`` comments.
Used by scripts/hermes-install.sh to keep live specialist settings on --force.
Never prints anything but the requested key.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_KEY = re.compile(r"^(\s*)([^\s#:][^:#]*?|'[^']*'|\"[^\"]*\")\s*:(?:\s+(.*?))?\s*$")


def _strip_comment(raw: str) -> str:
    out, quote = [], ""
    for i, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_flow(body: str) -> List[str]:
    """Split a flow list body on commas outside quotes."""
    parts, cur, quote, i = [], [], "", 0
    while i < len(body):
        ch = body[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(body):
                cur.append(body[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _scalar(raw: str) -> Optional[str]:
    value = _strip_comment(raw).strip()
    if value in ("", "~", "null", "Null", "NULL"):
        return None
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)  # YAML double-quoted escapes (\" and \\)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        items = [_scalar(p) for p in _split_flow(value[1:-1])]
        return ",".join(i for i in items if i)
    return value


def lookup(text: str, dotted: str) -> Tuple[bool, Optional[str]]:
    path = dotted.split(".")
    lines = text.splitlines()
    depth, indent, child, i = 0, -1, 0, 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KEY.match(line)
        if not m:
            continue
        ind = len(m.group(1))
        key = m.group(2).strip().strip("'\"")
        if ind <= indent:
            return False, None  # left the parent block without finding the key
        if child is None:
            child = ind
        if ind != child or key != path[depth]:
            continue  # a sibling, or a key nested inside a sibling
        rest = m.group(3) or ""
        if depth == len(path) - 1:
            if _scalar(rest) is not None or _strip_comment(rest).strip():
                return True, _scalar(rest)
            items: List[str] = []
            while i < len(lines):  # block list under the key
                nxt = lines[i]
                if not nxt.strip() or nxt.lstrip().startswith("#"):
                    i += 1
                    continue
                stripped = nxt.lstrip()
                if not stripped.startswith("- ") and stripped != "-":
                    break
                if len(nxt) - len(stripped) < ind:
                    break
                item = _scalar(stripped[1:])
                if item:
                    items.append(item)
                i += 1
            return True, ",".join(items) if items else None
        depth, indent, child = depth + 1, ind, None
    return False, None


def main(argv: List[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        return 1
    found, value = lookup(path.read_text(encoding="utf-8", errors="replace"), argv[2])
    if not found:
        return 1
    if value is not None:
        print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
