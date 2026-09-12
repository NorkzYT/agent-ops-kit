# Troubleshooting

Start with `make doctor`. It names the failing piece and the make target that
fixes it. Then find the symptom below.

## Stack

| Symptom | Cause | Fix |
|---------|-------|-----|
| `honcho-api` stays `starting` | waiting for Ollama's model pull | `make logs S=ollama`; first pull is ~270 MB |
| `ollama` unhealthy after 15 min | pull failing (no network, disk full) | `docker exec ollama ollama pull nomic-embed-text`; check `df -h` |
| deriver logs `response_format` / `json_schema` errors | proxied model rejects strict schemas | `HONCHO_STRUCTURED_OUTPUT_MODE=json_object` in `.env`, `make up` |
| deriver logs 401 from cliproxyapi | key mismatch after rotation | `make init && make up` re-renders the proxy config |
| `make models` shows no GPT models | Codex login missing or expired | `make auth-codex` |
| `honcho-api` restarts with `embedding dim (1536) does not match EMBEDDING_VECTOR_DIMENSIONS` | database created before the kit entrypoint, or dimensions changed after data existed | `make up` (entrypoint now resizes empty tables); if data exists, `make clean && make up` |
| claude-max-proxy log says `no Claude Max credentials yet` | no `CLAUDE_CODE_OAUTH_TOKEN` | `make auth-claude-proxy` |
| claude-max-proxy `/v1/models` empty or 401 | token expired or revoked | `make auth-claude-proxy` again |
| claude-max-proxy `EACCES` / cannot write `~/.claude` | `data/claude-max-proxy` not owned by `PUID` | `sudo chown -R $(id -u):$(id -g) data/claude-max-proxy`; check `PUID`/`PGID` in `.env` |
| claude-max-proxy build fails | `vendor/claude-max-api-proxy` missing | `make init` (clones it); check `CLAUDE_MAX_PROXY_REPO` |
| host freezes during coding tasks | proxy limits too high | lower `CLAUDE_MAX_PROXY_CPUS`, `CLAUDE_MAX_PROXY_MEM_LIMIT`, `CLAUDE_PROXY_MAX_CONCURRENT_REQUESTS` |
| a port is already in use | another service on 8317/3456/8000 | change `*_PORT` in `.env`, `make up`, `make hermes-install` |

## Hermes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `hermes: command not found` after install | new PATH not loaded | `export PATH=$HOME/.local/bin:$PATH` or open a new shell |
| bot never answers | intents off, user not allowed, or no mention | enable Message Content + Server Members intents; set `DISCORD_ALLOWED_USERS`; @mention it |
| bot answers in DM but not in a channel | `require_mention` | mention it, or add the channel to `DISCORD_FREE_RESPONSE_CHANNELS` |
| gateway dies on logout | no lingering user session | `sudo loginctl enable-linger $USER` |
| model errors `401`/`invalid api key` | `~/.hermes/.env` stale | `make hermes-install` re-writes it from `.env` |
| `hermes memory status` says disconnected | Honcho down or wrong URL | `make honcho-health`; check `~/.hermes/honcho.json` |
| subagents fail immediately | claude-max-proxy not authenticated | see the proxy rows above |
| config edits ignored | gateway caches config | `hermes gateway restart` |
| `config.yaml.agent-ops-kit` appeared | you had a custom config | diff it against `config.yaml`, merge, or `scripts/hermes-install.sh --force` |

## Profiles and Kanban

| Symptom | Cause | Fix |
|---------|-------|-----|
| tasks stay `ready` | gateway not running or board missing | `hermes gateway status`; `hermes kanban init` |
| worker profile has no tools | kanban toolset disabled for it | `hermes -p <name> tools enable kanban` |
| profile answers without its knowledge | `/learn` not run in that profile | `hermes -p <name> chat`, then `/learn <folder>` |
| profile bot offline | its `.env` lacks a token or gateway not installed | `PROFILE_DISCORD_BOT_TOKEN=... make hermes-profile NAME=<name>`; `<name> gateway install` |

## Windows worker

| Symptom | Cause | Fix |
|---------|-------|-----|
| cannot reach the model | host binds to 127.0.0.1 | `BIND_ADDR=<tailscale ip>` in `.env`, `make up`; check the firewall |
| computer use does nothing | desktop locked or RDP disconnected | keep the console session open; disable lock/sleep |
| cannot click an admin window | Windows integrity levels | run the gateway task elevated for that job, or do the admin step yourself |
| scheduled task not starting | registered under another user | re-run `install-worker.ps1` from the agent's account |

## Claude Code kit

| Symptom | Cause | Fix |
|---------|-------|-----|
| hook blocks a needed command | guard pattern | add an allowlist rule in `.claude/hooks/guard_bash.py` |
| edit blocked | protected file marker | see `.claude/hooks/protect_files.py` |
| hooks not running | settings file mismatch | confirm `.claude/settings.local.json` is the one Claude loads |

Validate the bundle itself with `bash .claude/extras/doctor.sh` and
`python3 .claude/scripts/test_guard.py`.

## Reset everything

```bash
make clean               # deletes Honcho memory, Ollama models, proxy OAuth state
rm -rf data vendor .env
make init && make up
```
