<#
.SYNOPSIS
  Install the Hermes "windows-operator" worker inside the Windows VM.

.DESCRIPTION
  Installs Hermes, points it at the stack on your Linux host (CLIProxyAPI for
  the model, Honcho for memory), enables the computer_use toolset, installs the
  cua driver, seeds the windows-operator persona and registers a logon task
  that starts the Discord gateway on the interactive desktop.

  Run from an elevated PowerShell in the VM's console/RDP session (not SSH):
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\install-worker.ps1 -HostAddress 100.64.0.1 -CliProxyApiKey <key> `
        -DiscordBotToken <token> -AllowedUsers 282100214024896522

  The host must publish the stack on that address: BIND_ADDR=0.0.0.0 in .env
  (Tailscale address recommended), then `make up`.
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
  [string] $HomeChannel = ""
)
$ErrorActionPreference = "Stop"

$HermesHome = Join-Path $env:USERPROFILE ".hermes"
$KitRoot    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
  Write-Host "[agent-ops-kit] installing Hermes"
  Invoke-Expression (Invoke-RestMethod https://hermes-agent.nousresearch.com/install.ps1)
  $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}
New-Item -ItemType Directory -Force -Path $HermesHome, (Join-Path $HermesHome "memories") | Out-Null

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
  provider: honcho

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

@"
{
  "baseUrl": "http://$HostAddress`:$HonchoPort",
  "hosts": {
    "hermes": { "enabled": true, "aiPeer": "hermes.windows-operator", "peerName": "$PeerName", "workspace": "$Workspace" }
  }
}
"@ | Set-Content -Path (Join-Path $HermesHome "honcho.json") -Encoding UTF8

$soulSrc = Join-Path $KitRoot "hermes\profiles\windows-operator\SOUL.md"
if (Test-Path $soulSrc) { Copy-Item $soulSrc (Join-Path $HermesHome "SOUL.md") -Force }
else { Write-Warning "SOUL.md not found at $soulSrc; copy hermes/profiles/windows-operator/SOUL.md to $HermesHome manually" }

Write-Host "[agent-ops-kit] installing the computer-use driver"
hermes computer-use install

$venvPy = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
if (Test-Path $venvPy) { & $venvPy -m pip install -q honcho-ai }

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
