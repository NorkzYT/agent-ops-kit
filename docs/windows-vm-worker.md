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
`:8317`). Run all worker↔host traffic over Tailscale.

**Keep the Docker services bound to `127.0.0.1` and publish them to the tailnet
with Tailscale Serve.** This is the canonical path. Docker never binds a routable
address, so the host's own Hermes, `make doctor` and every loopback probe keep
reaching `127.0.0.1:<port>` unchanged; Tailscale Serve terminates TLS on the
tailnet and forwards raw TCP back to that same loopback port. From the repo root:

```bash
make windows-vm-network         # forward :8317 (models) and :8000 (Honcho) over Tailscale Serve
make windows-vm-network-status  # verify: loopback services up + both forwarders live
make windows-vm-network-off     # remove just those two forwarders
```

Under the hood that runs the current Tailscale CLI:

```bash
tailscale serve --bg --yes --tcp=8317 tcp://127.0.0.1:8317
tailscale serve --bg --yes --tcp=8000 tcp://127.0.0.1:8000
```

`--bg` persists the forwarders across reboots and Tailscale restarts. Access is
**tailnet-only** (Serve, not Funnel) and your Tailscale ACLs still govern which
tailnet peers may reach these ports — the VM's node must be allowed to reach the
host on `:8317` (and `:8000` if it uses host Honcho). The VM then points at
`http://<host-tailnet-name-or-IP>:8317/v1`. Serve needs the Tailscale operator
set once (`sudo tailscale set --operator=$USER`) or it runs under `sudo`; the
script handles both and tells you which.

Do **not** set `CLIPROXY_BIND_ADDR` / `HONCHO_BIND_ADDR` to the host's Tailscale
IP or `0.0.0.0`. A non-loopback Docker bind breaks the loopback forward target
and makes `make doctor` and the host Hermes (which probe `127.0.0.1`) disagree
with what the VM reaches. `make windows-vm-network` refuses to run if `.env` asks
for a non-loopback bind.

If this host already runs an **unrelated** Tailscale Serve or Funnel config
(e.g. a public site on `:443`), it is separate and untouched: these targets add
and remove only the two `--tcp` listeners above — they never run
`tailscale serve reset` and never touch Funnel.

Memory is a separate choice. By default the worker uses Hermes's local built-in
memory, so it needs nothing from the host for memory. In that case you can skip
the Honcho forwarder; the CLIProxyAPI forward on `:8317` is all the worker needs.
This is the recommended path.

Honcho ships with auth disabled. Sharing the host's Honcho (the `:8000` forwarder
plus `-UseHostHoncho` on the worker) is acceptable only when both hold:

- the worker reaches it strictly over Tailscale (Serve keeps it tailnet-only;
  Docker stays on `127.0.0.1`), and
- you accept that anyone on the tailnet allowed by your ACLs can read and write
  that memory.

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
via CLIProxyAPI, `computer_use` on, browser headed) and `.env`.

The model is a custom OpenAI-compatible endpoint (CLIProxyAPI), so the key and URL
go into `.env` as `OPENAI_API_KEY` and `OPENAI_BASE_URL` — the exact names
`hermes doctor` scans for. `config.yaml` references them as literal `${OPENAI_API_KEY}`
placeholders. A non-standard name like `CLIPROXY_API_KEY` still resolves at runtime
but makes `hermes doctor` report `No API key found in ~/.hermes/.env` and nudge
`hermes setup`.

It then copies the `windows-operator` persona, installs the computer-use driver and
verifies it with `hermes computer-use doctor` (repairing once if the runtime comes
back degraded), and provisions the browser tools (best-effort): the `agent-browser`
CLI, plus the **Playwright Chromium** build `hermes doctor` actually checks for, via
`npx playwright install chromium` run from the Hermes repo. (`agent-browser install`
alone can leave doctor's `chromium-*` cache check failing; Playwright shares the same
ms-playwright cache and skips builds already present.)

