# BrowserOS neo pilot (Docker + noVNC + Hermes MCP)

One dedicated, persistent browser profile in a container. You sign in to sites
yourself through a noVNC viewer; Hermes drives the same browser over MCP. This
is a pilot: nothing here is installed, started or wired into Hermes until an
operator runs the steps below.

## What runs

| Piece | Source | Host binding |
|---|---|---|
| `neo` — BrowserOS neo, Xvnc, websockify/noVNC, claw-server | upstream image, pinned by digest | viewer `127.0.0.1:16080` |
| `mcp-gate` — plain TCP forwarder to Neo's MCP port (`docker/browseros-neo/mcp-gate.py`) | same image, read-only, uid 65534, no capabilities | MCP `127.0.0.1:19010` |

Files: `docker/browseros-neo/compose.yaml`, `neo.toml`, `mcp-gate.py`;
`scripts/browseros-neo.sh` (the only supported way to run it);
`hermes/mcp/browseros-neo.yaml` (Hermes entry).

### Image identity

```
ghcr.io/browseros-ai/neo:0.52.1.2-d32f6ab@sha256:9f6302905f4db0d7d58a13e00b74cfc5c4448c5a7517a36700581a48e102d72e
```

- Labels: `org.opencontainers.image.revision=d32f6ab8dd9d6a8389b58725f9f0dde9adaec198`,
  version `0.52.1.2-d32f6ab`, `com.browseros.claw-server.version=0.0.93`, AGPL-3.0-or-later.
- Upstream source: `github.com/browseros-ai/BrowserOS`, `packages/browseros/docker/neo`.
  The image's `/usr/local/bin/neo-entrypoint` is byte-identical to that commit's
  `entrypoint.sh` (sha256 `453b3628…5d78d27`).
- The public image embeds CAPTCHA provider keys (upstream README); the Hermes
  entry excludes `solve_captcha`.
- To move the pin: change `IMAGE` in `scripts/browseros-neo.sh` and the
  anchor in `compose.yaml` together, re-read upstream's README/entrypoint for
  new ports or env, and rerun the tests.

## Use it

```bash
make browseros-neo-up          # check, start Neo, open the agent path
make browseros-neo-status
make browseros-neo-down        # containers go; the profile folder stays
```

The profile lives in `~/.local/share/agent-ops-kit/browseros-neo/data`
(`BROWSEROS_NEO_DATA`), mode 0700. It holds cookies and saved logins encrypted
with Chromium's built-in key (`--password-store=basic`): anyone who can read the
folder can read the sessions. Do not back it up anywhere less private.

### Reach the viewer (private, authenticated)

The viewer has no password of its own. It is published on the host's loopback
only; SSH is the authentication:

```bash
ssh -N -L 16080:127.0.0.1:16080 <you>@<host>     # from your own machine
# open http://127.0.0.1:16080/ locally
```

Do not publish 16080 or 19010 on another interface, a reverse proxy, a tunnel
service or Tailscale Serve/Funnel. `make browseros-neo-access` prints this.

### Sign in, then hand back

```bash
make browseros-neo-pause       # agent path closed; waits 65 s for in-flight runs
# sign in through the viewer, including MFA; close the tunnel
make browseros-neo-resume      # agent path reopened
```

You type credentials into the page yourself. Never paste them into chat; the
Hermes entry hides Neo's `credentials` and `payment_cards` tools.

## What pause does and does not do

Pause stops the `mcp-gate` container. Verified on the pinned image:

- `127.0.0.1:19010` refuses connections; `hermes mcp test browseros_neo` fails
  (exit 1); the viewer keeps working.
- Open MCP connections are cut. A script Neo already started keeps running
  inside Neo until its run budget (60 s, `neo.toml`) ends: in testing a script
  wrote to the page about 4 s after the client was cut. That is why pause
  waits 65 s before saying the browser is yours.
- The paused state is a file next to the profile; `up` honours it and leaves
  the gate stopped until `resume`.

It does **not**:

