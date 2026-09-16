#!/usr/bin/env bash
# test_windows_worker_install.sh — regression guard for the Windows worker
# installer (scripts/windows-vm/install-worker.ps1).
#
# The worker script sets its own $HermesHome and then bootstraps the official
# Hermes installer. The official installer ALSO declares $HermesHome, and
# PowerShell variable names are case-insensitive. Running the fetched installer
# with Invoke-Expression executes it in the worker's already-optimized script
# scope, which fails at runtime with:
#   "Cannot overwrite variable HermesHome because the variable has been optimized."
# The fix runs the official installer in an isolated child scope via a fresh
# scriptblock invoked with the call operator (&). These checks assert we never
# regress back to current-scope Invoke-Expression and that isolated execution
# stays in place.
#
# It also guards the Windows gateway registration fix: the worker must pin
# $env:HERMES_HOME (Hermes defaults to %LOCALAPPDATA%\hermes on Windows, not
# %USERPROFILE%\.hermes, which stranded the config/.env we wrote and made the
# gateway report "DISCORD_BOT_TOKEN missing"), and it must register the logon task
# through `hermes gateway install` — which resolves the full DOMAIN\user identity
# and an explicit InteractiveToken principal — never a hand-rolled
# Register-ScheduledTask with a bare '$env:USERNAME' logon trigger (that failed with
# "The parameter is incorrect. (…):UserId"). These checks pin the correct idioms and
# forbid the buggy ones.
#
# LIMITATION: no PowerShell runtime (pwsh) is available in this environment and
# installing one requires network + privilege, which repo policy forbids. This
# is therefore a static/source-level test, not a live PowerShell parse or
# repro. It is intentionally strict about the exact idioms so a live rerun of
# the collision cannot silently return.
# No `pipefail`: checks use `code | grep -Eq …`, and `grep -q` closes the pipe on its
# first match, which SIGPIPEs the upstream `code` (grep -Ev) — under pipefail that 141
# would spuriously fail an otherwise-matching check. We want grep's exit status, not the
# producer's. Each check is a self-contained boolean, so pipefail buys nothing here.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

ps1="scripts/windows-vm/install-worker.ps1"

pass=0; fail=0
ck() { if eval "$2"; then printf '  ok   %s\n' "$1"; pass=$((pass+1)); else printf '  FAIL %s\n' "$1"; fail=$((fail+1)); fi; }

ck "installer script exists" "[[ -f '$ps1' ]]"

# Executable lines only (drop PowerShell line comments) so guards match real
# code, not the comment that documents the fix.
code() { grep -Ev '^\s*#' "$ps1"; }

# The remote installer must be fetched into a variable, not executed inline.
ck "fetches official installer with Invoke-RestMethod" \
   "grep -Eq 'Invoke-RestMethod +https://hermes-agent\.nousresearch\.com/install\.ps1' '$ps1'"

# Root-cause guards: no current-scope execution of the fetched installer.
ck "no Invoke-Expression in executable code" \
   "! code | grep -Eiq '\bInvoke-Expression\b'"
ck "no iex alias in executable code" \
   "! code | grep -Eiq '(^|[^-[:alnum:]])iex([^-[:alnum:]]|$)'"
# Dot-sourcing a scriptblock/file would also run it in the current scope.
ck "installer is not dot-sourced into current scope" \
   "! grep -Eq '^\s*\.\s+\(?\[scriptblock\]|^\s*\.\s+.*install\.ps1' '$ps1'"

# Isolated execution: build a fresh scriptblock and invoke it with the call
# operator, which gives the installer its own child scope.
ck "creates a scriptblock from the fetched text" \
   "grep -Eq '\[scriptblock\]::Create\(' '$ps1'"
ck "invokes the scriptblock with the call operator (&)" \
   "grep -Eq '&\s*\(\s*\[scriptblock\]::Create\(' '$ps1'"

# The official installer runs its setup wizard (Nous Portal sign-in) unless
# -SkipSetup is passed. This stack self-hosts the model API and writes its own
# CLIProxyAPI config.yaml right after, so the wizard must never run: it would
# stall the unattended install on an interactive portal login. Assert the
# fetched installer scriptblock is invoked WITH -SkipSetup.
ck "invokes the installer scriptblock with -SkipSetup" \
   "grep -Eq '\[scriptblock\]::Create\([^)]*\)\)\s+-SkipSetup' '$ps1'"
