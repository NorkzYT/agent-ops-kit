<#
.SYNOPSIS
  Install the Hermes "windows-operator" worker inside the Windows VM.

.DESCRIPTION
  Installs Hermes, points it at the model API on your Linux host (CLIProxyAPI),
  enables the computer_use toolset, installs the cua driver, seeds the
  windows-operator persona and registers a logon task that starts the Discord
  gateway on the interactive desktop.

  Memory defaults to Hermes's local built-in memory. Pass -UseHostHoncho only
  when you have chosen to share the host's unauthenticated Honcho over Tailscale.

  For browser-only apps, pass -EnableChromeDevToolsMcp with a dedicated
  -ChromeUserDataDir to arm the Chrome DevTools MCP against a signed-in automation
  Chrome over loopback-only remote debugging. It is opt-in because CDP exposes that
  profile's live tabs, cookies and storage. See docs/windows-vm-worker.md.

  Run from an elevated PowerShell in the VM's console/RDP session (not SSH):
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\install-worker.ps1 -HostAddress 100.64.0.1 -CliProxyApiKey <key> `
        -DiscordBotToken <token> -AllowedUsers 282100214024896522

  To share the host's Honcho, add -UseHostHoncho on a continued line. End each
  preceding line with a backtick so PowerShell joins the continuation:
    .\install-worker.ps1 -HostAddress 100.64.0.1 -CliProxyApiKey <key> `
        -DiscordBotToken <token> -AllowedUsers 282100214024896522 `
        -UseHostHoncho

  Host prerequisites (do these on the Linux host, NOT here — this script never
  configures the host's Tailscale):
    - The stack is up with Docker bound to 127.0.0.1 (the default): `make up`.
    - CLIProxyAPI (and Honcho, only if you pass -UseHostHoncho) are published to
      the tailnet with Tailscale Serve: `make windows-vm-network`. That keeps the
      containers on loopback and forwards :8317 (and :8000) over the tailnet.
    - This VM's Tailscale node is allowed by the host's ACLs to reach :8317
      (and :8000 for host Honcho).
  Do NOT bind Docker to the Tailscale IP or 0.0.0.0. See docs/windows-vm-worker.md.

  Before any expensive setup, this script probes HostAddress:CliProxyPort (and
  HostAddress:HonchoPort when -UseHostHoncho) and stops with a clear message if
  the host is not reachable, so you fix the host once rather than after a long install.
#>
param(
  [Parameter(Mandatory = $true)] [string] $HostAddress,
  [Parameter(Mandatory = $true)] [string] $CliProxyApiKey,
  [Parameter(Mandatory = $true)] [string] $DiscordBotToken,
  [Parameter(Mandatory = $true)] [string] $AllowedUsers,
  [string] $Model = "gpt-5.6-sol",
  [int]    $CliProxyPort = 8317,
  [int]    $HonchoPort = 8000,
  [string] $PeerName = "me",
  [string] $Workspace = "agent-ops",
  [string] $HomeChannel = "",
  [switch] $UseHostHoncho,
  # Chrome DevTools MCP (browser-only apps). OFF unless explicitly enabled: CDP hands
  # the agent live tabs, cookies and storage of the target profile, so it is opt-in and
  # requires you to name the dedicated automation profile. See docs/windows-vm-worker.md.
  [switch] $EnableChromeDevToolsMcp,
  [string] $ChromeUserDataDir = "",
  [int]    $ChromeDebugPort = 9222,
  [string] $ChromeProfileDirectory = "",
  [string] $ChromeExePath = ""
)
$ErrorActionPreference = "Stop"

# Chrome DevTools MCP is an explicit, security-sensitive opt-in. Validate the grant and
# warn about what CDP exposes BEFORE any config is written, so the operator confirms the
# dedicated profile rather than a personal one. The mcp_servers block is only emitted
# into config.yaml when this passes.
$mcpServersBlock = ""
if ($EnableChromeDevToolsMcp) {
  if (-not $ChromeUserDataDir) {
    throw "-EnableChromeDevToolsMcp requires -ChromeUserDataDir pointing at a DEDICATED Chrome automation profile. It never defaults to a personal profile, because CDP exposes that profile's live tabs, cookies and storage."
  }
  Write-Warning "[agent-ops-kit] Chrome DevTools MCP arms the Chrome DevTools Protocol against '$ChromeUserDataDir'. CDP exposes that profile's live tabs, cookies and storage to the agent. Use a dedicated automation profile only -- never a personal Chrome profile, and never a profile with banking or password-manager sign-ins."
  $profileLine = ""
  # `--browser-url` connects to the dedicated Chrome the launcher starts on LOOPBACK
  # (127.0.0.1) only — CDP is never exposed to the LAN or the tailnet, and there is no
  # Linux->Windows CDP listener. `--no-usage-statistics` (and the env var, belt and
  # suspenders) keeps telemetry off. npx.cmd is the native Windows Node shim.
  $mcpServersBlock = @"

mcp_servers:
  chrome-devtools:
    command: npx.cmd
    args:
      - "-y"
      - "chrome-devtools-mcp@latest"
      - "--browser-url"
      - "http://127.0.0.1:$ChromeDebugPort"
      - "--no-usage-statistics"
    env:
      CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS: "1"
    connect_timeout: 60
    timeout: 180
"@
}

$HermesHome = Join-Path $env:USERPROFILE ".hermes"
$KitRoot    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

# Hermes on Windows defaults HERMES_HOME to %LOCALAPPDATA%\hermes, NOT
# %USERPROFILE%\.hermes. Pin it for every child `hermes` call (and for the
# official installer below) so the config.yaml/.env we write, `hermes doctor`,
# and the gateway logon task all read the SAME home. Without this the gateway
# boots against an empty %LOCALAPPDATA%\hermes and reports "DISCORD_BOT_TOKEN
# missing" with an unmigrated (v0) config even though we wrote both here.
# `hermes gateway install` bakes this value into the logon launcher, so the task
# is also immune to the interactive account's %USERPROFILE% drifting (e.g. a
# Default-profile fallback like C:\Users\default.<HOST>).
$env:HERMES_HOME = $HermesHome

# Probe the host's tailnet endpoints BEFORE any expensive setup (Hermes install,
# cua driver, browser build). The host publishes these with `make windows-vm-network`
# (Docker on 127.0.0.1 + Tailscale Serve); this script does not configure the host's
# Tailscale. A TCP connect is enough — we do not authenticate and never send or print
# the API key. Fail clearly and early so the host is fixed once, not after a long install.
function Test-HostPort([string] $Address, [int] $Port, [int] $TimeoutMs = 4000) {
  try {
    $client = New-Object System.Net.Sockets.TcpClient
    $iar = $client.BeginConnect($Address, $Port, $null, $null)
    $ok = $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
    if ($ok -and $client.Connected) { $client.EndConnect($iar); return $true }
    return $false
  } catch { return $false }
  finally { if ($client) { $client.Close() } }
}

# Write a GENERATED config file as UTF-8 WITHOUT a BOM. Windows PowerShell 5.1's
# `Set-Content`/`Out-File -Encoding UTF8` prepends a BOM (EF BB BF). Hermes reads
# honcho.json with read_text(encoding='utf-8') + json.loads, and json.loads rejects a
# leading U+FEFF ("Unexpected UTF-8 BOM"); its Honcho CLI swallows that as {}, so a
# BOM'd honcho.json is reported as "No Honcho config found" right after we wrote it.
# config.yaml and .env share the hazard. UTF8Encoding($false) emits no BOM and
# WriteAllText bypasses PowerShell's own encoding layer entirely. (The .ps1 scripts
# themselves KEEP their BOM so 5.1 reads them as UTF-8, not CP1252 — a separate bug,
# guarded by test_windows_ps1_encoding.sh.)
function Write-Utf8NoBom {
  param(
    [Parameter(Mandatory)][string] $Path,
    [Parameter(Mandatory)][AllowEmptyString()][string] $Content
  )
  $enc = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Path, $Content, $enc)
}

Write-Host "[agent-ops-kit] probing host $HostAddress`:$CliProxyPort (CLIProxyAPI) over Tailscale"
if (-not (Test-HostPort $HostAddress $CliProxyPort)) {
  throw "Cannot reach the model API at ${HostAddress}:$CliProxyPort. On the Linux host: bring the stack up (make up, Docker stays on 127.0.0.1), publish it over Tailscale (make windows-vm-network), and allow this VM in your Tailscale ACLs. Verify on the host with: make windows-vm-network-status. Do NOT bind Docker to the Tailscale IP. See docs/windows-vm-worker.md."
}
if ($UseHostHoncho) {
  Write-Host "[agent-ops-kit] probing host $HostAddress`:$HonchoPort (Honcho) over Tailscale"
  if (-not (Test-HostPort $HostAddress $HonchoPort)) {
    throw "Cannot reach the host Honcho at ${HostAddress}:$HonchoPort, but -UseHostHoncho was requested. On the host: publish it with `make windows-vm-network` (forwards :$HonchoPort from loopback) and allow this VM in your ACLs, then re-run. Or drop -UseHostHoncho to use the worker's local built-in memory."
  }
}

