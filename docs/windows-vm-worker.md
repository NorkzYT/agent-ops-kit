# Windows VM worker

Some work needs a real desktop: BrowserStack App Live screenshots of an iPad
app, dashboards without an API, Windows-only tools. A second Hermes runs
inside a Windows VM with the `computer_use` toolset. It has its own Discord
bot, reaches the host's model API over Tailscale, keeps its own local memory,
and takes Kanban tasks assigned to `windows-operator`.

ChatGPT Desktop can also be installed in the same VM for you to use by hand.
It is not driven by Hermes: the subscription proxy gives Hermes the GPT
models, not a way into the ChatGPT Desktop app. Do not let both operate the
desktop at the same time.

## VM

- Windows 11, 4 to 8 vCPU, 8 to 16 GB RAM, 60 GB disk
- Fixed resolution (1920x1080), 100 % scaling, screen lock and sleep disabled
- A dedicated Windows account for the agent; no personal email, banking or
  password manager signed in
- A dedicated Chrome profile; sign in to the tools it needs (BrowserStack,
  PostHog, Grafana) yourself
- Tailscale installed, so the VM reaches the host privately
- Snapshot the VM before first install and before major changes

## Host side

The worker strictly needs one thing from the host: the model API (CLIProxyAPI on
`:8317`). Run all worker↔host traffic over Tailscale. In `.env` set
`CLIPROXY_BIND_ADDR` to the host's Tailscale IP (not `0.0.0.0`), then `make up`.
The VM then reaches `http://<host>:8317` (models) over the tailnet, and no other
service is exposed. Each bind is separate: opening one leaves every other service
on loopback.

Memory is a separate choice. By default the worker uses Hermes's local built-in
memory, so it needs nothing from the host for memory and you leave
`HONCHO_BIND_ADDR` on loopback. This is the recommended path.

Honcho ships with auth disabled. Sharing the host's Honcho is acceptable only
when both hold:

- the worker reaches it strictly over Tailscale — set `HONCHO_BIND_ADDR` to the
  host's Tailscale IP, never `0.0.0.0` or a bare LAN, and
- you accept that anyone on the tailnet can read and write that memory.

