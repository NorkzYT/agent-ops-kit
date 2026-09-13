# Subscription proxies

No AI-provider API keys are used anywhere in this stack. Two proxies turn the
subscriptions you already pay for into OpenAI-compatible endpoints.

| Proxy | Subscription | Runs | Port | Used by |
|-------|--------------|------|------|---------|
| CLIProxyAPI | ChatGPT (Codex OAuth) | Docker | 8317 | Hermes orchestrator, Honcho, Windows worker |
| claude-max-proxy | Claude Max | host, systemd user service | 3456 | Hermes coding subagents (Claude Code under the hood) |

Both are adapters. If one breaks, the rest of the stack keeps its shape; swap
the `base_url` and carry on.

## CLIProxyAPI (ChatGPT subscription)

Image `eceasy/cli-proxy-api`, source
<https://github.com/router-for-me/CLIProxyAPI>. The config is rendered by
`make init` from `docker/cliproxyapi/config.example.yaml` into
`data/cliproxyapi/config.yaml`; OAuth state lives in `data/cliproxyapi/auths/`.

```bash
make auth-codex        # device-code login with the ChatGPT account
make models            # list what the subscription exposes
```

Clients authenticate with `Authorization: Bearer $CLIPROXY_API_KEY`. Rotate
the key by changing it in `.env`, then `make init && make restart S=cliproxyapi
&& make hermes-install`.

The management API and control panel are disabled in the template; enable
`remote-management.allow-remote` in the example config only behind Tailscale.

## claude-max-proxy (Claude Max subscription)

Source <https://github.com/mattschwen/claude-max-api-proxy>, vendored into
`vendor/claude-max-api-proxy` and built with `npm` by
`make claude-proxy-install`. The proxy
launches a real Claude Code CLI per request (`--dangerously-skip-permissions`,
cwd `REPOS_DIR`) and serves `/v1/chat/completions`, so Hermes subagents get the
full Claude Code toolchain.

```bash
make claude-proxy-install    # node 24+ check, claude CLI, build, systemd unit, start
make auth-claude-proxy       # login URL -> sk-ant-oat01-... token, stored in .env, service restarted
make models                  # opus / sonnet / haiku / fable ...
make claude-proxy-logs       # journalctl -f
make claude-proxy-restart
make claude-proxy-update     # newer upstream sources if reachable, rebuild, restart
```

### Vendored sources

The proxy's source tree is committed to this repo (MIT licence kept alongside)
so the kit does not depend on the upstream repository staying online.
`vendor/claude-max-api-proxy/UPSTREAM` records the upstream commit it matches.

- `make claude-proxy-install` builds what is in the repo. It asks upstream
  once whether there is something newer and only prints the answer.
- `make claude-proxy-update` (also part of `make update`) replaces the
  vendored tree with upstream's current `main` when upstream is reachable,
  rewrites `UPSTREAM`, rebuilds and restarts. The change is left uncommitted
  for you to review (`git diff --stat -- vendor/`) and commit as
  `chore(proxy): sync claude-max-api-proxy to <sha>`. It refuses to overwrite
  uncommitted edits under `vendor/`.
- If upstream is unreachable or deleted, both targets say so and carry on
  with the vendored copy. Nothing breaks.

Build output (`dist/`, `node_modules/`) is ignored; the sources and
`package-lock.json` are tracked, so `npm ci` reproduces the same build.
To follow a fork instead, set `CLAUDE_MAX_PROXY_REPO` / `_REF` in `.env`.

### Why on the host and not in Docker

The proxy's job is to run Claude Code, and Claude Code's job is to work on
your repos with your tools. Inside a container that meant mounting the repos,
baking every toolchain into the image (and losing it on each rebuild), and no
`docker` at all. On the host the sessions see exactly what you see: `go`,
`node`, `uv`, `docker`, `gh`, your git identity, your repos. Upstream also
recommends the host and calls Docker optional.