# honcho-ai (the Honcho SDK) has to land in the SAME Python venv the `hermes`
# runtime imports from, which on Windows is NOT under $HermesHome. HERMES_HOME
# (%USERPROFILE%\.hermes here) holds config/.env; the Hermes checkout + venv
# defaults to %LOCALAPPDATA%\hermes\hermes-agent and STAYS there when Hermes was
# already installed before we pinned HERMES_HOME (the common case: `hermes` is
# already on PATH so the official installer above is skipped). Guessing
# $HermesHome\hermes-agent\venv then silently no-ops (Test-Path is false) and
# `hermes honcho status` still reports "honcho-ai is not installed". Discover the
# real interpreter instead: follow the on-PATH `hermes` launcher first (a
# relocatable-venv install stages a hermes.cmd whose body points at the venv's
# hermes.exe), then fall back to the known runtime roots, returning the first
# python.exe that exists.
function Get-HermesRuntimePython {
  $candidates = New-Object System.Collections.Generic.List[string]

  # 1) Derive from the launcher Hermes actually runs. The .cmd delegator body is
  #    `"<Root>\venv\Scripts\hermes.exe" %*`; the sibling python.exe in that
  #    Scripts dir is the runtime interpreter, wherever the tree lives.
  $cmd = Get-Command hermes -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source) {
    foreach ($p in @($cmd.Source, [System.IO.Path]::ChangeExtension($cmd.Source, ".cmd"))) {
      if ($p -and ($p -like "*.cmd") -and (Test-Path -LiteralPath $p)) {
        try {
          $body = Get-Content -LiteralPath $p -Raw -ErrorAction Stop
          $m = [regex]::Match($body, '([A-Za-z]:\\[^"]*?\\venv\\Scripts\\)hermes\.exe')
          if ($m.Success) { $candidates.Add((Join-Path $m.Groups[1].Value "python.exe")) }
        } catch {}
      }
    }
  }

  # 2) Known runtime roots, most-likely first: %LOCALAPPDATA%\hermes is the
  #    Windows default; $HermesHome\hermes-agent covers an install that DID honour
  #    a pre-set HERMES_HOME.
  foreach ($root in @("$env:LOCALAPPDATA\hermes\hermes-agent", (Join-Path $HermesHome "hermes-agent"))) {
    if ($root) { $candidates.Add((Join-Path $root "venv\Scripts\python.exe")) }
  }

  foreach ($py in $candidates) {
    if ($py -and (Test-Path -LiteralPath $py)) { return $py }
  }
  return $null
}