Otherwise keep Honcho loopback-only and use local memory on the worker (the
install script's default). Shared Honcho over Tailscale is the opt-in.

## Install the worker

Create a second Discord application for the worker. In the VM, from an
elevated PowerShell in the console or RDP session (not SSH):

```powershell
git clone https://github.com/NorkzYT/agent-ops-kit.git $HOME\agent-ops-kit
cd $HOME\agent-ops-kit\scripts\windows-vm
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install-worker.ps1 -HostAddress 100.64.0.1 -CliProxyApiKey <CLIPROXY_API_KEY> `
    -DiscordBotToken <worker bot token> -AllowedUsers <your discord id>
```

The script installs Hermes non-interactively (it passes `-SkipSetup` to the
official installer so the Nous Portal sign-in wizard never runs — this stack
self-hosts the model API and the script writes its own CLIProxyAPI config right
after). It pins `HERMES_HOME` to `%USERPROFILE%\.hermes` (Hermes defaults to
`%LOCALAPPDATA%\hermes` on Windows, so without this the gateway would read a
different home than the one the script writes to). It writes `config.yaml` (model
via CLIProxyAPI, `computer_use` on, browser headed), `.env`, copies the
`windows-operator` persona, installs the computer-use driver and verifies it with
`hermes computer-use doctor` (repairing once if the runtime comes back degraded),
provisions the browser tools (`agent-browser` CLI + Chromium, best-effort), and
registers the logon task with `hermes gateway install --start-on-login
--start-now`. That is Hermes's own Windows installer: it resolves the full
`DOMAIN\user` logon identity, registers a Scheduled Task with an explicit
`InteractiveToken` principal at `LeastPrivilege` (an elevated gateway could not
drive normal-integrity apps across the Windows UIPI boundary), launches
`hermes gateway run` through a console-less `wscript.exe` shim, and starts +
verifies the gateway. It ends with `hermes doctor`, which also migrates the config
schema to the current version.

### Recovery: the installer stopped at a Nous Portal login

If you ran an older copy of the script and it paused on the Nous Portal
sign-in wizard, do not sign in. Press `Ctrl+C` to cancel the wizard, then
re-run `install-worker.ps1` (now with `-SkipSetup`). The worker's own
`config.yaml` is what points Hermes at the host's CLIProxyAPI; the portal
account is not used by this stack.

### Recovery: the gateway task failed to register (older script)

An older copy of the script hand-rolled the Scheduled Task with a bare
`$env:USERNAME` logon trigger and no principal. On accounts where the effective
identity does not map from the bare username (e.g. `USERPROFILE` resolves to
`C:\Users\default.<HOST>` while the account is `ai-workstation`) this failed with:

```
Register-ScheduledTask : The parameter is incorrect. (7,32):UserId:ai-workstation
```

and then `Start-ScheduledTask` failed because nothing was registered. It also
wrote `config.yaml`/`.env` under `%USERPROFILE%\.hermes` while Hermes on Windows
reads `%LOCALAPPDATA%\hermes`, so `hermes doctor` reported `DISCORD_BOT_TOKEN`
missing and an unmigrated (v0) config even though both were written.

The current script fixes both. To recover a half-finished install, just **update
and re-run** it (it is idempotent). From an elevated PowerShell on the console/RDP
session, in `$HOME\agent-ops-kit`:

```powershell
git pull
cd scripts\windows-vm
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install-worker.ps1 -HostAddress 100.64.0.1 -CliProxyApiKey <CLIPROXY_API_KEY> `
    -DiscordBotToken <worker bot token> -AllowedUsers <your discord id>
```

Or, if the config and `.env` are already correct, register just the gateway task
by hand from that same elevated, unlocked session:

```powershell
$env:HERMES_HOME = "$env:USERPROFILE\.hermes"
hermes gateway install --start-on-login --start-now
hermes gateway status          # expect "Scheduled Task registered" + "Gateway process running"
hermes doctor                  # migrates the config; DISCORD_BOT_TOKEN now resolves
```

If `hermes doctor` still reports `DISCORD_BOT_TOKEN` missing, confirm the token is
in `%USERPROFILE%\.hermes\.env` and that `HERMES_HOME` points there for the shell
you run `hermes` from. `discord.py` showing as missing is expected — the Discord
gateway installs it on first run; it does not block startup.

### Recovery: computer use is degraded

The install verifies the driver with `hermes computer-use doctor`. If it warns
that computer use is not healthy, fix it from an elevated interactive session on
the **unlocked** desktop (a locked session or disconnected RDP fails the X/GUI
checks): run `hermes computer-use install`, then `hermes computer-use doctor`
and read the health matrix. Telemetry stays off (`cua_telemetry: false`).

By default the worker uses Hermes's local built-in memory: the script does not
write `honcho.json` and does not point at the host's Honcho. Add
`-UseHostHoncho` only when you have chosen to share the host's Honcho over
Tailscale (see Host side). That flag writes `honcho.json` (same workspace as the
host, its own `aiPeer`), installs `honcho-ai`, and prints a warning that this
shares unauthenticated memory.

Optional parameters: `-Model`, `-CliProxyPort`, `-HonchoPort`, `-PeerName`,
`-Workspace`, `-HomeChannel`, `-UseHostHoncho`.

## Use it

- Mention the worker bot in Discord: "Open BrowserStack App Live, launch the
  Velvet iPad build, capture the floor overview and the checkout screen."
- Or assign from the host: `hermes kanban create "Capture App Store
  screenshots: login, floor, checkout" --assignee windows-operator`.

The worker screenshots before and after each action and attaches evidence. It
waits for you on logins and MFA.

## Workflows

For the plan-first workflow (audit, propose, wait for approval, apply, verify,
report), Discord server organization over REST, Super Productivity handling and
setup examples, see [Windows operator workflows](windows-operator-workflows.md).

## Limits to know

- The desktop must be unlocked and visible. A locked session or a disconnected
  RDP window stops computer use. Use the VM console or keep RDP connected.
- Windows session isolation: the agent cannot drive elevated (admin) windows
  from a normal-integrity process. Run the gateway task at the same integrity
  level as the apps it needs, and do not expect it to click UAC prompts.
- Pixel-level control is the slowest path. The persona is told to prefer an
  API or CLI, then the browser tools, then computer use.

## Repeatable screenshots

For App Store assets that must be reproduced every release, the durable path
is a scripted Appium run on BrowserStack App Automate that captures named
PNGs. Use the worker to do it once by hand and to write that script; keep the
manual path for exceptions.