- Stop other host processes. Neo's own ports (9010 MCP + `/v1` + settings,
  9011 TLS, 6080 viewer) listen on the container's bridge address without
  authentication, and any local user or process (including an agent's terminal
  tool) can reach them there, paused or not. Docker cannot isolate a bridge
  from the host unless the network is internal, and Neo needs the internet.
  Treat the host as single-tenant and trusted.
- Lock the screen. Xvnc runs with `-AlwaysShared` and no VNC auth; every
  viewer has full control and agent actions interleave with yours if you work
  while the agent path is open. Always pause first.
- Survive bypassing the wrapper: a raw `docker compose up` starts the gate.

## Hermes connection

Hermes connects with its native MCP client over Streamable HTTP; no Hermes code
changes. Print the entry with the configured port and merge it into
`~/.hermes/config.yaml` by hand, then apply it with the operator-approved
gateway restart (docs/install.md). This pilot does not do either.

```bash
bash scripts/browseros-neo.sh hermes-snippet
```

- `trust: untrusted`: Neo marks no tool `readOnlyHint: true`, so every Neo tool
  call asks for approval (Allow once / Session / Always per tool). This is the
  fail-closed default; loosen it per tool, not per server.
- Unannotated tools are not replayed after an MCP session expires; a call cut
  by pause returns an error and must be checked before it is retried.
- `timeout: 90` sits above Neo's 60 s run budget, as upstream advises.

## What Neo records

- Every agent tool call, with nested call arguments (top-level `run` code is
  redacted, nested calls such as `evaluate_in_page` code are not) and JPEG
  screenshots, kept 7 days in `/data/browserclaw` and served on 9010 `/v1`
  without authentication. Live screencast/video of agent tabs is served there
  too while someone watches.
- Not recorded: the human's noVNC session (websockify runs without
  `--record`), rrweb page recordings (bundled extensions are off and the
  checker refuses turning them on).
- Hosted mode is required (`BROWSERCLAW_HOSTED_MODE=1`); upstream states it
  turns product analytics off and the image sets `CLAW_ANALYTICS_ENABLED=0`.
  Outbound traffic was not captured to confirm this.

## Sandbox and exposure

- Chromium runs with `--no-sandbox` (upstream default; its sandbox needs
  `seccomp=unconfined`, which the checker refuses). The container is the only
  wall: all capabilities dropped except CHOWN, DAC_OVERRIDE, FOWNER, SETUID,
  SETGID, KILL (the entrypoint drops to the `neo` user with `setpriv`),
  `no-new-privileges`, 4 GiB / 2 CPUs / 2048 pids.
- `check` (run by `up`) refuses: Docker engine older than 28.0 (earlier
  engines let LAN hosts reach 127.0.0.1-published ports), a remote
  `DOCKER_HOST`/context, any port not on 127.0.0.1, Neo's 9010/9011 published
  directly, an unpinned or different image, privileged/host namespaces,
  devices, extra capabilities, seccomp/apparmor unconfined, hosted mode off,
  the sandbox flag, bundled extensions on, `NEO_MCP_REMOTE`, `CLAW_HOST_TCP`,
  non-empty `NEO_ARGS`, docker.sock or unexpected mounts, a gate that is
  writable or root, a profile folder that is missing, a symlink, not yours,
  group/other-accessible, inside the repo, or mounted by another container.
  `COMPOSE_FILE`, `.env` and profiles are ignored so the checked config is
  the one that runs.
- After start, the live bindings are re-read; a non-loopback binding stops
  the gate and fails.

## Test

```bash
bash scripts/test_browseros_neo.sh    # offline: no containers started
```

Live checks (start, Hermes `mcp test`, page drive, pause/resume, in-flight run,
persistence across `down`/`up`) were run once by hand in a scratch project
(`BROWSEROS_NEO_PROJECT`, `BROWSEROS_NEO_DATA`, `BROWSEROS_NEO_VNC_PORT`,
`BROWSEROS_NEO_MCP_PORT` point a run at scratch values).