# Probe whether $Py can import $Module and return a plain boolean. The Hermes runtime
# venv is pip-less and honcho-ai may be absent, so `python -c "import honcho"` can exit
# non-zero with a traceback on stderr. Under the script-wide $ErrorActionPreference='Stop'
# that native stderr is promoted to a terminating NativeCommandError — which previously
# aborted the whole install (before Chrome MCP) at `& $venvPy -c "import honcho" 2>$null`.
# We drop to 'Continue' for the probe, swallow both streams, and key the result off the
# process exit code only, so a failed import is data — never a thrown error.
function Test-PythonImport {
  param([Parameter(Mandatory)][string] $Py, [Parameter(Mandatory)][string] $Module)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    & $Py -c "import $Module" 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
  } finally {
    $ErrorActionPreference = $prev
  }
}

# Install a pip-style requirement into the interpreter $Py WITHOUT assuming pip exists.
# The Hermes runtime venv is intentionally pip-less: `python.exe -m pip` fails with
# "No module named pip". Hermes ships `uv` on Windows, so we install into the exact
# target interpreter with `uv pip install --python` first. Only when uv is missing (or its
# install exits non-zero) do we fall back — `hermes honcho setup` installs the same pin,
# and as a last resort we bootstrap pip with `ensurepip` and use it only if that succeeds.
# Native stderr is kept non-terminating here for the same reason as the import probe.
# Returns $true when $Module ends up importable by $Py.
function Install-PythonRequirement {
  param(
    [Parameter(Mandatory)][string] $Py,
    [Parameter(Mandatory)][string] $Spec,
    [Parameter(Mandatory)][string] $Module
  )
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
      & uv pip install --python "$Py" -q "$Spec" 2>&1 | Out-Null
      if ($LASTEXITCODE -eq 0 -and (Test-PythonImport $Py $Module)) { return $true }
      Write-Warning "[agent-ops-kit] 'uv pip install $Spec' did not yield an importable $Module (exit $LASTEXITCODE); trying fallbacks."
    } else {
      Write-Warning "[agent-ops-kit] 'uv' was not found on PATH; falling back to install $Spec without it."
    }

    # Fallback 1: let Hermes install honcho-ai the way `hermes honcho setup` does.
    & hermes honcho setup 2>&1 | Out-Null
    if (Test-PythonImport $Py $Module) { return $true }

    # Fallback 2 (last resort): bootstrap pip into the venv with ensurepip, then use it.
    # Guarded — a truly pip-less interpreter without ensurepip support just returns false
    # instead of hard-failing the install.
    & $Py -m ensurepip --upgrade 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
      & $Py -m pip install -q "$Spec" 2>&1 | Out-Null
    }
    return (Test-PythonImport $Py $Module)
  } finally {
    $ErrorActionPreference = $prev
  }
}