It registers the logon task with `hermes gateway install --start-on-login
--start-now`. That is Hermes's own Windows installer: it resolves the full
`DOMAIN\user` logon identity, registers a Scheduled Task with an explicit
`InteractiveToken` principal at `LeastPrivilege` (an elevated gateway could not
drive normal-integrity apps across the Windows UIPI boundary), launches
`hermes gateway run` through a console-less `wscript.exe` shim, and starts +
verifies the gateway. It ends with `hermes doctor --fix`, which migrates the config
schema to the current version (v0 → v44) non-interactively. Plain `hermes doctor`
only *reports* the drift; only `--fix` runs the migration, and it never launches the
setup wizard or Nous Portal.

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
hermes doctor --fix            # migrates the config (v0 → v44); DISCORD_BOT_TOKEN resolves
```

If `hermes doctor` still reports `DISCORD_BOT_TOKEN` missing, confirm the token is
in `%USERPROFILE%\.hermes\.env` and that `HERMES_HOME` points there for the shell
you run `hermes` from. `discord.py` showing as missing is expected — the Discord
gateway installs it on first run; it does not block startup.

### Recovery: doctor reports "No API key found in ~/.hermes/.env"

`hermes doctor` scans `.env` for known provider variable names. The model is a
custom OpenAI-compatible endpoint, so the key/URL must be `OPENAI_API_KEY` and
`OPENAI_BASE_URL` (not a bespoke name like `CLIPROXY_API_KEY`, which resolves at
runtime but is invisible to this check). The current script writes the right names;
an older `.env` can be fixed by re-running the installer, or by setting them by hand:

```powershell
# in %USERPROFILE%\.hermes\.env
OPENAI_API_KEY=<CLIPROXY_API_KEY>
OPENAI_BASE_URL=http://100.64.0.1:8317/v1
```

`config.yaml` references them as `${OPENAI_API_KEY}` / `${OPENAI_BASE_URL}`.

### Recovery: doctor reports "Playwright Chromium not installed"

The browser backend needs a Playwright `chromium-*` build in the ms-playwright
cache — `agent-browser install` alone does not always satisfy doctor's check.
Install the exact build from the Hermes repo:

```powershell
cd $env:USERPROFILE\.hermes\hermes-agent
npx playwright install chromium
hermes doctor            # "Playwright Chromium" now passes
```

This does not affect `computer_use`; browser tools are simply hidden from the agent
until Chromium is present.

### Recovery: computer use is degraded

The install verifies the driver with `hermes computer-use doctor`. If it warns
that computer use is not healthy, fix it from an elevated interactive session on
the **unlocked** desktop (a locked session or disconnected RDP fails the X/GUI
checks): run `hermes computer-use install`, then `hermes computer-use doctor`
and read the health matrix. Telemetry stays off (`cua_telemetry: false`).

By default the worker uses Hermes's local built-in memory: the script does not
write `honcho.json` and does not point at the host's Honcho. Add
`-UseHostHoncho` only when you have chosen to share the host's Honcho over
Tailscale (see Host side). That flag writes `honcho.json` (with `defaultHost: hermes`
and `hosts.hermes.enabled: true`, same workspace as the host, its own `aiPeer`),
sets `memory.provider: honcho` in `config.yaml`, installs `honcho-ai`, enables the
host block via `hermes honcho enable`, and prints a warning that this shares
unauthenticated memory. It then **verifies** the provider is live with
`hermes honcho status` / `hermes memory status` and **fails the install** if shared
Honcho is disabled or unreachable — because you explicitly asked for shared memory,
the script does not silently fall back to built-in memory. To recover, confirm the
host publishes Honcho over Tailscale Serve (`make windows-vm-network`, which forwards
`:8000` from loopback; verify with `make windows-vm-network-status`) and that the
worker can reach `http://<host>:8000`, then re-run the installer.

Optional parameters: `-Model`, `-CliProxyPort`, `-HonchoPort`, `-PeerName`,
`-Workspace`, `-HomeChannel`, `-UseHostHoncho`.

## Reading `hermes doctor`: benign vs blocking

The install ends with `hermes doctor --fix`. Not every warning it prints is a
problem. What is safe to ignore and what must be fixed:

**Benign (expected on a healthy worker):**

- `discord.py … (optional)` shown as missing — the Discord gateway installs it
  lazily on first run; it does not block startup.
- `Nous Portal auth (not logged in)` — this stack self-hosts the model API through
  CLIProxyAPI and never signs into the Portal.
- `Croniter (optional)` and other `(optional)` dependency rows.
- `Config version outdated (v0 → v44)` seen mid-run **before** the `--fix` migration
  line — `--fix` then prints `Config migrated to latest version`. Only a persistent
  drift after `--fix` is a problem.

**Blocking (fix before relying on the worker):**

- `No API key found in ~/.hermes/.env` — the model provider credential is missing or
  under a name doctor does not recognise (see the recovery above; use
  `OPENAI_API_KEY` / `OPENAI_BASE_URL`).
- `DISCORD_BOT_TOKEN missing` — the gateway cannot start; check `.env` and
  `HERMES_HOME`.
- `Playwright Chromium not installed` — browser tools are hidden until fixed (recovery
  above). `computer_use` still works.
- `Computer use … degraded` — desktop control will fail (see recovery below).
- Config still showing `v0` **after** `--fix` — migration did not run; re-run
  `hermes doctor --fix` from the pinned `HERMES_HOME`.

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
