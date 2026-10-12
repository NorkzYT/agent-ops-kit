# Discord → Kanban → workers → review → Discord

The standard path for work that needs the Mac worker, the Windows worker, or
more than one lane. Every step is a card on the Hermes Kanban board, thus the
work survives restarts and has an audit trail.

```
Discord request
  └─ orchestrator runs the kanban-workflow skill (one command)
       ├─ [mac] <title>  → mac-worker relay      ── hermes peer run ──► the Mac's own Hermes      ─┐
       ├─ [win] <title>  → windows-worker relay  ── hermes peer run ──► the Windows PC's own Hermes ─┤ relay ends in
       │                                                                                              │ kanban_request_review → reviewer
       └─ Report: <title> → default (orchestrator), waits for every lane ◄──────────────────────────┘
            runs the review gate, writes the report, completes
            → gateway notifier wakes the orchestrator in the same Discord thread → final report
```

Hermes `--parent` means "waits for". The request card has the lane cards as
parents, thus it starts only after every lane is done.

| Piece | Where |
|-------|-------|
| Intake, dispatch, wait, gate, status script | `hermes/skills/kanban-workflow/scripts/workflow.py` |
| Orchestrator instructions | `hermes/skills/kanban-workflow/SKILL.md`, `hermes/SOUL.md` |
| Lane relay and reviewer prompts | `hermes/profiles/{mac-worker,windows-worker,reviewer}/SOUL.md` |
| Lane profile + peer setup | `scripts/hermes-profile.sh` (`PROFILE_PEER_URL`, `PROFILE_PEER_KEY`) |
| Discord shortcut | `!work <mac\|windows\|both> <task>` in `discord-dev-bridge` |

## How a peer lane runs

The board is single-host: `~/.hermes/kanban.db` is local SQLite and the
dispatcher spawns workers on this machine. The Mac and the Windows PC each run
their own Hermes gateway, and the work runs there. The link is the Hermes peer
API (`hermes peer`): authenticated HTTP to the worker gateway's `api_server`,
with the worker's `API_SERVER_KEY` as the bearer key.

1. Intake creates the `[mac]` card for the host profile `mac-worker`. That
   profile is a thin relay: a local terminal, one registered peer (`mac` by
   default) and the peer key in its own `.env`. The card body opens with
   `Transport: hermes-peer` and `Peer: mac`, then the relay steps, then the
   remote task: the request, the work contract and the JSON report format.
2. The relay runs `workflow.py dispatch <card>`. It sends the remote task with
   `hermes -p mac-worker peer run mac --idempotency-key kanban:<card>:<attempt>`
   and records `peer-run {transport, peer, run_id, session_id, attempt, ...}` as a
   card comment. A retried dispatch resumes the recorded run; the peer replays
   the same idempotency key instead of starting the work twice.
3. The relay runs `workflow.py wait <card>`. It polls `hermes peer status` and
   reads the JSON block at the end of the remote reply:
   `ready_for_review` → exit 0 and the relay calls `kanban_request_review`;
   `blocked` or a remote command approval → exit 4 and the relay blocks the card
   for you; failed, interrupted or no report → exit 1 and the relay blocks.
   Still running → exit 3; the relay waits again.
4. The reviewer on the host reviews the pushed branch as before. A change
   request sends the card back to the relay, and `dispatch` starts attempt 2 on
   the worker with the reviewer's reason. An unblock after a finished run does
   the same with "the operator cleared the block".

The remote agent never touches the host board. The host board stays the system
of record, and only the host's independent reviewer can approve a lane.

What never leaves the host: the peer key. `hermes peer` reads it from the lane
profile's `.env` (`HERMES_PEER_MAC_KEY`); the workflow never reads, passes or
prints it. Card bodies hold the peer name only. `wait` masks credential-looking
strings in the remote report before the relay copies it to the board.

