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
  [switch] $UseHostHoncho
)
$ErrorActionPreference = "Stop"

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

@"
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
"@ | Set-Content -Path (Join-Path $HermesHome "config.yaml") -Encoding UTF8

# OPENAI_API_KEY/OPENAI_BASE_URL (not CLIPROXY_API_KEY): these are the provider
# credential names `hermes doctor` scans .env for. The model provider is `custom`
# with an explicit base_url, so the value is just the CLIProxyAPI key/URL — the
# standard names make doctor report "API key or custom endpoint configured".
@"
DISCORD_BOT_TOKEN=$DiscordBotToken
DISCORD_ALLOWED_USERS=$AllowedUsers
DISCORD_HOME_CHANNEL=$HomeChannel
DISCORD_REQUIRE_MENTION=true
OPENAI_API_KEY=$CliProxyApiKey
OPENAI_BASE_URL=http://$HostAddress`:$CliProxyPort/v1
"@ | Set-Content -Path (Join-Path $HermesHome ".env") -Encoding UTF8

if ($UseHostHoncho) {
  Write-Warning "-UseHostHoncho shares the host's Honcho, which runs with auth disabled. Use it only when the worker reaches the host strictly over Tailscale and you accept that anyone on the tailnet can read and write this memory."
# defaultHost pins "hermes" as the active host block: Hermes's Honcho loader reads
# hosts[defaultHost] to decide which endpoint is live, so without it a honcho.json with
# only hosts.hermes.enabled true can still resolve to no active host and the provider
# reports disabled. enabled stays true on the block itself.
@"
{
  "baseUrl": "http://$HostAddress`:$HonchoPort",
  "defaultHost": "hermes",
  "hosts": {
    "hermes": { "enabled": true, "aiPeer": "hermes.windows-operator", "peerName": "$PeerName", "workspace": "$Workspace" }
  }
}
"@ | Set-Content -Path (Join-Path $HermesHome "honcho.json") -Encoding UTF8
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
  $venvPy = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
  if (Test-Path $venvPy) { & $venvPy -m pip install -q honcho-ai }

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

Write-Host ""
Write-Host "Worker installed. It answers as its own Discord bot and can take Kanban tasks assigned to 'windows-operator'."
Write-Host "Keep the VM unlocked with a fixed resolution while it works (see docs/windows-vm-worker.md)."
