# Windows VM worker

Some work needs a real desktop: BrowserStack App Live screenshots of an iPad
app, dashboards without an API, Windows-only tools. A second Hermes runs
inside a Windows VM with the `computer_use` toolset. It has its own Discord
bot, uses the same proxies and Honcho as the host, and takes Kanban tasks
assigned to `windows-operator`.

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

In `.env` set `BIND_ADDR` to the host's Tailscale IP (or `0.0.0.0` behind a
firewall), then `make up`. The VM must reach `http://<host>:8317` and
`http://<host>:8000`.

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

The script installs Hermes, writes `config.yaml` (model via CLIProxyAPI,
`computer_use` on, browser headed), `.env`, `honcho.json` (same peer and
workspace as the host, its own `aiPeer`), copies the `windows-operator`
persona, runs `hermes computer-use install`, and registers a logon scheduled
task that starts `hermes gateway` on the interactive desktop. It ends with
`hermes doctor`.

Optional parameters: `-Model`, `-CliProxyPort`, `-HonchoPort`, `-PeerName`,
`-Workspace`, `-HomeChannel`.

## Use it

- Mention the worker bot in Discord: "Open BrowserStack App Live, launch the
  Velvet iPad build, capture the floor overview and the checkout screen."
- Or assign from the host: `hermes kanban create "Capture App Store
  screenshots: login, floor, checkout" --assignee windows-operator`.

The worker screenshots before and after each action and attaches evidence. It
waits for you on logins and MFA.

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
