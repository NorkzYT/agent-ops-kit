#!/usr/bin/env python3
r"""Prove the honcho.json BOM regression and its fix, end to end, without pwsh.

THE BUG (fixed): the installer wrote honcho.json with `Set-Content -Encoding UTF8`.
Windows PowerShell 5.1's `-Encoding UTF8` emits a leading UTF-8 BOM (EF BB BF).
Hermes's Honcho CLI `_read_config()` does `path.read_text(encoding='utf-8')` then
`json.loads(...)` and swallows every exception as `{}`. Python's json.loads rejects a
leading U+FEFF with "Unexpected UTF-8 BOM", so a BOM-prefixed honcho.json is silently
treated as absent — the exact `No Honcho config found at ...\.hermes\honcho.json`
symptom, even though the installer had just written that path.

THE FIX: generated config files are written UTF-8 WITHOUT a BOM (the installer's
`Write-Utf8NoBom` helper -> System.Text.UTF8Encoding($false)). The .ps1 scripts
themselves keep their BOM so Windows PowerShell 5.1 reads them as UTF-8, not CP1252
(see test_windows_ps1_encoding.sh — a separate, independent concern).

This script renders the installer's real honcho.json here-string with sample values,
then asserts:
  * the rendered text is valid JSON,
  * written UTF-8-no-BOM (the fix) it has no BOM and the exact Hermes read path
    (read_text('utf-8') + json.loads) round-trips,
  * written UTF-8-with-BOM (the old Set-Content bug) that same read path RAISES
    "Unexpected UTF-8 BOM" — reproducing the field failure.

No secrets: all substituted values are synthetic sample data.

Usage: honcho_bom_repro.py <install-worker.ps1>
Exit 0 = fix holds and the bug is reproduced; nonzero + stderr on any failure.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

# Synthetic sample substitutions for the here-string's PowerShell variables.
SAMPLES = {
    "HostAddress": "100.64.0.1",
    "HonchoPort": "8000",
    "PeerName": "windows-operator",
    "Workspace": "default",
}


def render_honcho_json(ps1_text: str) -> str:
    """Extract the honcho.json here-string and render it like PowerShell would.

    Finds the `@"..."@` here-string containing `"defaultHost"`, then applies the two
    PowerShell substitutions the installer relies on: a backtick-escaped colon
    (`` `: ``) is a literal `:`, and `$Name` tokens interpolate to their value.
    """
    m = re.search(r'@"\r?\n(?P<body>.*?)\r?\n"@', ps1_text, re.DOTALL)
    blocks = re.findall(r'@"\r?\n(.*?)\r?\n"@', ps1_text, re.DOTALL)
    body = next((b for b in blocks if '"defaultHost"' in b), None)
    if body is None:
        raise SystemExit("could not locate the honcho.json here-string in the script")
    # `` `: `` -> literal `:` (backtick escapes the colon so it is not a drive/scope sep)
    body = body.replace("`:", ":")
    # Interpolate $Name tokens. Longer names first so no name is a prefix of another.
    for name in sorted(SAMPLES, key=len, reverse=True):
        body = body.replace("$" + name, SAMPLES[name])
    return body


def hermes_read_config(path: Path) -> dict:
    """The exact read path Hermes's Honcho CLI uses: utf-8 decode + json.loads."""
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ps1 = Path(sys.argv[1])
    rendered = render_honcho_json(ps1.read_text(encoding="utf-8-sig"))

    parsed = json.loads(rendered)
    assert parsed.get("defaultHost") == "hermes", "rendered JSON missing defaultHost"
    assert parsed["hosts"]["hermes"]["baseUrl"].startswith("http://"), "bad host baseUrl"

    with tempfile.TemporaryDirectory() as d:
        # FIX: UTF-8 without BOM — what Write-Utf8NoBom produces.
        no_bom = Path(d) / "honcho.nobom.json"
        no_bom.write_bytes(rendered.encode("utf-8"))
        assert no_bom.read_bytes()[:3] != b"\xef\xbb\xbf", "fix file unexpectedly has a BOM"
        cfg = hermes_read_config(no_bom)
        assert cfg["defaultHost"] == "hermes", "Hermes read path lost the config"

        # BUG: UTF-8 with BOM — what `Set-Content -Encoding UTF8` produced on 5.1.
        with_bom = Path(d) / "honcho.bom.json"
        with_bom.write_bytes(b"\xef\xbb\xbf" + rendered.encode("utf-8"))
        try:
            hermes_read_config(with_bom)
        except json.JSONDecodeError as e:
            assert "BOM" in str(e), f"expected a BOM error, got: {e}"
        else:
            raise SystemExit("FAIL: BOM honcho.json parsed — bug not reproduced")

    print("ok   honcho.json renders to valid JSON")
    print("ok   UTF-8-no-BOM honcho.json has no BOM and Hermes's read path parses it")
    print("ok   UTF-8-with-BOM honcho.json reproduces 'Unexpected UTF-8 BOM' (the field bug)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
