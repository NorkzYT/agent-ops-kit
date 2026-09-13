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

  The host must publish CLIProxyAPI on that address over Tailscale: set
  CLIPROXY_BIND_ADDR to the host's Tailscale IP in .env (not 0.0.0.0), then
  `make up`. Keep every other service, including Honcho, on loopback.
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
model:
  provider: custom
  default: $Model
  base_url: http://$HostAddress`:$CliProxyPort/v1
  api_key: `${CLIPROXY_API_KEY}

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

@"
DISCORD_BOT_TOKEN=$DiscordBotToken
DISCORD_ALLOWED_USERS=$AllowedUsers
DISCORD_HOME_CHANNEL=$HomeChannel
DISCORD_REQUIRE_MENTION=true
CLIPROXY_API_KEY=$CliProxyApiKey
"@ | Set-Content -Path (Join-Path $HermesHome ".env") -Encoding UTF8

if ($UseHostHoncho) {
  Write-Warning "-UseHostHoncho shares the host's Honcho, which runs with auth disabled. Use it only when the worker reaches the host strictly over Tailscale and you accept that anyone on the tailnet can read and write this memory."
@"
{
  "baseUrl": "http://$HostAddress`:$HonchoPort",
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

if ($UseHostHoncho) {
  $venvPy = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
  if (Test-Path $venvPy) { & $venvPy -m pip install -q honcho-ai }
}

# Gateway must run on the interactive desktop (Session 1+), so register a logon task.
$exe = (Get-Command hermes).Source
$action  = New-ScheduledTaskAction -Execute $exe -Argument "gateway"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "hermes-gateway" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "hermes-gateway"

hermes doctor
Write-Host ""
Write-Host "Worker installed. It answers as its own Discord bot and can take Kanban tasks assigned to 'windows-operator'."
Write-Host "Keep the VM unlocked with a fixed resolution while it works (see docs/windows-vm-worker.md)."
