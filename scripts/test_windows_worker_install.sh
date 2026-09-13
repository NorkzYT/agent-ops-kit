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
set -uo pipefail
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
# Chromium. The installer provisions them best-effort (non-fatal).
ck "provisions the agent-browser CLI for browser tools" \
   "code | grep -Eq 'npm install -g agent-browser'"
ck "installs the Chromium build for browser tools" \
   "code | grep -Eq 'agent-browser install'"

# Usage example demonstrates a trailing backtick continuation before
# -UseHostHoncho on its own line.
ck "-UseHostHoncho shown on a continued line" \
   "grep -Eq '^\s*-UseHostHoncho\s*\$' '$ps1'"
ck "line before -UseHostHoncho ends with a backtick" \
   "grep -B1 -E '^\s*-UseHostHoncho\s*\$' '$ps1' | grep -Eq '\`\s*\$'"

# Secrets must never be echoed by the script.
ck "does not Write-Host the Discord bot token" \
   "! grep -Eq 'Write-(Host|Output|Warning).*\\\$DiscordBotToken' '$ps1'"
ck "does not Write-Host the CLIProxy API key" \
   "! grep -Eq 'Write-(Host|Output|Warning).*\\\$CliProxyApiKey' '$ps1'"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