if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
  Write-Host "[agent-ops-kit] installing Hermes"
  # Run the official installer in its own child scope. It declares its own
  # $HermesHome, and PowerShell variable names are case-insensitive, so
  # Invoke-Expression — which runs the fetched code in THIS optimized script
  # scope — fails with "Cannot overwrite variable HermesHome because the
  # variable has been optimized." Building a fresh scriptblock and invoking it
  # with the call operator (&) gives the installer a private local scope, so its
  # $HermesHome (and every other local) can never collide with ours. Env/PATH
  # changes it makes still propagate because those live on the process.
  #
  # -SkipSetup is required. The official installer runs its setup wizard (the
  # Nous Portal sign-in) unless told to skip it, which would stall this
  # unattended install on an interactive login. This stack self-hosts the model
  # API and writes its own CLIProxyAPI config.yaml below, so the wizard has
  # nothing to configure — skip it and let our config win.
  $installerScript = Invoke-RestMethod https://hermes-agent.nousresearch.com/install.ps1
  & ([scriptblock]::Create($installerScript)) -SkipSetup
  $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}
New-Item -ItemType Directory -Force -Path $HermesHome, (Join-Path $HermesHome "memories") | Out-Null

if ($UseHostHoncho) {
  $memoryProvider = "  provider: honcho"
} else {
  $memoryProvider = "  # local built-in memory (no external Honcho)"
}

$configYaml = @"
# Rendered by scripts/windows-vm/install-worker.ps1 — the Windows desktop worker.
# The model is a custom OpenAI-compatible endpoint (CLIProxyAPI on the host). The
# key/URL live in .env as OPENAI_API_KEY/OPENAI_BASE_URL — the exact names
# `hermes doctor` recognises as "API key or custom endpoint configured". A
# non-standard name like CLIPROXY_API_KEY resolves at runtime but makes doctor
# report "No API key found in ~/.hermes/.env" and nudge `hermes setup`.
model:
  provider: custom
  default: $Model
  base_url: `${OPENAI_BASE_URL}
  api_key: `${OPENAI_API_KEY}

toolsets: [hermes-cli]
platform_toolsets:
  cli: [web, search, terminal, file, browser, vision, skills, todo, memory, session_search, clarify, kanban, computer_use]
  discord: [web, search, terminal, file, browser, vision, skills, todo, memory, session_search, clarify, kanban, computer_use, discord]

computer_use:
  permission_mode: standard
  cua_telemetry: false

browser:
  backend: browser-use
  headed: true
  allow_private_urls: true

memory:
  memory_enabled: true
  user_profile_enabled: true
$memoryProvider

discord:
  require_mention: true
  auto_thread: true
  allow_mentions:
    everyone: false
    roles: false
    users: true
$mcpServersBlock
"@
Write-Utf8NoBom -Path (Join-Path $HermesHome "config.yaml") -Content $configYaml

