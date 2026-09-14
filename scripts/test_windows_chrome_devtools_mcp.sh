#!/usr/bin/env bash
# test_windows_chrome_devtools_mcp.sh — regression guard for the Chrome DevTools
# MCP setup on the Windows Hermes worker.
#
# The worker runs a browser-only Chrome DevTools MCP server LOCALLY inside the
# Windows VM, attached to a dedicated, signed-in Chrome automation profile over a
# LOOPBACK-only remote debugging port. This exercises three source artifacts:
#   - scripts/windows-vm/install-worker.ps1        (writes mcp_servers, verifies, restarts)
#   - scripts/windows-vm/start-chrome-automation.ps1 (loopback-only debuggable Chrome)
#   - hermes/profiles/windows-operator/SOUL.md      (tool routing rules)
#
# Design mirrors test_windows_worker_install.sh: exact-idiom static checks. It is
# strict about the exact strings so a regression (LAN-exposed CDP, telemetry back
# on, a personal profile targeted, a silent "installed == working" claim) cannot
# return unnoticed.
#
# LIMITATION: no PowerShell runtime (pwsh) exists in this environment and
# installing one needs network + privilege, which repo policy forbids. These are
# therefore static/source-level checks, not a live PowerShell parse or a real MCP
# handshake. The authoritative runtime probe is `hermes mcp test chrome-devtools`,
# which the installer runs on the Windows host (asserted below).
#
# No `pipefail`: checks use `code | grep -Eq …`; `grep -q` closes the pipe on its
# first match and SIGPIPEs the upstream producer, which under pipefail would
# spuriously fail an otherwise-matching check. Each check is a self-contained
# boolean, so pipefail buys nothing.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

ps1="scripts/windows-vm/install-worker.ps1"
launcher="scripts/windows-vm/start-chrome-automation.ps1"
soul="hermes/profiles/windows-operator/SOUL.md"
doc="docs/windows-vm-worker.md"

pass=0; fail=0
ck() { if eval "$2"; then printf '  ok   %s\n' "$1"; pass=$((pass+1)); else printf '  FAIL %s\n' "$1"; fail=$((fail+1)); fi; }

ck "installer script exists" "[[ -f '$ps1' ]]"
ck "chrome automation launcher exists" "[[ -f '$launcher' ]]"
ck "windows-operator SOUL exists" "[[ -f '$soul' ]]"

# Executable lines only (drop PowerShell line comments) so guards match real code,
# not the comment that documents the decision.
code() { grep -Ev '^\s*#' "$ps1"; }
lcode() { grep -Ev '^\s*#' "$launcher"; }

# ---------------------------------------------------------------- opt-in ---
# Chrome DevTools MCP is OFF unless the operator explicitly opts in. CDP hands the
# agent live tabs, cookies and storage of the target profile, so it must never turn
# on implicitly.
ck "installer gates the MCP behind an explicit -EnableChromeDevToolsMcp switch" \
   "grep -Eiq '\[switch\]\s*\\\$EnableChromeDevToolsMcp' '$ps1'"
ck "installer takes a dedicated -ChromeUserDataDir parameter" \
   "grep -Eiq '\\\$ChromeUserDataDir' '$ps1'"
ck "installer takes a -ChromeDebugPort parameter" \
   "grep -Eiq '\\\$ChromeDebugPort' '$ps1'"
# Enabling the MCP without naming the dedicated profile must fail clearly — never
# silently fall back to a default/personal Chrome profile.
ck "throws when MCP is enabled without a dedicated ChromeUserDataDir" \
   "code | grep -Eiq 'throw.*ChromeUserDataDir'"

# ---------------------------------------------------------- exact config ---
# The mcp_servers block Hermes reads at gateway startup. Server key chrome-devtools,
# stdio transport via npx.cmd (native Windows Node shim), official package.
ck "config declares an mcp_servers block" \
   "grep -Eq 'mcp_servers:' '$ps1'"
ck "config registers the chrome-devtools server" \
   "grep -Eq 'chrome-devtools:' '$ps1'"
ck "MCP command uses the native Windows npx.cmd shim" \
   "grep -Eq 'command:\s*npx\.cmd' '$ps1'"
ck "MCP runs the official chrome-devtools-mcp package" \
   "grep -Eq 'chrome-devtools-mcp@latest' '$ps1'"

# --------------------------------------------------- loopback-only / no LAN ---
# CDP must reach ONLY local Chrome on loopback. Never LAN, never the tailnet.
ck "MCP connects to Chrome over --browser-url" \
   "grep -Eq -- '--browser-url' '$ps1'"
ck "browser-url targets loopback (127.0.0.1)" \
   "grep -Eq 'http://127\.0\.0\.1:' '$ps1'"
ck "MCP browser-url is never 0.0.0.0" \
   "! grep -Eq 'browser-url[^0-9]*0\.0\.0\.0' '$ps1'"
# No Linux->Windows CDP listener, and no tailnet/LAN bind anywhere in the MCP path.
ck "installer never binds CDP to 0.0.0.0" \
   "! code | grep -Eq 'remote-debugging-address[= ]+0\.0\.0\.0'"
ck "installer does not point the MCP at the host tailnet address" \
   "! grep -Eq 'browser-url[^\"]*\\\$HostAddress' '$ps1'"

