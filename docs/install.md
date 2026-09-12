# Install

One Linux host runs everything. Hermes is installed on the host (it needs the
terminal, the browser and your repos). Everything Hermes talks to over HTTP
runs in Docker.

```
Discord ──► Hermes (host, ~/.hermes)
              ├── model ........... CLIProxyAPI      :8317  (ChatGPT subscription)
              ├── coding subagents  claude-max-proxy :3456  (Claude Max subscription)
              ├── memory .......... Honcho API       :8000  (+ deriver, Postgres, Redis, Ollama)
              └── browser ......... Browser Use      (local Chromium, or Cloud with a key)
```

## Prerequisites

- Linux host (Ubuntu 22.04+ or Debian 12+ tested), a non-root user with `sudo`
- Docker Engine 24+ with the Compose plugin (`docker compose version`)
- `git`, `curl`, `make`
- A ChatGPT subscription (Plus, Pro or Team) for the orchestrator model
- A Claude Max subscription for the coding worker
- A Discord application with a bot user. Enable **Message Content Intent** and
  **Server Members Intent** under Bot, and invite it to your server with the
  `bot` and `applications.commands` scopes.
- 4 CPU / 8 GB RAM minimum. Ollama runs a small embedding model on the CPU.

## Install in six commands

```bash
git clone https://github.com/NorkzYT/agent-ops-kit.git /opt/agent-ops-kit
cd /opt/agent-ops-kit

make init                 # 1. .env with generated secrets, proxy config, proxy sources
make up                   # 2. start Honcho, Ollama, CLIProxyAPI, claude-max-proxy
make auth-codex           # 3. sign in with the ChatGPT account (device code)
make auth-claude-proxy    # 4. sign in with the Claude Max account (token is stored in .env)
$EDITOR .env              # 5. DISCORD_BOT_TOKEN, DISCORD_ALLOWED_USERS, DISCORD_HOME_CHANNEL
make hermes-install       # 6. install Hermes, render its config, start the Discord gateway
make doctor               #    everything green?
```

Then `@mention` the bot in Discord, or run `hermes chat` in a terminal.

## What each step does

**`make init`** copies `.env.example` to `.env` (mode 600), fills
`CLIPROXY_API_KEY` and `CLIPROXY_MANAGEMENT_KEY` with random values and
`PUID`/`PGID` with your uid/gid, renders `data/cliproxyapi/config.yaml`,
clones `claude-max-api-proxy` into `vendor/` so its image can be built, and
creates the `data/` directories the containers write to. Safe to re-run.

**`make up`** runs `docker compose up -d --build`. First start pulls the
Honcho, pgvector, Redis, Ollama and CLIProxyAPI images, builds the Claude proxy
image, and Ollama downloads the embedding model (about 270 MB). Honcho waits
for that, so the first `make up` takes a few minutes. Watch with `make logs`.

**`make auth-codex`** runs CLIProxyAPI's Codex device login. Open the URL it
prints, sign in with the ChatGPT account, and the OAuth state is saved under
`data/cliproxyapi/auths/`. `make models` should now list GPT models.

**`make auth-claude-proxy`** runs `claude setup-token` in a one-off proxy
container. Sign in with the Claude Max account, paste the printed
`sk-ant-oat01-...` token when asked, and the script stores it in `.env` as
`CLAUDE_CODE_OAUTH_TOKEN`, recreates the service and waits for it to answer.
Until then the container idles rather than serving. The token lasts about a
year.

**`make hermes-install`** runs the official Hermes installer if `hermes` is
missing, then writes `~/.hermes/config.yaml`, `~/.hermes/.env`,
`~/.hermes/honcho.json`, the orchestrator `SOUL.md`, and the kit skills. If a
Discord token is set it installs the gateway as a systemd user service
(`hermes-gateway.service`) and runs `hermes doctor`. Re-run it after changing
`.env`; an existing `config.yaml` is left alone and the new render is written
next to it as `config.yaml.agent-ops-kit` (use `--force` to overwrite, a `.bak`
is kept).

**`make doctor`** checks files, containers, all three HTTP endpoints, the
Ollama model, and the Hermes configuration and gateway.

## Ports

| Port | Service | Notes |
|------|---------|-------|
| 8317 | CLIProxyAPI | OpenAI-compatible, needs `Authorization: Bearer $CLIPROXY_API_KEY` |
| 3456 | claude-max-proxy | OpenAI-compatible, any non-empty key |
| 8000 | Honcho API | no auth on the local stack |
| 1455 | CLIProxyAPI OAuth callback | only during browser-based login |

All bind to `127.0.0.1`. Set `BIND_ADDR=0.0.0.0` in `.env` only when the
Windows VM worker must reach the stack, and put Tailscale or a firewall in
front of it (see [windows-vm-worker.md](windows-vm-worker.md)).

## Updating

```bash
git pull
make update          # sync proxy sources, pull images, rebuild, restart
make hermes-install  # re-render Hermes config if templates changed
hermes update        # Hermes itself
```

## Uninstall

```bash
make down            # stop containers, keep data
make clean           # stop and delete Honcho memory, Ollama models, proxy state
hermes gateway uninstall
```

## The Claude Code kit only

If you only want the `.claude/` bundle (hooks, agents, skills) in another repo:

```bash
curl -fsSL https://raw.githubusercontent.com/NorkzYT/agent-ops-kit/main/install.sh \
  | bash -s -- --repo NorkzYT/agent-ops-kit --ref main --force
```

`--bootstrap-linux` adds developer tooling and the external agent packs;
`--no-extras` skips the packs. Every install writes `.claude/install.manifest`,
and `bash .claude/scripts/self-update.sh` replays it later.