# OPENAI_API_KEY/OPENAI_BASE_URL (not CLIPROXY_API_KEY): these are the provider
# credential names `hermes doctor` scans .env for. The model provider is `custom`
# with an explicit base_url, so the value is just the CLIProxyAPI key/URL — the
# standard names make doctor report "API key or custom endpoint configured".
$envFile = @"
DISCORD_BOT_TOKEN=$DiscordBotToken
DISCORD_ALLOWED_USERS=$AllowedUsers
DISCORD_HOME_CHANNEL=$HomeChannel
DISCORD_REQUIRE_MENTION=true
OPENAI_API_KEY=$CliProxyApiKey
OPENAI_BASE_URL=http://$HostAddress`:$CliProxyPort/v1
"@
Write-Utf8NoBom -Path (Join-Path $HermesHome ".env") -Content $envFile

if ($UseHostHoncho) {
  Write-Warning "-UseHostHoncho shares the host's Honcho, which runs with auth disabled. Use it only when the worker reaches the host strictly over Tailscale and you accept that anyone on the tailnet can read and write this memory."
# defaultHost pins "hermes" as the active host block: Hermes's Honcho loader reads
# hosts[defaultHost] to decide which endpoint is live, so without it a honcho.json with
# only hosts.hermes.enabled true can still resolve to no active host and the provider
# reports disabled. enabled stays true on the block itself.
#
# baseUrl goes INSIDE hosts.hermes (host-block fields win over root, and it is the
# first spelling the loader reads: host_block.baseUrl -> ... -> raw.baseUrl). The
# active Hermes 0.21.2 build keys the provider's availability off the host block —
# doctor's own remediation is "set apiKey on hosts.hermes" — so a base URL stranded
# only at the root can leave Honcho reported as disabled / "no base URL configured".
# The root copy is kept for back-compat with loaders that read it there. No apiKey:
# the host's Honcho runs unauthenticated and its Tailscale/CGNAT URL is treated as a
# local deployment, so the SDK supplies a placeholder key itself.
$honchoJson = @"
{
  "baseUrl": "http://$HostAddress`:$HonchoPort",
  "defaultHost": "hermes",
  "hosts": {
    "hermes": { "enabled": true, "baseUrl": "http://$HostAddress`:$HonchoPort", "aiPeer": "hermes.windows-operator", "peerName": "$PeerName", "workspace": "$Workspace" }
  }
}
"@
  Write-Utf8NoBom -Path (Join-Path $HermesHome "honcho.json") -Content $honchoJson
} else {
  Write-Host "[agent-ops-kit] using Hermes local built-in memory (no external Honcho)"
}

$soulSrc = Join-Path $KitRoot "hermes\profiles\windows-operator\SOUL.md"
if (Test-Path $soulSrc) { Copy-Item $soulSrc (Join-Path $HermesHome "SOUL.md") -Force }
else { Write-Warning "SOUL.md not found at $soulSrc; copy hermes/profiles/windows-operator/SOUL.md to $HermesHome manually" }

# Computer use (cua-driver): install, then verify. The official installer's own
# cua-driver step is best-effort — it can print "install succeeded" and still
# hand back an incompatible runtime, and that warning is easy to miss in a long
# log. So provision it explicitly here and confirm with the health matrix.
# Telemetry stays off (config.yaml sets cua_telemetry: false above). Run this
# from the elevated interactive session so the driver's runtime is exercised now
# rather than deferred to first tool use. Best-effort but never hidden: if it
# stays unhealthy we warn loudly with a recovery step instead of failing silently.
Write-Host "[agent-ops-kit] installing and verifying the computer-use driver"
$cuaHealthy = $false
foreach ($attempt in 1, 2) {
  try { hermes computer-use install } catch { Write-Warning "computer-use install (attempt $attempt) errored: $($_.Exception.Message)" }
  # status prints the on-PATH driver; doctor runs cua-driver's health_report,
  # which starts the runtime and returns non-zero when anything is degraded.
  try { hermes computer-use status } catch {}
  hermes computer-use doctor
  if ($LASTEXITCODE -eq 0) { $cuaHealthy = $true; break }
  Write-Warning "[agent-ops-kit] computer-use doctor reported a degraded runtime (attempt $attempt); repairing."
}
if ($cuaHealthy) {
  Write-Host "[agent-ops-kit] computer use verified healthy"
} else {
  Write-Warning "[agent-ops-kit] Computer use is NOT healthy after install and one repair. The worker will still start, but desktop control will fail until this is fixed. From an elevated interactive session on the unlocked desktop, run: hermes computer-use install; then hermes computer-use doctor."
}