# ------------------------------------------------------ no usage telemetry ---
ck "MCP disables usage statistics with --no-usage-statistics" \
   "grep -Eq -- '--no-usage-statistics' '$ps1'"
ck "MCP env also pins CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS off" \
   "grep -Eq 'CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS' '$ps1'"

# ------------------------------------------------------------- no secrets ---
# The MCP block must not embed the CLIProxy key or the Discord token, and nothing
# on the MCP path may echo a secret.
ck "MCP config does not embed the CLIProxy API key" \
   "! grep -Eq 'chrome-devtools-mcp.*CliProxyApiKey' '$ps1'"
ck "does not Write-Host the ChromeUserDataDir as a secret dump of the profile" \
   "! grep -Eq 'Write-(Host|Output).*\\\$CliProxyApiKey' '$ps1'"

# ------------------------------------------------- explicit CDP disclosure ---
# The operator must be warned, in plain words, what CDP exposes before it is armed.
ck "installer warns that CDP exposes live tabs, cookies and storage" \
   "grep -Eiq 'Write-Warning.*(cookie|tabs|storage)' '$ps1'"
ck "installer tells the operator to use a dedicated, not personal, profile" \
   "grep -Eiq '(dedicated).*profile' '$ps1'"

# -------------------------------------------------------- verify, not assume ---
# `hermes mcp test` is the authoritative runtime probe: it starts the server and
# lists its tools. The install must run it AND `hermes mcp list`, and must FAIL
# clearly when the server cannot start — never claim success from writing config.
ck "verifies the MCP with 'hermes mcp test chrome-devtools'" \
   "code | grep -Eq 'hermes\s+mcp\s+test\s+chrome-devtools'"
ck "confirms the server is present with 'hermes mcp list'" \
   "code | grep -Eq 'hermes\s+mcp\s+list'"
ck "throws when the MCP cannot start (does not claim success from install alone)" \
   "code | grep -Eiq 'throw.*([Mm][Cc][Pp]|chrome-devtools)'"

# ------------------------------------------------ gateway restart after change ---
# The running gateway loads MCP tools at startup, so a config change requires a
# restart. It must run AFTER the config is written, and only for the MCP path.
ck "restarts the gateway so it reloads MCP tools" \
   "code | grep -Eq 'hermes\s+gateway\s+restart'"
ck "gateway restart runs after the mcp_servers config is written" \
   "[[ \$(grep -n 'mcp_servers:' '$ps1' | head -1 | cut -d: -f1) -lt \$(grep -n 'hermes\s\+gateway\s\+restart' '$ps1' | head -1 | cut -d: -f1) ]]"

# =============================================== launcher: loopback-only Chrome ===
ck "launcher takes a UserDataDir parameter" \
   "grep -Eiq '\\\$UserDataDir' '$launcher'"
ck "launcher opens a remote debugging port" \
   "grep -Eq -- '--remote-debugging-port' '$launcher'"
ck "launcher points Chrome at the dedicated user-data-dir" \
   "grep -Eq -- '--user-data-dir' '$launcher'"
# The bind address is set explicitly, defaults to loopback, and the launcher refuses
# anything else — so the effective bind is always 127.0.0.1.
ck "launcher sets an explicit remote-debugging-address" \
   "grep -Eq -- '--remote-debugging-address=' '$launcher'"
ck "launcher defaults the bind address to loopback (127.0.0.1)" \
   "grep -Eq 'BindAddress\s*=\s*\"127\.0\.0\.1\"' '$launcher'"
ck "launcher never contains a 0.0.0.0 bind anywhere (not even in comments)" \
   "! grep -Eq '0\.0\.0\.0' '$launcher'"
ck "launcher hard-refuses a non-loopback debugging address" \
   "lcode | grep -Eiq 'throw'"
# The guard compares against loopback and throws for anything else.
ck "launcher guard rejects a non-loopback bind address" \
   "lcode | grep -Eq 'BindAddress -ne \"127\.0\.0\.1\"'"

# ==================================================== SOUL: tool routing ===
# Discord inventory/admin: REST from the host orchestrator, never GUI scrolling.
ck "SOUL routes Discord inventory/admin to the REST API from the host orchestrator" \
   "grep -Eiq 'Discord.*REST' '$soul'"
ck "SOUL forbids GUI scrolling for Discord server inventory/admin" \
   "grep -Eiq '(no|not|never|avoid).*(GUI|scroll)' '$soul'"
# Browser-only apps: Chrome DevTools MCP / DOM tools.
ck "SOUL routes browser-only apps to Chrome DevTools MCP / DOM tools" \
   "grep -Eiq 'Chrome DevTools MCP' '$soul'"
# computer_use: native dialogs, browser chrome, non-web apps.
ck "SOUL reserves computer_use for native dialogs / browser chrome / non-web apps" \
   "grep -Eiq 'computer_use.*(native|dialog|browser chrome|non-web)' '$soul'"
# Plan-first and approval rules are preserved.
ck "SOUL preserves the plan-first / approval discipline" \
   "grep -Eiq '(plan first|approv|wait for)' '$soul'"

# ==================================================== docs coverage ===
ck "docs cover the Chrome DevTools MCP setup" \
   "grep -Eiq 'Chrome DevTools MCP' '$doc'"
ck "docs mention the one-time chrome://inspect approval / sign-in step" \
   "grep -Eiq 'chrome://inspect|sign in|sign-in' '$doc'"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