ck "-SkipSetup is present in executable code" \
   "code | grep -Eq '\-SkipSetup'"

# Nothing on our path may launch the setup wizard or the Nous Portal: no
# 'hermes setup', 'hermes portal', or 'hermes login' anywhere in executable code.
ck "no 'hermes setup' invocation in executable code" \
   "! code | grep -Eiq 'hermes\s+setup\b'"
ck "no 'hermes portal' invocation in executable code" \
   "! code | grep -Eiq 'hermes\s+portal\b'"
ck "no 'hermes login' invocation in executable code" \
   "! code | grep -Eiq 'hermes\s+login\b'"

# Computer use must be installed AND verified. The official installer's own
# cua-driver step is best-effort and its "install succeeded but runtime is not
# compatible" warning is easy to miss, so our path installs then checks the
# health matrix with 'hermes computer-use doctor', and telemetry stays off.
ck "installs the computer-use driver" \
   "code | grep -Eq 'hermes\s+computer-use\s+install'"
ck "verifies computer use with the doctor" \
   "code | grep -Eq 'hermes\s+computer-use\s+doctor'"
ck "keeps cua telemetry disabled in rendered config" \
   "grep -Eq 'cua_telemetry:\s*false' '$ps1'"
ck "surfaces an unhealthy computer-use result (does not hide it)" \
   "grep -Eiq 'Write-Warning.*[Cc]omputer' '$ps1'"

# The worker still manages its own Hermes home (the variable that collided).
ck "worker still defines its own \$HermesHome" \
   "grep -Eq '\\\$HermesHome\s*=\s*Join-Path' '$ps1'"

# Hermes on Windows defaults HERMES_HOME to %LOCALAPPDATA%\hermes, not
# %USERPROFILE%\.hermes. The worker writes config.yaml/.env under
# %USERPROFILE%\.hermes, so it MUST pin $env:HERMES_HOME to that same home or the
# gateway boots against an empty %LOCALAPPDATA%\hermes and reports
# "DISCORD_BOT_TOKEN missing" with an unmigrated config. Assert the pin exists and
# is set before the official installer and gateway install run against it.
ck "pins \$env:HERMES_HOME to the worker's Hermes home" \
   "code | grep -Eq '\\\$env:HERMES_HOME\s*=\s*\\\$HermesHome'"
ck "pins HERMES_HOME before installing the official Hermes CLI" \
   "[[ \$(grep -n '\\\$env:HERMES_HOME\s*=\s*\\\$HermesHome' '$ps1' | head -1 | cut -d: -f1) -lt \$(grep -n 'install\.ps1' '$ps1' | head -1 | cut -d: -f1) ]]"

# Scheduled-task registration: use Hermes's own Windows installer, never a
# hand-rolled Register-ScheduledTask. The old code registered a logon trigger with a
# bare '$env:USERNAME' and no principal, which fails with
# "Register-ScheduledTask : The parameter is incorrect. (…):UserId:<user>", then
# started the task regardless. These guards keep that class of bug from returning.
ck "installs the gateway via the native Hermes installer" \
   "code | grep -Eq 'hermes\s+gateway\s+install'"
ck "gateway install is non-interactive (start-on-login + start-now)" \
   "code | grep -Eq 'hermes\s+gateway\s+install(\s+--start-on-login|\s+--start-now){2}'"
ck "does not hand-roll Register-ScheduledTask" \
   "! code | grep -Eiq 'Register-ScheduledTask'"
ck "no bare \$env:USERNAME logon trigger" \
   "! code | grep -Eiq 'New-ScheduledTaskTrigger.*-User\s+\\\$env:USERNAME'"
ck "does not New-ScheduledTaskTrigger with a bare username at all" \
   "! code | grep -Eiq 'New-ScheduledTaskTrigger'"
ck "does not invoke the gateway with the bare 'gateway' subcommand" \
   "! code | grep -Eq '\-Argument\s+\"gateway\"'"

# Register-before-start / verify: the install command registers then starts, and the
# script must verify registration (hard failure) before trusting the gateway.
ck "hard-fails when gateway install returns nonzero" \
   "code | grep -Eq 'hermes\s+gateway\s+install' && code | grep -Eiq 'throw.*gateway install'"
ck "verifies the gateway task after install (gateway status)" \
   "code | grep -Eq 'hermes\s+gateway\s+status'"
ck "throws when the gateway task did not register" \
   "code | grep -Eiq 'throw.*(did not register|register)'"