# Browser tools: config.yaml selects the browser-use backend (headed), which needs
# the agent-browser CLI plus a Playwright Chromium build. Provision both best-effort
# from this elevated session so the first browser action doesn't stall on a cold
# download. Non-fatal: if it fails, computer_use is unaffected and Hermes retries the
# Chromium fetch lazily on first browser use (security.allow_lazy_installs). The final
# `hermes doctor` reports the browser tool's real state.
#
# `hermes doctor` checks for a Playwright "chromium-*" build in the ms-playwright cache
# (the exact predicate the agent uses to decide whether to expose browser_* tools).
# `agent-browser install` alone can leave that check failing, so we ALSO run the exact
# command doctor recommends — `npx playwright install chromium` from the Hermes repo —
# which populates the same ms-playwright cache. Playwright skips builds already present,
# so this does not duplicate agent-browser's download when the revision matches.
Write-Host "[agent-ops-kit] provisioning browser tools (agent-browser + Playwright Chromium)"
$hermesRepo = Join-Path $HermesHome "hermes-agent"
$browserReady = $false
if (Get-Command npm -ErrorAction SilentlyContinue) {
  try {
    npm install -g agent-browser
    if ($LASTEXITCODE -eq 0) { agent-browser install }
    # Install the Playwright Chromium `hermes doctor` looks for, from the Hermes repo
    # (matches its Playwright pin) so the browser_* tools are advertised to the agent.
    if (Get-Command npx -ErrorAction SilentlyContinue) {
      if (Test-Path $hermesRepo) {
        Push-Location $hermesRepo
        try { npx --yes playwright install chromium; if ($LASTEXITCODE -eq 0) { $browserReady = $true } }
        finally { Pop-Location }
      } else {
        npx --yes playwright install chromium; if ($LASTEXITCODE -eq 0) { $browserReady = $true }
      }
    }
  } catch { Write-Warning "[agent-ops-kit] browser provisioning errored: $($_.Exception.Message)" }
} else {
  Write-Warning "[agent-ops-kit] npm/Node.js not found; skipping browser provisioning."
}
if (-not $browserReady) {
  Write-Warning "[agent-ops-kit] Browser tools are not fully provisioned. computer_use is unaffected. To repair from an elevated session: npm install -g agent-browser; agent-browser install; cd `"$hermesRepo`"; npx playwright install chromium"
}

# The gateway must run on the interactive desktop (Session 1+). Use Hermes's own
# Windows service installer instead of hand-rolling schtasks: it resolves the full
# logon identity (DOMAIN\user from USERDOMAIN\USERNAME — a bare username as a logon
# trigger fails with "Register-ScheduledTask : The parameter is incorrect. (…):UserId"),
# registers a Scheduled Task with an explicit InteractiveToken principal at
# LeastPrivilege (the correct run level for computer_use — an elevated/Highest gateway
# cannot drive normal-integrity apps across the Windows UIPI boundary), launches
# `hermes gateway run` through a console-less wscript.exe shim so the logon
# CTRL_CLOSE can't kill it, is idempotent (delete+create), falls back to a
# Startup-folder item when schtasks is blocked, and starts + verifies. HERMES_HOME
# (pinned at the top) is baked into the generated launcher, so the gateway reads the
# same config/.env we wrote. Registration precedes the start; both are verified below.
Write-Host "[agent-ops-kit] installing the Hermes gateway logon task"
hermes gateway install --start-on-login --start-now
if ($LASTEXITCODE -ne 0) {
  throw "hermes gateway install failed (exit $LASTEXITCODE). Run it from an elevated PowerShell on the interactive desktop: hermes gateway install --start-on-login --start-now"
}

# Verify registration (hard failure) and that a gateway process is live (soft
# warning: a logon task legitimately waits for the next interactive logon when it
# was installed over RDP/SSH rather than on the console).
$gatewayStatus = (hermes gateway status 2>&1 | Out-String)
Write-Host $gatewayStatus
if ($gatewayStatus -notmatch 'Scheduled Task registered' -and $gatewayStatus -notmatch 'login item installed') {
  throw "The Hermes gateway service did not register. See the status output above, then re-run from an elevated session: hermes gateway install --start-on-login --start-now"
}
if ($gatewayStatus -notmatch 'Gateway process running') {
  Write-Warning "[agent-ops-kit] Gateway service registered but no gateway process is running yet. It starts at the next interactive logon. To start it now from the unlocked desktop: hermes gateway start (logs: $HermesHome\logs\gateway.log)."
}

# `hermes doctor --fix` migrates the config schema non-interactively (v0 → current,
# v44) against HERMES_HOME before printing the health matrix. Plain `hermes doctor`
# only REPORTS the drift ("Config version outdated (v0 → v44)") — it does not migrate;
# only --fix runs migrate_config(interactive=False). --fix stays non-interactive here
# (stdin is not a prompt: interactive=False skips the missing-key questions) and never
# launches the setup wizard or Nous Portal. A config with no _config_version stamp is
# treated as fresh and gets the full migration ladder plus the version stamp.
hermes doctor --fix

if ($UseHostHoncho) {
  # honcho.json (written above: defaultHost hermes + hosts.hermes.enabled true) makes
  # Honcho reachable and config.yaml's memory.provider activates it. Install the plugin
  # dependency, flip the host block on through the CLI when that subcommand exists, then
  # verify. -UseHostHoncho is an explicit opt-in for shared memory, so if it stays
  # disabled or unreachable we fail clearly instead of silently degrading to built-in
  # memory. This runs AFTER `hermes doctor --fix` so the config migration and the
  # browser/gateway remediation above still complete even when the host is not publishing
  # Honcho — only the shared-memory guarantee the operator asked for is enforced here.
  # Install honcho-ai into the venv the Hermes runtime imports from (discovered via
  # the launcher / known runtime roots, NOT a hardcoded $HermesHome subtree that does
  # not exist when the checkout lives under %LOCALAPPDATA%\hermes). Pin the version
  # Hermes' own `hermes honcho setup` installs, then confirm the interpreter can import
  # it so a silent pip failure surfaces here rather than as "honcho-ai is not installed"
  # in the status check below.
  $venvPy = Get-HermesRuntimePython
  if ($venvPy) {
    Write-Host "[agent-ops-kit] installing honcho-ai into the Hermes runtime ($venvPy)"
    # The runtime venv is pip-less (`python -m pip` => "No module named pip"), so install
    # uv-first via Install-PythonRequirement; it falls back to `hermes honcho setup`/ensurepip
    # and probes the import through Test-PythonImport so a failure cannot terminate the run.
    $honchoOk = Install-PythonRequirement -Py $venvPy -Spec "honcho-ai==2.2.0" -Module "honcho"
    if (-not $honchoOk) {
      Write-Warning "[agent-ops-kit] honcho-ai is still not importable by the Hermes runtime at $venvPy. Install it by hand from the worker's session: uv pip install --python '$venvPy' 'honcho-ai==2.2.0' (or run 'hermes honcho setup')."
    }
  } else {
    Write-Warning "[agent-ops-kit] could not locate the Hermes runtime Python to install honcho-ai. Run 'hermes honcho setup' from the worker's session, or install it into the Hermes venv with uv: uv pip install --python '<hermes-agent>\venv\Scripts\python.exe' 'honcho-ai==2.2.0'."
  }

  # `hermes honcho enable` sets hosts.hermes.enabled through the CLI (idempotent with what
  # honcho.json already writes). The subcommand is not on every Hermes build, so probe the
  # help text and only call it when present.
  $honchoHelp = (hermes honcho --help 2>&1 | Out-String)
  if ($honchoHelp -match '(?im)\benable\b') {
    try { hermes honcho enable } catch { Write-Warning "[agent-ops-kit] 'hermes honcho enable' errored: $($_.Exception.Message)" }
  }

  Write-Host "[agent-ops-kit] verifying shared Honcho memory"
  $honchoStatus = ""
  if ($honchoHelp -match '(?im)\bstatus\b') { $honchoStatus = (hermes honcho status 2>&1 | Out-String); Write-Host $honchoStatus }
  $memStatus = (hermes memory status 2>&1 | Out-String)
  Write-Host $memStatus

  $honchoActive = ($memStatus -match 'Provider:\s*honcho' -and $memStatus -match 'Status:\s*available') -or `
                  ($honchoStatus -match '(?im)enabled' -and $honchoStatus -match '(?im)(available|reachable|connected)')
  if (-not $honchoActive) {
    throw "[agent-ops-kit] -UseHostHoncho was requested but shared Honcho is disabled or unreachable. Confirm config.yaml has memory.provider: honcho, honcho.json has defaultHost: hermes and hosts.hermes.enabled true with the host's Tailscale baseUrl, honcho-ai is installed, and the host publishes Honcho on http://${HostAddress}:$HonchoPort over Tailscale. Re-check with: hermes honcho status; hermes memory status"
  }
  Write-Host "[agent-ops-kit] shared Honcho memory verified active"
}

