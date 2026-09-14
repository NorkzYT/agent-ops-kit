<#
.SYNOPSIS
  Start the dedicated Chrome automation profile with LOOPBACK-only remote debugging
  so the Chrome DevTools MCP server can attach to it.

.DESCRIPTION
  The Windows worker's Chrome DevTools MCP connects to an already-running Chrome over
  --browser-url http://127.0.0.1:<port>. This launcher brings that Chrome up against a
  DEDICATED, signed-in automation profile, exposing the DevTools protocol on the
  loopback interface only. It never binds to the LAN or the tailnet.

  Security: the remote debugging endpoint hands anything that can reach the port full
  control of the profile — its live tabs, cookies and storage. Keep it on 127.0.0.1.
  This script refuses any non-loopback bind address by design. Point it only at the
  dedicated automation profile, never a personal Chrome profile.

  It is idempotent: if a debuggable Chrome is already listening on the port it exits
  without launching a second instance (one process per user-data-dir).

  Example (run in the worker's interactive session):
    .\start-chrome-automation.ps1 -UserDataDir "$env:USERPROFILE\chrome-automation"
#>
param(
  [Parameter(Mandatory = $true)] [string] $UserDataDir,
  [int]    $Port = 9222,
  # Loopback only. The parameter exists so the guarantee is explicit and testable;
  # any non-loopback value is rejected below. Do NOT set this to a wildcard or a
  # routable/tailnet address — that would expose CDP to the network.
  [string] $BindAddress = "127.0.0.1",
  [string] $ProfileDirectory = "",
  [string] $ChromeExePath = ""
)
$ErrorActionPreference = "Stop"

# Hard loopback guard: never expose the DevTools protocol beyond this machine.
if ($BindAddress -ne "127.0.0.1" -and $BindAddress -ne "localhost" -and $BindAddress -ne "::1") {
  throw "Refusing to bind Chrome remote debugging to '$BindAddress'. CDP must stay loopback-only (127.0.0.1); a non-loopback bind exposes live tabs, cookies and storage to the network."
}

# Resolve the Chrome executable. Fail clearly rather than silently launching nothing.
if (-not $ChromeExePath) {
  $candidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
  )
  $ChromeExePath = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $ChromeExePath -or -not (Test-Path $ChromeExePath)) {
  throw "Chrome was not found. Install Google Chrome, or pass -ChromeExePath to start-chrome-automation.ps1."
}

New-Item -ItemType Directory -Force -Path $UserDataDir | Out-Null

# Idempotent: if a debuggable Chrome already answers on loopback:$Port, do not start
# a second one (Chrome allows a single process per user-data-dir).
$alreadyUp = $false
try {
  $probe = New-Object System.Net.Sockets.TcpClient
  $iar = $probe.BeginConnect("127.0.0.1", $Port, $null, $null)
  if ($iar.AsyncWaitHandle.WaitOne(1500, $false) -and $probe.Connected) { $alreadyUp = $true }
} catch { }
finally { if ($probe) { $probe.Close() } }
if ($alreadyUp) {
  Write-Host "[agent-ops-kit] a debuggable Chrome is already listening on 127.0.0.1:$Port; leaving it running."
  return
}

$chromeArgs = @(
  "--remote-debugging-port=$Port",
  "--remote-debugging-address=$BindAddress",
  "--user-data-dir=$UserDataDir",
  "--no-first-run",
  "--no-default-browser-check"
)
if ($ProfileDirectory) { $chromeArgs += "--profile-directory=$ProfileDirectory" }

Write-Host "[agent-ops-kit] starting the dedicated Chrome automation profile with loopback-only remote debugging on 127.0.0.1:$Port"
Start-Process -FilePath $ChromeExePath -ArgumentList $chromeArgs | Out-Null