The one thing the container gave you, hard limits so a runaway session cannot
freeze the host, the systemd unit keeps: `CPUQuota`, `MemoryMax` and
`TasksMax` apply to the proxy and every process it spawns (cgroup v2). Set
them in `.env`:

| `.env` | Unit setting | Default |
|--------|--------------|---------|
| `CLAUDE_MAX_PROXY_CPUS` | `CPUQuota` (x100%) | 4 |
| `CLAUDE_MAX_PROXY_MEM_LIMIT` | `MemoryMax` | 12G |
| `CLAUDE_MAX_PROXY_TASKS_MAX` | `TasksMax` | 4096 |
| `CLAUDE_PROXY_MAX_CONCURRENT_REQUESTS` | proxy queue width | 3 |

Sizing: about one core and 4 GB per concurrent session, `CPUS ~ 1.3 x
sessions`, `MEM ~ 4G x sessions`. All sessions share the Claude Max 5-hour
window, which caps usefulness well before the hardware does. Suggested
values are in `.env.example`; re-run `make claude-proxy-install` after
changing them (it re-renders the unit and restarts the service).

### How it is wired

- `~/.config/systemd/user/claude-max-proxy.service`, rendered from
  `scripts/claude-max-proxy/claude-max-proxy.service.tmpl`. `WorkingDirectory`
  is `REPOS_DIR`; `PATH` is captured from the shell that ran the install, so
  tools you can run, the subagents can run. Installed a new tool? Run
  `make claude-proxy-install` again from a shell that has it.
- `data/claude-max-proxy/proxy.env`: the proxy's environment, rendered from
  `.env` (token, port, `HOST=CLAUDE_MAX_PROXY_BIND`, the `CLAUDE_MAX_PROXY_API_KEY`
  bearer, thinking budget, concurrency).
- `data/claude-max-proxy/claude/`: the proxy's private `CLAUDE_CONFIG_DIR`.
  Its login and session state never touch your own `~/.claude`.
- `data/claude-max-proxy/data/`: conversation DB and session map.
- `scripts/claude-max-proxy/run.sh` is the unit's `ExecStart`. With no token
  it idles and logs `run make auth-claude-proxy` instead of crash-looping.
  With `CLAUDE_PROXY_MAX_UPTIME_HOURS` (default 12) it restarts the proxy
  after that long, but only once nothing is active or queued (it polls
  `/ops/snapshot`); systemd brings it straight back. Upstream does not
  implement that variable itself. Empty or 0 disables.

Other `.env` settings: `DEFAULT_THINKING_BUDGET` (`off|low|medium|high|xhigh|max`)
applied when a client sends none. Timeouts are built into the proxy per model
family (Opus and Fable: 120 s stall, 30 min hard, x3 when thinking is on).

The operator dashboard at `http://127.0.0.1:3456/` works; its banner images
404 because the proxy serves them relative to its cwd, which is `REPOS_DIR`
here rather than the checkout. Cosmetic.

Boot without a login session needs lingering enabled once:
`sudo loginctl enable-linger $USER` (the same requirement as the Hermes
gateway).

## Model names

- CLIProxyAPI: whatever `make models` lists (for example `gpt-5.6-sol`). Set
  `HERMES_MODEL` and `HONCHO_MODEL`.
- claude-max-proxy: `opus`, `sonnet`, `haiku`, `fable`, `best`, `default`, or
  an exact id from its `/v1/models`. Set `HERMES_CODING_MODEL`.

## Reaching the proxies from the Windows VM

Open only the services the worker needs, per service. Set `CLIPROXY_BIND_ADDR`
(and, if the worker uses host Honcho, `HONCHO_BIND_ADDR`) in `.env` to the
host's Tailscale address (or `0.0.0.0` behind a firewall), then
`make up && make claude-proxy-install`, and point the worker at
`http://<host>:8317/v1`. Leave every other bind at `127.0.0.1`. If you expose
the host claude-max-proxy itself, set `CLAUDE_MAX_PROXY_API_KEY` — it refuses a
non-loopback bind without one. The Windows installer wires this for you (see
[windows-vm-worker.md](windows-vm-worker.md)).