A local lane (`--lane coder`, any profile name) still runs on this host as a
normal Kanban worker, without a peer.

## One-time setup

### 1. Worker machines

Each worker gets a dedicated account with no personal logins, reachable from
this host over Tailscale, with `git`, the build tools and Hermes installed. Sign
in to GitHub (or the repo host) yourself as that account; workers never type
credentials.

Run that account's Hermes gateway with the API server on the Tailscale
address. In the worker's `~/.hermes/.env`:

```bash
API_SERVER_ENABLED=true
API_SERVER_HOST=100.x.y.z          # the worker's Tailscale IP, not 0.0.0.0
API_SERVER_PORT=8642
API_SERVER_KEY=<long random key>   # e.g. openssl rand -hex 32; keep it out of chat and git
```

Restart the worker's gateway from its own terminal (`hermes gateway restart`)
and keep it running (`hermes gateway install` for a login service). The API
server gives full tool access to whoever holds the key; limit the Tailscale ACL
to this host.

### 2. Profiles on this host

```bash
make hermes-profile NAME=reviewer
PROFILE_PEER_URL=http://mac-worker.tailnet:8642 PROFILE_PEER_KEY='<Mac API_SERVER_KEY>' \
  make hermes-profile NAME=mac-worker
PROFILE_PEER_URL=http://win-worker.tailnet:8642 PROFILE_PEER_KEY='<Windows API_SERVER_KEY>' \
  make hermes-profile NAME=windows-worker
hermes -p mac-worker peer list         # mac      http://mac-worker.tailnet:8642  [key set]
hermes -p windows-worker peer list     # windows  http://win-worker.tailnet:8642  [key set]
```

The script registers the peer (`mac` for `mac-worker`, `windows` for
`windows-worker`; `PROFILE_PEER_NAME` overrides), writes the key to the profile's
`.env` (mode 600, never on a command line) and sets the relay's terminal to
`local`. Put a leading space before the command, or read the key into a
variable first, to keep it out of your shell history.

Check the link end to end before the first real job:

```bash
echo 'Reply with the word pong and your hostname.' | hermes -p mac-worker peer dm mac
```

To point a lane at a named profile on a multiplexed worker gateway, pass
`--peer mac=mac/coder` at intake; the peer stays `mac`.

### 3. Install the skill and prompts

```bash
make hermes-config-apply     # installs the kanban-workflow skill and the new SOUL.md; no restart
make hermes-restart          # from your own terminal, never from inside Hermes
```

Check without creating anything:

```bash
python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py intake \
  --title "smoke" --lane mac --lane windows --key smoke-1 --dry-run
```

The dry run prints each lane's transport and peer. The real intake fails closed
(exit 2, no cards) when a lane profile is missing, its peer is not registered or
has no key, or its terminal is not `local`, and prints the setup command.

### Upgrading from SSH lanes

Earlier versions ran the lane's terminal on the worker over SSH. Re-run step 2
with `PROFILE_PEER_*`; it switches the relay's terminal back to `local`. The SSH
keys and `terminal.ssh_*` settings can stay: SSH is now an optional
administration and recovery path (restarting a stuck worker gateway, reading its
logs), not part of normal work.

## Daily use

In Discord:

```
!work both Fix the installer path bug in velvet and run the Windows and macOS test suites
```

or plain words ("run this on the Mac and Windows workers"). The orchestrator
replies with the card ids. You then see, in the same thread:

- `⏸ … blocked: human-approval: …` when a lane needs you (login, MFA, admin
  prompt, a command approval on the worker, a merge). Finish the step on the
  worker, then `hermes kanban unblock <id>`; the relay resumes or restarts the
  remote run.
- Review lines as lanes hand off to the reviewer, and `🛑 … requested changes`
  when the reviewer returns one.
- The final report when every lane passed review. Mac/Windows lanes list their
  peer and remote run id.

From a terminal:

```bash
make workflow-status ID=<request card id>   # cards, plus [peer <name> run <run id>] per peer lane
make workflow-gate ID=<request card id>     # exit 0 iff every lane passed independent review
python3 hermes/skills/kanban-workflow/scripts/workflow.py wait <lane id> --timeout 0   # remote run, once
hermes -p mac-worker peer status mac <run id>   # raw remote status and output
hermes -p mac-worker peer stop mac <run id>     # stop a runaway remote run
hermes kanban watch                              # live events
hermes kanban diagnostics                        # stranded or stuck cards
```

## Review gate

A lane card passes only when it is `done`, its last `kanban_request_review` was
followed by a completion, no unanswered change request sits in between, and the
completing run came from a profile other than the implementer. A peer lane also
needs a recorded peer run: a Mac/Windows card done without one did not run on
the worker and fails the gate. The request card runs `workflow.py gate` first
and blocks with `review-gate: …` when any lane fails; the report never claims
unreviewed work as done.

While a lane sits in `review`, `hermes kanban reopen-review <id> --reason "…"`
sends it back to its relay. `done` is final on the board: a lane completed
without review cannot be reopened, only re-run (see Troubleshooting).

## Human gates

These hold on every lane, in the card body, in the remote task and in the
profile prompts:

- No merge, deploy, release or publish. The report lists them under "Needs your approval".
- No passwords, tokens, MFA codes or keys typed by a worker. The remote agent
  reports `blocked` with `human-approval:`, the relay blocks the card, and it
  waits for you.
- No Discord messages from workers or relays. Only the board's notifier and the orchestrator post.

## Limits

- All runs to one peer share that worker's canonical "Bot Chat" session. Each
  card works in its own `~/hermes-work/<card>` directory, and parallel cards to
  the same worker still share one conversation history.
- `wait` reads the run's status while the worker gateway keeps it. When the
  worker restarts mid-run, the run ends as `interrupted`; the relay blocks the
  card, and an unblock starts the next attempt.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `profile 'mac-worker' does not exist` | lane profile missing | step 2 above |
| `no Hermes peer 'mac' is configured` / `has no API key` | peer not registered on the lane profile | step 2 with `PROFILE_PEER_URL` and `PROFILE_PEER_KEY` |
| `terminal.backend is 'ssh', not 'local'` | relay still set up as an old SSH lane | step 2 with `PROFILE_PEER_*`, or `hermes -p mac-worker config set terminal.backend local` |
| card `blocked: peer-setup: …` / `peer-run: Could not reach peer` | worker offline, gateway down, or API server off | `hermes -p mac-worker peer dm mac` from step 2; on the worker, `hermes gateway status`; SSH in to restart it if needed |
| `peer-run: … (HTTP 401)` | key mismatch | re-run step 2 with the worker's current `API_SERVER_KEY` |
| `peer-run: remote run completed without the JSON report` | the remote agent ended without the report block | read it with `hermes -p mac-worker peer status mac <run id>`, then `hermes kanban unblock <id>` for a new attempt |
| `no idempotency key` | intake ran outside a Discord turn | pass `--key <unique>` |
| lane card sits in `ready` | gateway down, or `max_in_progress` reached | `hermes gateway status`, `hermes kanban diagnostics` |
| request card `blocked: review-gate: …` | a lane was completed without independent review or without a peer run | review that lane's branch yourself, or re-run it: a new intake for that lane with `--key <old key>-r2`; then `hermes kanban archive <old request id>` |
| no report in Discord | intake ran without a Discord origin | `hermes kanban notify-list <request id>`; subscribe with `hermes kanban notify-subscribe <id> --platform discord --chat-id <id> --delivery-mode notify+wake` |

Tests: `make test-kanban-workflow` (fake runner, plus the real `hermes kanban`
and `hermes peer` CLIs on a temporary board against a local fake peer gateway
when Hermes is installed; never the live board or real peers).