# Browser tools: config selects browser-use (headed), which needs agent-browser +
# a Playwright Chromium build. The installer provisions them best-effort (non-fatal).
ck "provisions the agent-browser CLI for browser tools" \
   "code | grep -Eq 'npm install -g agent-browser'"
ck "installs the agent-browser Chromium build" \
   "code | grep -Eq 'agent-browser install'"
# `hermes doctor` checks for a Playwright "chromium-*" build in the ms-playwright cache,
# not agent-browser's bundled Chrome. Install the exact Chromium doctor looks for with
# `npx playwright install chromium`, run from the Hermes repo (matches its Playwright pin).
ck "installs the Playwright Chromium that doctor checks for" \
   "code | grep -Eq 'npx.*playwright install chromium'"
ck "runs the Playwright Chromium install from the Hermes repo dir" \
   "code | grep -Eq 'Join-Path \\\$HermesHome \"hermes-agent\"' && code | grep -Eq 'Push-Location \\\$hermesRepo'"

# Model provider credentials: the model is a custom OpenAI-compatible endpoint
# (CLIProxyAPI). `hermes doctor` scans ~/.hermes/.env for KNOWN provider names
# (OPENAI_API_KEY / OPENAI_BASE_URL, ...); a non-standard CLIPROXY_API_KEY resolves at
# runtime but makes doctor report "No API key found in ~/.hermes/.env". Assert the .env
# uses the recognised names and config references them as literal ${VAR} placeholders.
ck "writes OPENAI_API_KEY into .env (a name doctor recognises)" \
   "code | grep -Eq '^OPENAI_API_KEY='"
ck "writes OPENAI_BASE_URL into .env (custom endpoint doctor recognises)" \
   "code | grep -Eq '^OPENAI_BASE_URL='"
ck "does not write the non-standard CLIPROXY_API_KEY into .env" \
   "! code | grep -Eq '^CLIPROXY_API_KEY='"
# The config here-string must emit a LITERAL ${OPENAI_API_KEY} for Hermes to resolve
# from .env — a backtick escapes the $ so PowerShell does not interpolate it.
ck "config references the OPENAI_API_KEY placeholder" \
   "grep -Eq 'OPENAI_API_KEY}' '$ps1'"
ck "config api_key placeholder is backtick-escaped (emits a literal env reference)" \
   "grep -Eq 'api_key: \`\\\$' '$ps1'"
# Executable code (not the explanatory comments) must be free of CLIPROXY_API_KEY.
ck "config/.env no longer reference CLIPROXY_API_KEY in executable code" \
   "! code | grep -Eq 'CLIPROXY_API_KEY'"
ck "does not put the real key literally in config.yaml (only a placeholder)" \
   "! code | grep -Eq 'api_key:\s*\\\$CliProxyApiKey'"

# Config migration: plain 'hermes doctor' only REPORTS a version drift; only
# 'hermes doctor --fix' migrates (migrate_config(interactive=False)). Assert the final
# health check uses --fix, that it runs AFTER the config.yaml is written, and that no
# interactive migration path ('hermes config migrate' is interactive) is used.
ck "runs 'hermes doctor --fix' to migrate the config schema" \
   "code | grep -Eq 'hermes\s+doctor\s+--fix'"
ck "config migration runs after config.yaml is written" \
   "[[ \$(grep -n 'config.yaml' '$ps1' | head -1 | cut -d: -f1) -lt \$(grep -n 'hermes\s\+doctor\s\+--fix' '$ps1' | head -1 | cut -d: -f1) ]]"
ck "does not use the interactive 'hermes config migrate'" \
   "! code | grep -Eiq 'hermes\s+config\s+migrate'"

# Shared Honcho (only with -UseHostHoncho): honcho.json must pin the active host with
# defaultHost AND enable the host block, the installer must flip it on through the CLI
# when that subcommand exists, verify the provider with non-interactive status checks,
# and fail clearly (not just warn) when -UseHostHoncho was requested but Honcho is down.
ck "honcho.json pins defaultHost: hermes (the active host block)" \
   "grep -Eq '\"defaultHost\":\s*\"hermes\"' '$ps1'"
ck "honcho.json enables the hermes host block" \
   "grep -Eq '\"enabled\":\s*true' '$ps1'"
ck "enables the host Honcho through the CLI (hermes honcho enable)" \
   "code | grep -Eq 'hermes\s+honcho\s+enable'"
