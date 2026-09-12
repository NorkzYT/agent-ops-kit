# Subscription proxies

No AI-provider API keys are used anywhere in this stack. Two proxies turn the
subscriptions you already pay for into OpenAI-compatible endpoints.

| Proxy | Subscription | Port | Used by |
|-------|--------------|------|---------|
| CLIProxyAPI | ChatGPT (Codex OAuth) | 8317 | Hermes orchestrator, Honcho, Windows worker |
| claude-max-proxy | Claude Max | 3456 | Hermes coding subagents (Claude Code under the hood) |

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

Built from <https://github.com/NorkzYT/claude-max-api-proxy>, cloned into
`vendor/` by `make init`. Inside the container a real Claude Code CLI serves
`/v1/chat/completions`, so subagents get the full Claude Code toolchain against
the repos mounted from `REPOS_DIR`.

```bash
make auth-claude-proxy                 # login URL -> sk-ant-oat01-... token, stored in .env
curl -s http://127.0.0.1:3456/v1/models
```

`make auth-claude-proxy` runs `claude setup-token` in a one-off container, asks
you to paste the printed token, writes it to `.env` as
`CLAUDE_CODE_OAUTH_TOKEN`, recreates the service and waits for `/health`.
Until a token exists the container stays up but idle (its log says so); it
does not crash-loop, and `make doctor` reports it as waiting.

Settings in `.env`:

- `DEFAULT_THINKING_BUDGET` (`off|low|medium|high|xhigh|max`) applied when a
  client sends none.
- `CLAUDE_PROXY_MAX_CONCURRENT_REQUESTS` and the container's CPU/memory limits
  keep a runaway session from starving the host.
- `CLAUDE_PROXY_MAX_UPTIME_HOURS` restarts the process on a schedule so Docker
  brings it back clean.

The container runs as your host user (`PUID`/`PGID` from `make init`) with
`data/claude-max-proxy/home` as its home, so its Claude CLI state is private to
the container and never shared with a `claude` installed on the host. `gh` auth
and `.gitconfig` are mounted so the coding worker can push branches and open
PRs as you; run `gh auth login` on the host once.

### Container or host?

The kit runs the proxy in Docker on purpose: cgroup CPU, memory and pid limits
are what stop a runaway Claude Code session from freezing the host, the
uptime-based self-restart comes free with `restart: unless-stopped`, and one
`make up` brings the whole HTTP half back after a reboot. Upstream also
supports running it on the host (`npm start` with the host's `claude` login);
do that only if you would rather manage limits with a systemd unit yourself.
Hermes does not care either way, it just needs `http://127.0.0.1:3456/v1`.

## Model names

- CLIProxyAPI: whatever `make models` lists (for example `gpt-5.5`). Set
  `HERMES_MODEL` and `HONCHO_MODEL`.
- claude-max-proxy: `opus`, `sonnet`, `haiku`, `default`, or an exact id from
  its `/v1/models` (Fable appears there when the account has it). Set
  `HERMES_CODING_MODEL`.

## Reaching the proxies from the Windows VM

Set `BIND_ADDR` in `.env` to the host's Tailscale address (or `0.0.0.0` with a
firewall), `make up`, and point the worker at `http://<host>:8317/v1`. The
Windows installer does this for you (see
[windows-vm-worker.md](windows-vm-worker.md)).
