#!/usr/bin/env bash
# test_windows_ps1_encoding.sh — regression guard for the Windows PowerShell
# encoding bug that broke `install-worker.ps1`.
#
# THE BUG (fixed): the installer was UTF-8 WITHOUT a BOM and contained an em-dash
# (U+2014 "—") inside a double-quoted string on the Write-Warning line. Windows
# PowerShell 5.1 decodes a BOM-less .ps1 as the ANSI code page (CP1252), so the
# em-dash's bytes E2 80 94 became "â€" + U+201D — and U+201D is a *right curly
# double-quote*, which the PowerShell tokenizer treats as a real string delimiter.
# The string terminated early at "…automation profile only â€", so the next word,
# `never`, became a bareword positional token. Result, for the exact operator
# invocation with -UseHostHoncho -EnableChromeDevToolsMcp -ChromeUserDataDir:
#     install-worker.ps1 : A positional parameter cannot be found that accepts
#     argument 'never'.  (PositionalParameterNotFound,install-worker.ps1)
# pwsh 7.x / any correct-UTF-8 read keeps U+2014 (not a delimiter), which is why
# it only reproduced on the Windows worker.
#
# THE FIX (two independent guards, both asserted here):
#   1. Each windows-vm .ps1 begins with a UTF-8 BOM, so Windows PowerShell 5.1
#      decodes it as UTF-8 rather than CP1252.
#   2. No non-ASCII character sits inside a single-line double-quoted string
#      literal, so even a BOM-stripped file cannot re-trigger the smart-quote
#      corruption. (Non-ASCII in # comments and @"…"@ here-strings is safe:
#      comments ignore quotes and here-strings are not closed by embedded quotes.)
#
# Design mirrors test_windows_worker_install.sh: exact-idiom static checks that a
# regression cannot slip past. A live PowerShell tokenizer proof runs additionally
# when a `pwsh` runtime is available; see LIMITATION.
#
# LIMITATION: CI has no PowerShell runtime, so the always-on checks are static
# (byte-level BOM + AST-free string scan). When `pwsh` is present the script also
# performs the authoritative proof: it re-reads each file as CP1252 (the exact
# Windows-5.1 ANSI misread) and asserts the tokenizer produces no bareword leak
# from an early-terminated string. That live proof is skipped, not failed, when
# pwsh is missing.
#
# No `pipefail`: checks pipe into `grep -q`, which closes the pipe on first match
# and SIGPIPEs the producer; under pipefail that would spuriously fail a matching
# check. Each check is a self-contained boolean, so pipefail buys nothing.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

pass=0; fail=0
ck() { if eval "$2"; then printf '  ok   %s\n' "$1"; pass=$((pass+1)); else printf '  FAIL %s\n' "$1"; fail=$((fail+1)); fi; }

ps1_files=(scripts/windows-vm/install-worker.ps1 scripts/windows-vm/start-chrome-automation.ps1)

for f in "${ps1_files[@]}"; do
  ck "exists: $f" "[[ -f '$f' ]]"

  # ---- guard 1: UTF-8 BOM so Windows PowerShell 5.1 reads UTF-8, not CP1252 ----
  ck "$f begins with a UTF-8 BOM (EF BB BF)" \
     "[[ \"\$(head -c3 '$f' | od -An -tx1 | tr -d ' \n')\" == 'efbbbf' ]]"

  # ---- guard 2: no non-ASCII inside a single-line \"…\" string literal ----
  # A here-string (@\"…\"@) is not closed by embedded quotes and # comments ignore
  # quotes, so only single-line double-quoted strings are dangerous. This scan
  # skips block comments (<# … #>), here-strings, and line comments, then flags any
  # non-ASCII byte that falls inside a \"…\" span on an executable line.
  ck "$f has no non-ASCII char inside a single-line double-quoted string" \
     "python3 scripts/lib/ps1_string_ascii_check.py '$f'"
done

# ---- authoritative live proof (only when a pwsh runtime exists) ----
if command -v pwsh >/dev/null 2>&1; then
  echo "  --   pwsh found: running live CP1252-misread tokenizer proof"
  for f in "${ps1_files[@]}"; do
    # Read the file the way a BOM-less Windows PowerShell 5.1 would (CP1252), then
    # tokenize. The bug's fingerprint is a *bareword* token `never`: it exists only
    # when the em-dash misdecodes to U+201D and closes the Write-Warning string
    # early, spilling the following words into the command line. Em-dashes that live
    # in # comments or @"…"@ here-strings stay inside a single Comment/String token
    # and never surface as barewords, so this discriminates the real defect precisely.
    ck "live: $f read as CP1252 leaves no bareword 'never' from a broken string" \
       "python3 scripts/lib/ps1_cp1252_to_utf8bom.py '$f' /tmp/_ps1sim.ps1 && \
        pwsh -NoProfile -Command '
          \$t=\$null;\$e=\$null
          [System.Management.Automation.Language.Parser]::ParseFile(\"/tmp/_ps1sim.ps1\",[ref]\$t,[ref]\$e) | Out-Null
          \$leak = \$t | Where-Object { \$_.Text -eq \"never\" -and \$_.Kind -in @(\"Identifier\",\"Generic\") }
          if (\$leak) { exit 1 } else { exit 0 }
        '"
  done
  rm -f /tmp/_ps1sim.ps1
else
  echo "  skip pwsh not installed — live tokenizer proof skipped (static checks stand)"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