ck "verifies shared Honcho with 'hermes honcho status'" \
   "code | grep -Eq 'hermes\s+honcho\s+status'"
ck "verifies shared Honcho with 'hermes memory status'" \
   "code | grep -Eq 'hermes\s+memory\s+status'"
ck "fails clearly (throws) when -UseHostHoncho stays disabled/unreachable" \
   "code | grep -Eiq 'throw.*Honcho'"

# baseUrl must sit INSIDE the hermes host block (host-block fields win and are read
# first; the active Hermes 0.21.2 build keys availability off the host block). A base
# URL stranded only at the root left Honcho reported disabled / "no base URL configured".
# The root copy stays for back-compat, so this asserts the host-block spelling exists.
ck "honcho.json sets baseUrl inside the hermes host block" \
   "grep -Eq '\"hermes\":\s*\{[^}]*\"baseUrl\"' '$ps1'"

# honcho-ai install path split (the exact "honcho-ai is not installed" symptom):
# HERMES_HOME is %USERPROFILE%\.hermes but the Hermes checkout+venv default to
# %LOCALAPPDATA%\hermes\hermes-agent, so a hardcoded $HermesHome\hermes-agent\venv
# Test-Path is false and honcho-ai never installs. The installer must DISCOVER the
# runtime interpreter (launcher + known roots) and install into that, not the
# HERMES_HOME subtree alone.
ck "discovers the Hermes runtime Python instead of guessing one path" \
   "grep -Eq 'function Get-HermesRuntimePython' '$ps1'"
ck "runtime discovery considers the %LOCALAPPDATA%\\hermes checkout (the path split)" \
   "code | grep -Eq '\\\$env:LOCALAPPDATA\\\\hermes\\\\hermes-agent'"
ck "runtime discovery follows the on-PATH hermes launcher" \
   "code | grep -Eq 'Get-Command hermes' && code | grep -Eq 'Get-Content -LiteralPath'"
ck "installs honcho-ai into the DISCOVERED runtime python" \
   "code | grep -Eq '\\\$venvPy\s*=\s*Get-HermesRuntimePython'"

# The Hermes runtime venv is intentionally PIP-LESS: `python.exe -m pip install`
# fails with the exact Windows symptom "No module named pip". Hermes ships `uv` on
# Windows, so honcho-ai MUST be installed with `uv pip install --python <interpreter>`
# first. `python -m pip` may only be reached as a guarded fallback AFTER an `ensurepip`
# bootstrap succeeds — never assumed to work on its own.
ck "installs honcho-ai uv-first (uv pip install --python <interpreter>)" \
   "code | grep -Eq 'uv pip install --python'"
ck "does not run an unconditional 'python -m pip install' at the honcho call site" \
   "! code | grep -Eq '\\\$venvPy\s+-m\s+pip\s+install'"
ck "reaches pip only through an ensurepip bootstrap (never bare pip)" \
   "! code | grep -Eq '\-m pip install' || code | grep -Eq '\-m ensurepip'"
ck "ensurepip bootstrap precedes any pip install fallback" \
   "! code | grep -Eq '\-m pip install' || [[ \$(grep -nE '\-m ensurepip' '$ps1' | head -1 | cut -d: -f1) -lt \$(grep -nE '\-m pip install' '$ps1' | head -1 | cut -d: -f1) ]]"
# A robust fallback that does not assume pip: `hermes honcho setup` installs the same pin.
ck "falls back to 'hermes honcho setup' when uv is unavailable" \
   "code | grep -Eq 'hermes\s+honcho\s+setup'"
ck "warns when uv is not found before falling back" \
   "code | grep -Eiq 'Write-Warning.*uv'"

ck "does not hardcode the HERMES_HOME venv as the sole honcho-ai target" \
   "! code | grep -Eq '\\\$venvPy\s*=\s*Join-Path\s+\\\$HermesHome\s+\"hermes-agent'"
ck "pins the honcho-ai version hermes honcho setup installs" \
   "code | grep -Eq 'honcho-ai==2\.2\.0'"

# Import verification must capture the exit status WITHOUT letting the Python traceback
# on stderr become a terminating NativeCommandError under $ErrorActionPreference='Stop'
# (that aborted the install before Chrome MCP). A probe helper drops to 'Continue',
# swallows both streams, and keys success off $LASTEXITCODE only.
ck "verifies honcho-ai via a non-terminating probe helper (Test-PythonImport)" \
   "grep -Eq 'function Test-PythonImport' '$ps1'"
