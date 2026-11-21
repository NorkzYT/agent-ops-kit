# BrowserOS neo: same-host development candidate (on 12fe982)

Operator decision (2026-11-21): run the pilot in this development VM, on
the same host as the agents, and accept that limitation. This is not proof
of isolation. It does not authorize weakening any control, public
listeners, production deployment or a gateway restart. Install and Hermes
config activation each need their own exact-scope approval.

Base: `feat/browseros-neo-pilot` at `12fe9823d3dfe9c18dfa86d1145f3ed23fb89bdf`.
Earlier evidence is unchanged and still applies:
`docs/browseros-neo-review-aa54759.md`, `docs/browseros-neo-review-6c32567.md`.
The untracked `scripts/test_browseros_neo__retain.sh` is untouched (sha256
`26b4efdb7dd48f78985daff48003603c5d2cc64857e2a722fabb81b22662db54`).

## Changes

| File | Change |
|---|---|
| `scripts/browseros-neo.sh` | `handoff --trusted-host-dev`: same gates, then records `trust_mode=same-host-dev` + `trust_accepted_at` and prints the trusted-host terms (no isolation guarantee, general status stays NO-GO). `status` shows the mode. `access` names the exception. `hermes-commands` warns on stderr that the last command is the live activation (no restart). |
| `scripts/lib/browseros_neo_check.py` | refuses a `websockify-6080` writable by group/other (a umask-002 checkout gives 0775). |
| `scripts/test_browseros_neo.sh` | `BROWSEROS_NEO_TEST_KEEP=1` keeps scratch. The manifest-mode check reads the on-disk mode instead of assuming 755. Adds 775/757/777 shim refusals, trusted-host handoff tests (refusals still hold, text, record, calls, status, cleared on resume) and the hermes-commands warning. |
| `scripts/test_browseros_neo_hermes_registration.sh` | `BROWSEROS_NEO_TEST_KEEP=1`; staged reconcile: nothing connects before `enabled true`, filtered tools after it. |
| `docs/browseros-neo.md`, `hermes/mcp/browseros-neo.yaml` | Same-host development section. Hermes activation corrected (reconcile, not a restart). Live re-run recorded. Test notes. |

`neo-handoff.py`, `compose.yaml`, `mcp-gate.py`, `websockify-6080`,
`chromium-policy.json` and `neo.toml` are unchanged, so an existing
deployment's manifest hashes stay valid.

## Evidence (scratch `~/.hermes/cache/scratch/neo-samehost-12fe982/`, kept)

| Run | Source | rc | Result |
|---|---|---|---|
| `test_browseros_neo_handoff_live.sh` (live, `--network none`, no ports) | HEAD 12fe982 | 0 | 34/0 (`live.out`; container `bneo-handoff-live-1795260829-1378194` exited by itself, kept) |
| `test_browseros_neo.sh` (retained copy) | HEAD 12fe982 | 0 | 211/0 (`suite.HEAD.out`) |
| candidate `test_browseros_neo.sh` | HEAD tree via `git archive` | 1 | 215/12. RED: 3 shim modes, hermes warning, 8 trusted-host checks (`suite.red2.out`) |
| `test_browseros_neo.sh` | candidate | 0 | 228/0 |
| `test_browseros_neo_hermes_registration.sh` | candidate, Hermes `6b21d0564d` | 0 | 5/0 |
| `test_browseros_neo_handoff_unit.py` | candidate (= HEAD) | 0 | 31/0 |
| `test_browseros_neo_viewer_origin.sh` (extracted pinned image) | candidate | 0 | passed (10 ok) |

The full Compose lifecycle (`up`/`pause`/`handoff`/`status`/`resume`/`down`)
was exercised only through the suite's bounded fake Docker, which uses the
real `compose config`. No real pilot project was started: `pause` is a
`compose stop`, which this task excludes. The manual login path (pause →
handoff → viewer sign-in → resume) was tested without secrets: fake
Docker for the wrapper, plus the live container for the scrub. No real
account was used.

## Traces

Operator browser: own browser → `ssh -N -L 16080:127.0.0.1:16080` →
host `127.0.0.1:16080` (Docker publish, loopback only, engine ≥ 28) →
`viewer` websockify on 6081 in Neo's namespace (`ExpectOrigin
http://127.0.0.1:16080`) → Xvnc `localhost:5900` (no VNC auth,
`-AlwaysShared`). Neo's own 6080 refuses every WebSocket.

Hermes: `mcp_servers.browseros_neo.url http://127.0.0.1:19010/mcp` →
`mcp-gate` (published loopback) → `neo:9010/mcp`. The installed Hermes
`6b21d0564d` discovers it at gateway start (`discover_mcp_tools`), on
`/reload-mcp`, and on every housekeeping tick through "MCP config
reconcile" (`reconcile_mcp_servers_with_config`). Absent `enabled` counts
as on. Tools land as `mcp-browseros_neo` / `mcp__browseros_neo__*` with
`trust: untrusted` (approval per call). Not verified: whether the running
gateway process loaded this revision, and the tick interval.

## Activation plan (each step needs its own approval; none was run)

1. Install approval: `chmod go-w docker/browseros-neo/websockify-6080` if
   needed, then `make browseros-neo-up` (default project, 127.0.0.1:16080 and
   :19010 must be free) and `make browseros-neo-status`. It must report `agent path: OPEN`.
2. Config activation approval: `bash scripts/browseros-neo.sh hermes-commands`,
   review, run against the live home. No gateway restart. Check with
   `hermes mcp list` / a tool listing that only the filtered tools appear.
3. Sign-in, per session: `make browseros-neo-pause` → `status` says CLOSED →
   `scripts/browseros-neo.sh handoff --trusted-host-dev --clear-origin https://…`
   (every sign-in origin) → SSH tunnel and viewer, with no agent task
   running → `make browseros-neo-resume`.
4. Revoke: `hermes config set mcp_servers.browseros_neo.enabled false`, or
   `pause`. Remove: `APPROVE_DOWN=yes make browseros-neo-down` (operator).

## Remaining gates

- The host-process bypass is open (accepted for same-host development only).
  A shared or multi-user host or production use needs the separate design.
- The real-Docker wrapper lifecycle on this revision is not run. Neither is
  the gateway tick connecting the entry in the live runtime, nor
  `PORT_PROBE` against the real image.
- The empty service-worker listing on a never-registered profile is not
  directly observed. If it is missing, `handoff` fails closed.
- Not pushed.
