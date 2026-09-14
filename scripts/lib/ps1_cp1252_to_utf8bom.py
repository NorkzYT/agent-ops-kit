#!/usr/bin/env python3
"""Reproduce a Windows PowerShell 5.1 ANSI misread of a BOM-less .ps1 for testing.

Drops a leading UTF-8 BOM if present, decodes the remaining bytes as CP1252 (what
Windows PowerShell 5.1 does with a BOM-less script), then writes the result as
UTF-8-with-BOM so a pwsh tokenizer reads back exactly the mojibake the Windows
worker would have seen. Used by scripts/test_windows_ps1_encoding.sh.

Usage: ps1_cp1252_to_utf8bom.py <src.ps1> <dst.ps1>
"""
import sys


def main() -> int:
    src, dst = sys.argv[1], sys.argv[2]
    raw = open(src, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    open(dst, "w", encoding="utf-8-sig").write(raw.decode("cp1252"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