ck "the honcho install/verify path invokes the probe helper" \
   "code | grep -Eq 'Test-PythonImport\s+\\\$P'"
ck "the import probe runs python -c \"import <module>\"" \
   "code | grep -Eq '\-c\s+\"import '"
ck "the import probe lowers \$ErrorActionPreference so native stderr is not terminating" \
   "code | grep -Eq \"ErrorActionPreference\s*=\s*'Continue'\""
ck "the import probe keys success off the process exit code" \
   "code | grep -Eq 'LASTEXITCODE\s+-eq\s+0'"
ck "no bare 'import honcho' probe left under the Stop preference (2>\$null idiom removed)" \
   "! code | grep -Eq '\\\$venvPy\s+-c\s+\"import honcho\"\s+2>\\\$null'"
ck "warns clearly when the runtime python or honcho-ai import cannot be resolved" \
   "code | grep -Eiq 'Write-Warning.*honcho-ai'"

# Usage example demonstrates a trailing backtick continuation before
# -UseHostHoncho on its own line.
ck "-UseHostHoncho shown on a continued line" \
   "grep -Eq '^\s*-UseHostHoncho\s*\$' '$ps1'"
ck "line before -UseHostHoncho ends with a backtick" \
   "grep -B1 -E '^\s*-UseHostHoncho\s*\$' '$ps1' | grep -Eq '\`\s*\$'"

# UTF-8-no-BOM for GENERATED config files. Windows PowerShell 5.1's
# `Set-Content -Encoding UTF8` writes a leading BOM (EF BB BF). Hermes reads
# honcho.json with `read_text(encoding='utf-8')` + json.loads, and json.loads
# rejects a leading U+FEFF ("Unexpected UTF-8 BOM"), which the Honcho CLI swallows
# as {} — so a BOM'd honcho.json reports "No Honcho config found" right after the
# installer wrote it. The .env/config.yaml share the hazard. The fix routes every
# generated file through a Write-Utf8NoBom helper (UTF8Encoding($false), no BOM).
# The .ps1 scripts themselves must KEEP their BOM (test_windows_ps1_encoding.sh) —
# that is a different concern (CP1252 misread), so this only forbids the encoded
# write for the three generated files.
ck "defines a Write-Utf8NoBom helper for generated config files" \
   "grep -Eq 'function Write-Utf8NoBom' '$ps1'"
ck "the helper writes UTF-8 WITHOUT a BOM (UTF8Encoding \$false + WriteAllText)" \
   "code | grep -Eq 'New-Object System\.Text\.UTF8Encoding\(\s*\\\$false' && code | grep -Eq '\[System\.IO\.File\]::WriteAllText'"
ck "writes honcho.json via Write-Utf8NoBom (not Set-Content -Encoding UTF8)" \
   "code | grep -Eq 'Write-Utf8NoBom.*honcho\.json'"
ck "writes config.yaml via Write-Utf8NoBom" \
   "code | grep -Eq 'Write-Utf8NoBom.*config\.yaml'"
ck "writes .env via Write-Utf8NoBom" \
   "code | grep -Eq 'Write-Utf8NoBom.*\.env'"
ck "no generated config file is written with Set-Content -Encoding UTF8" \
   "! code | grep -Eiq 'Set-Content.*-Encoding\s+UTF8'"
ck "no generated config file is written with Out-File -Encoding utf8 (also BOM on 5.1)" \
   "! code | grep -Eiq 'Out-File.*-Encoding\s+utf8'"
# End-to-end proof: render the real honcho.json here-string and show the fix's
# UTF-8-no-BOM write parses on Hermes's exact read path while the old BOM write
# reproduces "Unexpected UTF-8 BOM". Uses synthetic sample values only.
ck "rendered honcho.json is BOM-free and Hermes-parseable (BOM form reproduces the bug)" \
   "python3 scripts/lib/honcho_bom_repro.py '$ps1'"

# Secrets must never be echoed by the script.
ck "does not Write-Host the Discord bot token" \
   "! grep -Eq 'Write-(Host|Output|Warning).*\\\$DiscordBotToken' '$ps1'"
ck "does not Write-Host the CLIProxy API key" \
   "! grep -Eq 'Write-(Host|Output|Warning).*\\\$CliProxyApiKey' '$ps1'"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
