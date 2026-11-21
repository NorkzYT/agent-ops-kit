# BrowserOS neo pilot (Docker + noVNC + Hermes MCP)

One dedicated, persistent browser profile in a container, viewed through
noVNC, that Hermes drives over MCP. This is a pilot: nothing here is installed,
started or wired into Hermes until an operator runs the steps below.

> **Signing in to real accounts through this pilot is NO-GO.** See
> [Login status](#login-status). The viewer is for watching and for
> disposable test accounts only until those gates are proven.

## What runs

| Piece | Source | Host binding |
|---|---|---|
| `neo` — BrowserOS neo, Xvnc, websockify/noVNC (6080, no Origin check, not published), claw-server | upstream image, pinned by digest | publishes `127.0.0.1:16080` → 6081 (the `viewer`) |
| `viewer` — a second websockify from the same image, `--auth-plugin ExpectOrigin`, listening on 6081 in Neo's network namespace | same image, read-only, uid 65534, no capabilities | none of its own (`network_mode: service:neo`) |
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
APPROVE_DOWN=yes make browseros-neo-down   # operator approval: containers go; the profile folder stays
```

No wrapper command stops, removes or recreates a container on its own. Only
`down --approve-down` removes containers and only `pause` stops one (the
`mcp-gate`). When a check fails (drift, unexpected binding, inspect error,
gate that did not come up) the wrapper refuses, says that nothing was
stopped, and prints the exact approved command. `down` without the flag
refuses and does nothing.

The profile lives in `~/.local/share/agent-ops-kit/browseros-neo/data`
(`BROWSEROS_NEO_DATA`), mode 0700. It holds cookies and saved logins encrypted
with Chromium's built-in key (`--password-store=basic`): anyone who can read the
folder can read the sessions. Do not back it up anywhere less private.

### Reach the viewer (private, authenticated)

The viewer has no password of its own. It is published on the host's loopback
only; SSH is the authentication:

```bash
ssh -N -L 16080:127.0.0.1:16080 <you>@<host>     # from your own machine
# open http://127.0.0.1:16080/ locally — the same port, and 127.0.0.1, not localhost
```

The `viewer` websockify accepts a WebSocket only when `Origin` is exactly
`http://127.0.0.1:<viewer port>`; other sites, `localhost`, another port,
`https`, `null` and a missing Origin get 403. This stops a web page in the
operator's own browser from opening the desktop. It does not check `Host`,
does not authenticate, and does nothing for Neo's own 6080 (see
[Login status](#login-status)).

Do not publish 16080 or 19010 on another interface, a reverse proxy, a tunnel
service or Tailscale Serve/Funnel. `make browseros-neo-access` prints this.

### Hand the browser to a human, then back

```bash
make browseros-neo-pause       # close the agent path, then drain the pinned run budget (65 s)
make browseros-neo-status      # must say "agent path: CLOSED" (not proof of quiet; see below)
# use the viewer — NOT for real-account sign-in (Login status: NO-GO)
make browseros-neo-resume      # re-checks everything `up` checked, then reopens the path
```

`up`, `down`, `pause` and `resume` hold one lifecycle lock per profile
(`state-<project>/lock` next to the profile folder); a second one refuses
instead of waiting, so a `resume` cannot reopen the path during a drain.

Never paste credentials into chat; the Hermes entry hides Neo's
`credentials` and `payment_cards` tools.

## Login status

**NO-GO for real user login.** Do not sign in to accounts you care about
through this pilot. Two things are unproven:

1. **Cross-origin access to the desktop from inside Neo.** The image's own
   websockify (6080, websockify 0.10.0, no `--auth-plugin`) still listens in
   Neo's network namespace with no Origin check, and Xvnc (5900) has no VNC
   auth and runs `-AlwaysShared`. A page the agent opens in Neo can try
   `ws://127.0.0.1:6080` and, if it connects, see the screen and drive the
   desktop while a human types. The `viewer` service only protects the
   published port from pages in the *operator's* browser. Not yet done:
   disabling or Origin-checking 6080 inside the image, or proving a Neo page
   cannot reach it.
2. **Quiescence.** `CLOSED` means the agent's MCP path has been closed for
   the pinned run budget. It is not proof that nothing agent-planted is still
   running: page timers, injected scripts and listeners, service workers,
   open tabs, extensions or settings changed through the forwarded API can
   outlive it, and looking at the page cannot show them. Not yet done: a
   mechanism that clears or verifies that state, and a test proving no
   agent-planted code survives into the human session.

Until both are proven by a recorded test on the pinned image, the viewer is
for watching and for disposable test accounts only. Changing this status
needs an explicit operator decision, not a doc edit.

## What pause does and does not do

Pause records the pause, stops the `mcp-gate` container and verifies the gate
is not running, `127.0.0.1:19010` refuses connections and no other project's
pilot gate is running. Verified on the pinned image: Hermes' `mcp test` then
fails and the viewer keeps working.

States (`status` reconciles the record with the live gate, port and
bindings, and exits non-zero for anything unsafe or unknown):

| Status | Meaning |
|---|---|
| `OPEN` | gate running, no pause recorded |
| `DRAINING` | path closed, drain not finished (`pause --no-wait`, an interrupted pause, or a pause that found drift). Do not type secrets. |
| `CLOSED` | path closed and `pause` re-verified it after the full drain |
| `CLOSED but not drained` | gate stopped without a pause (e.g. after `down`); run `pause` first |
| `INCONSISTENT` | a pause is recorded but the path is open (raw `docker compose up`, failed stop) |
| `EXPOSED` / `UNKNOWN` | a non-loopback binding, or Docker could not answer; nothing is assumed |

- Open MCP connections are cut. A script Neo already started keeps running
  inside Neo until its run budget (60 s, `neo.toml`) ends: in testing a script
  wrote to the page about 4 s after the client was cut. Pause therefore waits
  the budget + 5 s and re-verifies the gate, port and budget file before it
  reports `CLOSED`. `pause --no-wait` only closes the path and reports
  `DRAINING`.
- The budget is pinned: `up` records the sha256 of `neo.toml` (and of the
  rendered config, `mcp-gate.py` and the engine ID) in `state-<project>/manifest`
  when it creates the containers. A file bind mount keeps the inode a
  container started with, so `up` and `resume` reuse existing containers
  (running or stopped, always `--no-recreate`) only when every recorded value
  still matches; otherwise they refuse and print the approved
  `down --approve-down`, then `up`. If `neo.toml` differs from that record,
  pause still closes the path but stays `DRAINING` with an unknown deadline
  and fails.
- **`CLOSED` is not proof the browser is quiet.** The run budget bounds a
  `run` script, not everything a tool call can leave behind: page timers,
  injected scripts and listeners, open tabs, downloads or settings changed
  through the forwarded API can outlive it, and looking at the page cannot
  show them. The wrapper never says the browser is safe to type into; see
  [Login status](#login-status).
- The pause record is a file next to the profile; `up` honours it and leaves
  the gate stopped until `resume`. If the gate is running under a pause
  record, `up` refuses (it does not stop it); run `pause`.
- `resume` re-runs every `check` (engine, profile, rendered config) and
  refuses unless the project, profile, ports, image, engine ID, rendered config,
  `neo.toml`, `mcp-gate.py` and run budget equal what `up` recorded, and the
  running Neo is the pinned image on this profile folder. All of that happens
  before the gate starts. An existing gate container is started as is (never
  recreated); its `mcp-gate.py` hash was just checked against the manifest. If
  the gate then fails to come up, `resume` refuses, reports the path as
  possibly OPEN and stops nothing; close it with `pause`.

It does **not**:

- Stop other host processes. Neo's own ports (9010 MCP + `/v1` + settings,
  9011 TLS, 6080 unchecked noVNC, 6081 Origin-checked viewer) listen on the
  container's bridge address without authentication, and any local user or process (including an agent's terminal
  tool) can reach them there, paused or not. Docker cannot isolate a bridge
  from the host unless the network is internal, and Neo needs the internet.
  Treat the host as single-tenant and trusted.
- Lock the screen. Xvnc runs with `-AlwaysShared` and no VNC auth; every
  viewer has full control and agent actions interleave with yours if you work
  while the agent path is open. Always pause first.
- Survive bypassing the wrapper: a raw `docker compose up` starts the gate.

## Hermes connection

Hermes connects with its native MCP client over Streamable HTTP; no Hermes code
changes. Do not hand-edit `~/.hermes/config.yaml`. Register the entry with
Hermes' own config commands, which validate each key:

```bash
bash scripts/browseros-neo.sh hermes-commands   # prints the commands; review, then run them
```

The commands write `enabled: false` first, then the URL, `trust: untrusted`
and the tool exclusions, and `enabled: true` last, so the server is never
enabled without its exclusions. `hermes mcp add` is not used: it offers
"enable all tools" before any exclusion is set. Apply with the
operator-approved gateway restart (docs/install.md). This pilot does neither.
`hermes-snippet` prints the same entry as YAML for review.

Exclusions verified at registration, not only in a tool listing:
`scripts/test_browseros_neo_hermes_registration.sh` (opt-in; needs a local
Hermes install) runs the printed commands in a throwaway `HERMES_HOME`,
points them at a stdlib fake MCP server that lists `credentials`,
`payment_cards`, `solve_captcha` and two browser tools, and calls Hermes'
real `discover_mcp_tools()`. Only the two browser tools land in the
`mcp-browseros_neo` toolset, trust is `untrusted`, and a control run without
exclusions registers all five. It never reads or writes the live Hermes home.

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
- `check` is an allowlist: exact service and top-level keys, exact mount
  sources (`/data` = the profile folder, `neo.toml` and `mcp-gate.py` from
  `docker/browseros-neo`), the image's own entrypoint for Neo, the exact gate
  entrypoint, a numeric non-root gate user, only `no-new-privileges`, and the
  single default-bridge `neo` network. `volumes_from`, `tmpfs`, `sysctls`,
  `extra_hosts`, `command`, external/macvlan networks and the like are refused
  because they are not on the list.
- The profile path must be canonical (`realpath` of itself: no symlinked
  parent, `..`, `//` or trailing `/`), outside this checkout and the main
  checkout, and no other container may mount it, a parent of it or a folder
  inside it.
- `check` also pins the `viewer`: exact websockify entrypoint with
  `ExpectOrigin` and the single Origin `http://127.0.0.1:<viewer port>`, no
  command, numeric non-root user, read-only, `network_mode: service:neo`
  only, depends on a healthy Neo; Neo must publish only 6081 (never 6080); a
  viewer port equal to a port Neo listens on internally (5900, 6080, 6081,
  9010, 9011) is refused.
- After `up`/`resume` start anything, the live bindings are re-read. An
  unexpected binding or an inspect error makes the command fail as `EXPOSED`
  / unknown. Nothing is stopped automatically: the message says so and
  prints the approved `down --approve-down` command for the operator.

## Test

```bash
bash scripts/test_browseros_neo.sh    # offline: no containers started
bash scripts/test_browseros_neo_hermes_registration.sh   # opt-in, local Hermes, isolated home
BROWSEROS_NEO_IMAGE_ROOT=<extracted image root> bash scripts/test_browseros_neo_viewer_origin.sh   # opt-in
```

`test_browseros_neo.sh` also drives `up`/`pause`/`status`/`resume`/`down`
against a bounded fake Docker (containers are files, the gate is a real
loopback listener, `compose config` is the real CLI): `--no-wait` stays
`DRAINING`, a held lock refuses, a gate reopened mid-drain or a silent stop
failure is caught, a raw gate start under a pause is `INCONSISTENT`, resume
refuses engine/config/`neo.toml`/image/profile drift before starting the gate,
an exposed viewer or inspect error refuses without stopping anything,
`up` refuses manifest drift with existing containers before any Compose call,
`down` without `--approve-down` makes no Compose call, and a final audit of
every engine call the wrapper made finds no `--force-recreate`, `stop` only
under `pause` (mcp-gate only), `down` only when approved, and `--no-recreate`
on every `up`.

`test_browseros_neo_viewer_origin.sh` runs the websockify package copied
read-only out of the pinned image with the exact rendered `viewer` arguments
on host loopback against a fake VNC server, and checks which Origins get 101
vs 403. It starts no containers. It does not test Neo's own 6080.

Live checks (start, Hermes `mcp test`, page drive, pause/resume, in-flight run,
persistence across `down`/`up`) were run by hand on an earlier revision,
before the `viewer` service and the no-auto-stop lifecycle; they have not
been repeated for this revision. They run in a scratch project
(`BROWSEROS_NEO_PROJECT`, `BROWSEROS_NEO_DATA`, `BROWSEROS_NEO_VNC_PORT`,
`BROWSEROS_NEO_MCP_PORT` point a run at scratch values).