if ($EnableChromeDevToolsMcp) {
  # Chrome DevTools MCP for browser-only apps. The mcp_servers block is already in
  # config.yaml (written above). Here we: start the dedicated debuggable Chrome on
  # loopback, verify the MCP server actually starts and lists tools, then restart the
  # gateway so it loads those tools. Verification is authoritative — the install does
  # NOT claim success from writing config alone.
  Write-Host "[agent-ops-kit] setting up Chrome DevTools MCP (browser-only apps)"

  # Start the dedicated Chrome automation profile with loopback-only remote debugging.
  # `hermes mcp test` below does not need Chrome up (the server lists its tools without a
  # browser and attaches lazily on first tool call), and browser tools at RUNTIME do, so
  # bring it up now and register it on logon. Non-fatal: the MCP verification is the gate.
  $chromeLauncher = Join-Path $PSScriptRoot "start-chrome-automation.ps1"
  if (Test-Path $chromeLauncher) {
    try {
      & $chromeLauncher -UserDataDir $ChromeUserDataDir -Port $ChromeDebugPort `
          -ProfileDirectory $ChromeProfileDirectory -ChromeExePath $ChromeExePath
    } catch { Write-Warning "[agent-ops-kit] could not start the automation Chrome now: $($_.Exception.Message). Browser tools attach at runtime once it is running." }

    # Register the launcher on logon (Startup shortcut) so the debuggable Chrome is up
    # each session, matching the gateway's own logon start. Idempotent.
    try {
      $startup = [Environment]::GetFolderPath('Startup')
      $lnkPath = Join-Path $startup "hermes-chrome-automation.lnk"
      $ws = New-Object -ComObject WScript.Shell
      $lnk = $ws.CreateShortcut($lnkPath)
      $lnk.TargetPath = (Get-Command powershell).Source
      $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$chromeLauncher`" -UserDataDir `"$ChromeUserDataDir`" -Port $ChromeDebugPort" + $(if ($ChromeProfileDirectory) { " -ProfileDirectory `"$ChromeProfileDirectory`"" } else { "" })
      $lnk.Save()
    } catch { Write-Warning "[agent-ops-kit] could not register the Chrome automation logon item: $($_.Exception.Message). Start it by hand each session with start-chrome-automation.ps1." }
  } else {
    Write-Warning "[agent-ops-kit] start-chrome-automation.ps1 not found next to this script; start the debuggable Chrome by hand before using browser tools."
  }

  # Authoritative verification: the server must start and list its tools. `hermes mcp
  # test` prints "Tools discovered: N" on success and "Connection failed" on failure and
  # does not always set a nonzero exit code, so parse the output. Fail clearly on error.
  hermes mcp list
  Write-Host "[agent-ops-kit] verifying the Chrome DevTools MCP server starts (hermes mcp test)"
  $mcpTest = (hermes mcp test chrome-devtools 2>&1 | Out-String)
  Write-Host $mcpTest
  if ($mcpTest -notmatch 'Tools discovered' -or $mcpTest -match 'Connection failed') {
    throw "[agent-ops-kit] Chrome DevTools MCP failed to start. Fix it, then re-run: hermes mcp test chrome-devtools. Common causes: Node/npx.cmd not on PATH, or the chrome-devtools-mcp package could not be fetched. The mcp_servers block is in $HermesHome\config.yaml."
  }
  Write-Host "[agent-ops-kit] Chrome DevTools MCP verified: server starts and tools are discovered"

  # Restart the gateway so the running process reloads the mcp_servers config and exposes
  # the mcp_chrome-devtools_* tools. Only after the config change, per the install order.
  hermes gateway restart
  Write-Host "[agent-ops-kit] gateway restarted to load Chrome DevTools MCP tools"

  Write-Host ""
  Write-Host "Chrome DevTools MCP one-time steps (do these in the worker's interactive session):"
  Write-Host "  1. In the dedicated Chrome automation profile ($ChromeUserDataDir), sign in to the browser-only apps yourself. The agent never types passwords or 2FA."
  Write-Host "  2. Confirm the debuggable Chrome is running on 127.0.0.1:$ChromeDebugPort (the logon item starts it). If you use the auto-connect flow instead, approve it once at chrome://inspect/#remote-debugging."
  Write-Host "  3. Keep that Chrome running while the worker uses browser tools."
}

Write-Host ""
Write-Host "Worker installed. It answers as its own Discord bot and can take Kanban tasks assigned to 'windows-operator'."
Write-Host "Keep the VM unlocked with a fixed resolution while it works (see docs/windows-vm-worker.md)."
