# Discord → Kanban → central writer → peer evidence → review → Discord

The standard path for engineering work that the Mac or Windows worker must
build or test. One coordinator writes all code; the workers only test the exact
commit it pushed and send structured evidence back. Every step is a card on the
Hermes Kanban board, thus the work survives restarts and has an audit trail.
Discord carries the request, status lines and the final report.

```
Discord request (e.g. the existing Notion CSV task set)
  └─ orchestrator runs the kanban-workflow skill (one intake command)
       [code 1] T   → default (coordinator): Claude Opus/Fable via delegate_task,
       │               commit, push, record-sha → exact SHA
       ├─ [mac 1] T → mac-worker relay      ── hermes peer run ──► the Mac's own Hermes:
       │                                        checkout SHA, build, test, install, launch → evidence
       ├─ [win 1] T → windows-worker relay  ── hermes peer run ──► the Windows PC's own Hermes: same
       └─ [round 1] T → default: `advance`
              every lane passed → Review: T → reviewer: exact diff + evidence → verdict
              any lane failed   → [code 2] (failure evidence) → [mac 2] [win 2] → [round 2] → …
  Report: T → default, waits for every round and review card; runs the gate,
              writes the report, completes → the orchestrator posts it in the same Discord thread
```

iOS/iPad apps use the staged variant: `--lane mac --mobile ios` validates on an
iPhone and an iPad simulator on the Mac, then on real iPhones and iPads on
BrowserStack App Automate, with every failure going back to the coordinator as the
next fix round. BrowserStack is disabled and fail-closed until the operator
activates it (`skipped: disabled` until then). The Windows lane is deferred until
the Windows worker is repaired. See [mobile-validation.md](mobile-validation.md).

Hermes `--parent` means "waits for". Each new round and review card is linked
into the report card before the card that opened it completes, thus the report
starts only after the last round passed and was reviewed.

| Piece | Where |
|-------|-------|
| intake, record-sha, dispatch, wait, advance, verdict, gate, status | `hermes/skills/kanban-workflow/scripts/workflow.py` |
| Orchestrator instructions | `hermes/skills/kanban-workflow/SKILL.md`, `hermes/SOUL.md` |
| Relay and reviewer prompts | `hermes/profiles/{mac-worker,windows-worker,reviewer}/SOUL.md` |
| Lane profile + peer setup | `scripts/hermes-profile.sh` (`PROFILE_PEER_URL`, `PROFILE_PEER_KEY`) |
| Discord shortcut | `!work <mac\|windows\|both\|ios> <task>` in `discord-dev-bridge` |
| iOS/iPad staged lane | [mobile-validation.md](mobile-validation.md) (`--mobile ios`) |

## Roles

| Who | Does | Never |
|-----|------|-------|
| Coordinator (`default`, code cards) | reads the task set, writes code with Claude Opus (Fable for hard debugging or broad architecture, a leading `#fable` line in the `delegate_task` goal), runs host-side checks, commits and pushes the feature branch, records the exact SHA | merge, force push, push to the base branch |
| `mac-worker` / `windows-worker` relays | send the test-only task, poll, record evidence, complete or block the card | build, test or fix on the host; switch to SSH |
| Mac / Windows Hermes (peers) | check out the exact SHA, build, test, install, launch, return evidence | edit, commit, push, branch, merge, sign, release |
| Round cards (`default`) | `advance`: review, or the next fix round | edit, test, review |
| `reviewer` | exact diff against the base, evidence for the exact SHA, verdict | push, merge, edit; approve the coordinator's or a relay's own work |
| Operator | merge, release, signing, store submission, production, logins/MFA, raising the fix-loop limit | |

Ollama stays available for explicit `#privacy` turns (see
[model-routing.md](model-routing.md)); it is not part of this workflow.

## The loop

1. **Intake.** One command builds round 1 and the report card and subscribes
   the Discord thread. The request (the CSV-derived task set) is stored
   verbatim at the end of each card.
2. **Code card.** The coordinator clones the repo URL into
   `~/hermes-work/<card>`, writes the code through `delegate_task` (Claude
   Opus; `#fable` for hard work), reads the diff, runs the host-side checks,
   commits on the feature branch and pushes. Then:
   `workflow.py record-sha <card> --repo-dir ~/hermes-work/<card> --notes "…"`.
   It checks the checkout is on the card's branch, the tracked tree is clean and
   the remote branch is at HEAD, and records `code-sha {sha, branch, notes}`.
   The card completes with `SHA_PUSHED <sha>`.
3. **Validation cards** (one per lane) become ready. The relay runs
   `workflow.py dispatch`: it builds the test-only task for the exact SHA (repo
   URL, branch, commit, platform, the coordinator's notes, the request as
   context; host paths are marked as the coordinator's) and sends it with
   `hermes -p <lane> peer run <peer> --idempotency-key kanban:<card>:<attempt>`.
4. The relay runs `workflow.py wait` until it settles:
   - `0` evidence recorded, **pass or fail**: the relay completes the card
     (`MAC_PASSED at <sha>: 42 passed …` or `WIN_FAILED at <sha>: …`).
   - `4` blocked: `human-approval:` (login, MFA, signing, a command approval on
     the worker) or `device:`. The relay blocks the card for you.
   - `1` no usable run after the automatic retries (below).
5. **Round card.** When every validation card is done, `workflow.py advance`
   reads each lane's evidence for the round's SHA:
   - every lane passed → opens `Review: T (round N)` for the reviewer
     (`VALIDATED`);
   - any lane failed → opens round N+1: a code card with the failure evidence
     (failures, failing commands and exit codes, device), fresh validation
     cards and a round card (`FIX_REQUIRED`). No operator step is needed.
   - After `--max-rounds` (default 5) failing rounds it blocks with
     `fix-loop: …`; each `hermes kanban unblock <round card>` allows one more
     round.
6. **Review.** The reviewer reads `git diff <base>...<sha>` and the evidence,
   then runs `workflow.py verdict <card> approve` or
   `verdict <card> changes --reason "1. …"`. Changes open the next fix round the
   same way a failing lane does.
7. **Report.** `workflow.py gate <report>` passes only when every card the
   report waits on is done, every lane of the last round passed that round's
   SHA with recorded evidence, and an independent reviewer approved exactly that
   SHA. The report lists the final branch and SHA, each round, each lane's
   evidence and peer run id, the review verdict, and "Needs your approval:".

### Evidence

The worker ends its reply with one JSON object between `BEGIN-EVIDENCE` and
`END-EVIDENCE` lines:

```json
{"task": "t_…", "status": "pass|fail|blocked", "sha_tested": "<full sha>",
 "platform": "…", "os": "…", "tools": "…",
 "commands": [{"cmd": "xcodebuild test …", "exit_code": 0}],
 "tests": {"passed": 42, "failed": 0, "skipped": 1}, "failures": [],
 "artifacts": ["…"], "device": "iPhone 17 simulator", "dirty_tree": false,
 "blocker": "", "summary": "…"}
```

`wait` parses it tolerantly (the marker block, else a fenced JSON block, else
the last bare JSON object with a `status`), checks the task id and that
`sha_tested` is the card's SHA, bounds and redacts every field, and stores the
normalized object as an `evidence` record (ledger + card comment), separate from
the model's prose. A dirty tracked tree or a non-zero failed count turns a
`pass` into `fail`. An echoed template (`"status": "pass|fail|blocked"`) is
rejected.

Automatic recovery, without the operator:

- completed without an evidence block → one report-only follow-up in the same
  peer session (`kanban:<card>:<attempt>:report`) asks for the block without
  re-running the build;
- failed, cancelled or interrupted run, wrong SHA or task, unusable evidence →
  the next attempt from a clean checkout, up to 3 attempts per card (each
  operator unblock adds one).

### Durable records and recovery

Every record (`peer-run`, `evidence`, `code-sha`, `round`, `verdict`) is first
appended to a local ledger under the shared Kanban root
(`~/.hermes/kanban/workflow-runs/<card>.jsonl`; `HERMES_WORKFLOW_STATE`
overrides) and then mirrored to the card as a `kanban-workflow` comment.

- `dispatch` ledgers a pending marker with the idempotency key before the peer
  call. A crash before the run id is known re-sends the same key on the next
  dispatch; the peer replays the same run.
- A failed card comment no longer loses the run: the ledger has it, `dispatch`
  and `wait` resume from it, and the next command mirrors it to the card.
- Mutating commands refuse to run inside a `delegate_task` child or a cron run
  (Hermes refuses Kanban writes there), before any remote run starts. The prompts
  tell relays and the coordinator to run them with their own terminal tool. That
  terminal carries the card id (`$HERMES_KANBAN_TASK`) and the supervised-terminal
  marker from runtime patch 0001 (`hermes/runtime-patches/README.md`); without the
  patch every `hermes kanban comment/create/link` from a worker is refused.
- Designated checkout (`--checkout`): `workflow.py preflight <code card> --repo-dir <checkout>`
  takes a repository lease (`~/.hermes/kanban/workflow-runs/leases/<key>.json`,
  owner = board, card, run, worker pid + start time), runs the registered
  privileged CWD scanner and classifies each match by process ancestry, checks
  remote, `index.lock`, branch, worktrees and uncommitted paths against the card's
  manifest (`manifests/<card>.json`, hashes only). `checkpoint` records the card's
  own uncommitted files; `release` gives the lease back. A dead owner (process
  gone, or its pid reused) is recovered; an alive owner, even with a stale
  heartbeat, is never taken over. `record-sha` repeats the lease and writer checks
  and rejects untracked files. Bootstrap of pre-existing, operator-authorized
  uncommitted work: `workflow.py adopt <code card> --repo-dir <checkout> --manifest <path: sha256 json>`
  (every listed path must match byte for byte; nothing else is adopted).
  Details and the threat boundary: `docs/hermes-autonomous-development.md`.
- An approval stop is tied to the card and run: the blocker names both
  (`… (card t_…, run run_…)`). After you approve or deny on the worker and
  unblock, `dispatch` resumes that same run while it is live, else starts the
  next attempt for the same SHA.

## One-time setup

### 1. Worker machines

Each worker gets a dedicated account with no personal logins, reachable from
this host over Tailscale, with `git`, the build tools and Hermes installed. Sign
in to GitHub (or the repo host) yourself as that account with read access;
workers never type credentials and never push.

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

The coordinator is the `default` profile: its `delegation` settings send
`delegate_task` to Claude Max through claude-max-proxy (Opus by default, Fable
with a leading `#fable` line). It needs `git` push access to the repository.

Check the link end to end before the first real job:

```bash
echo 'Reply with the word pong and your hostname.' | hermes -p mac-worker peer dm mac
```

To point a lane at a named profile on a multiplexed worker gateway, pass
`--peer mac=mac/<profile>` at intake; the peer stays `mac`.

### 3. Install the skill and prompts

```bash
make hermes-config-apply     # installs the kanban-workflow skill and the orchestrator SOUL.md; no restart
for p in mac-worker windows-worker reviewer; do cp hermes/profiles/$p/SOUL.md ~/.hermes/profiles/$p/SOUL.md; done
make hermes-runtime-patch-status   # the three runtime patches the workflow relies on (hermes/runtime-patches/README.md)
make hermes-runtime-patch-apply    # when any is "applicable"
make hermes-restart          # from your own terminal, never from inside Hermes
```

For a designated checkout, register its privileged CWD scanner once (the helper
must be root-owned and take no arguments; the sudoers entry is exact):

```bash
python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/repo_lease.py register-scanner \
  /opt/repos/validate -- sudo -n /usr/local/libexec/hermes-validate-cwd-scan
python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/repo_lease.py status --checkout /opt/repos/validate
```

Check without creating anything:

```bash
python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py intake \
  --title "smoke" --repo https://github.com/<org>/<repo>.git --lane mac --lane windows --key smoke-1 --dry-run
```

### Upgrading from peer-coding lanes (workflow 1.x)

Version 1 sent the whole request to the peers, which edited, committed, pushed
and asked for review themselves. Version 2 replaces that contract: peers only
validate. Cards created by version 1 keep their old bodies and are not driven by
the new script; let them finish or archive them, then start a new intake (for
the same work, `--key <old key>-v2 --branch <existing feature branch>`).

The `local-engineer` profile (private Ollama implementation) is removed. Coding
goes through the coordinator's Claude path; Ollama stays for explicit
`#privacy` turns. To remove it from an existing install:

```bash
hermes kanban list --assignee local-engineer                 # open cards still assigned to it
hermes kanban archive <id> …                                  # or reassign: hermes kanban reassign <id> <profile>
hermes profile export local-engineer -o ~/local-engineer-$(date +%Y%m%d).tar.gz   # optional backup
hermes profile delete local-engineer -y                       # also removes its ~/.local/bin wrapper
make hermes-config-apply                                      # model-router plugin without local-engineer
make hermes-restart
```

## Daily use

In Discord:

```
!work both Continue Submission 5 from the existing Notion CSV task set in validate
```

or plain words ("build it on the Mac and Windows workers"). The orchestrator
replies with the card ids. You then see, in the same thread, one line per
state change: `SHA_PUSHED`, `MAC_PASSED`/`MAC_FAILED`, `WIN_PASSED`/`WIN_FAILED`,
`FIX_REQUIRED` (the next round started on its own), `VALIDATED`,
`REVIEW_APPROVED` or `CHANGES_REQUESTED`, and the final report. You act only on:

- `⏸ … blocked: human-approval: …` or `device: …`: finish the step on the
  worker (approve or deny the command there, sign in, connect the device), then
  `hermes kanban unblock <id>`.
- `fix-loop: …`: decide; `hermes kanban unblock <round card>` allows one more round.
- "Needs your approval:" in the report: merge, release, signing, store
  submission, production changes.

From a terminal:

```bash
make workflow-status ID=<any card id>       # every round: SHA, lane state, evidence, peer run id
make workflow-gate ID=<report card id>      # exit 0 iff the final SHA passed and was approved
python3 hermes/skills/kanban-workflow/scripts/workflow.py wait <validation id> --timeout 0   # one poll
hermes -p mac-worker peer status mac <run id>   # raw remote status and output
hermes -p mac-worker peer stop mac <run id>     # stop a runaway remote run
hermes kanban watch                              # live events
hermes kanban diagnostics                        # stranded or stuck cards
```

## Human gates

These hold on every card, in the remote task and in the profile prompts:

- No merge, deploy, release, publish, signing, store submission or production
  change. The report lists them under "Needs your approval".
- No passwords, tokens, MFA codes or keys typed by a worker. The worker reports
  `blocked` with `human-approval:`, the relay blocks the card, and it waits for you.
- No destructive commands (`git reset --hard`, `clean -fdx`, force push) outside
  the card's own checkout without your approval.
- No Discord messages from workers or relays. Only the board's notifier and the
  orchestrator post.

## Limits

- All runs to one peer share that worker's canonical "Bot Chat" session. Each
  card works in its own `~/hermes-work/<card>` directory.
- `wait` reads a run's status while the worker gateway keeps it. A worker
  restart mid-run ends it as `interrupted`; `wait` starts the next attempt.
- Card bodies are fixed at creation; a new round gets new cards. `done` is final
  on the board.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `--repo … is not a clone URL` | a local path was passed | pass `git -C <path> remote get-url origin` |
| `--mobile ios: the Windows lane is deferred …` | Windows is not part of the mobile activation | `--lane mac --mobile ios` ([mobile-validation.md](mobile-validation.md)) |
| mac card `blocked: workspace: …` | no clone under `/Users/norkz/Documents`, a dirty reused worktree or wrong HEAD on the Mac | fix the clone on the Mac, then `hermes kanban unblock <id>` |
| `lane(s) … have no peer` | a host profile was passed as a lane | lanes are `mac`, `windows` or `--peer <lane>=<peer>`; host checks run on the code card |
| `profile 'mac-worker' does not exist` | lane profile missing | step 2 above |
| `no Hermes peer 'mac' is configured` / `has no API key` | peer not registered on the lane profile | step 2 with `PROFILE_PEER_URL` and `PROFILE_PEER_KEY` |
| `terminal.backend is 'ssh', not 'local'` | relay still set up as an old SSH lane | step 2 with `PROFILE_PEER_*`, or `hermes -p mac-worker config set terminal.backend local` |
| `… must run in the card's own worker terminal, not inside delegate_task` | a step ran in a delegate_task child | run it with the card worker's own terminal tool; nothing was started |
| code card done, validation `peer-setup: code card … has no recorded SHA` | the coordinator completed without `record-sha` | run `record-sha` for that code card from its checkout, then unblock the validation cards |
| `record-sha`: `push first` / `uncommitted or untracked paths` / `checkout is on …` | not pushed, dirty (incl. untracked files) or wrong branch | commit and push the card's branch (move files that are not the card's out of the checkout), run it again |
| `preflight` exit 2 `lease held by task … (pid …, active)` | another run of the same request (or an alive stale run) holds the checkout | wait for it; if it is a dead card, `hermes kanban show <id>`; an alive process is never taken over |
| `preflight` exit 2 `writers: pid N: agent-writer` / `git-mutation` | another autonomous agent or a git command is working in the checkout | let it finish or stop it yourself; the card retries `preflight` |
| `preflight` exit 3 `scan incomplete` / `unknown` | the registered scanner failed, is missing, or the unprivileged fallback could not inspect other users' processes | `repo_lease.py register-scanner <checkout> -- sudo -n <root-owned helper>`; check `sudo -n <helper>` runs without a password |
| `preflight` exit 2 `dirty: unknown dirty paths` | uncommitted files this card never recorded | if they are this card's: `checkpoint`; if they are authorized pre-existing work: `adopt --manifest`; otherwise the operator decides, never reset/stash |
| `this terminal belongs to card t_…, not t_…` | a command named another card | run it for `$HERMES_KANBAN_TASK` only |
| worker `hermes kanban comment …` says `delegate_task child contexts cannot mutate` | runtime patch 0001 not applied, or the command ran inside delegate_task | `make hermes-runtime-patch-status`; run from the worker's own terminal |
| card `blocked: peer-run: …` | worker offline, gateway down, key mismatch (HTTP 401), or 3 unusable attempts | `hermes -p mac-worker peer dm mac`; on the worker, `hermes gateway status`; re-run step 2 for a new key; then `hermes kanban unblock <id>` |
| round card `blocked: round N: … done without evidence` | a validation card was completed by hand | re-run that lane: unblock is not possible on `done`; start a new intake for the work with `--key <old key>-r2 --branch <same branch>` |
| report card `blocked: review-gate: …` | the final SHA lacks evidence or an independent approval | `make workflow-gate ID=<report>` lists the failing check |
| `no idempotency key` | intake ran outside a Discord turn | pass `--key <unique>` |
| card sits in `ready` | gateway down, or `max_in_progress` reached | `hermes gateway status`, `hermes kanban diagnostics` |
| no report in Discord | intake ran without a Discord origin | `hermes kanban notify-list <report id>`; subscribe with `hermes kanban notify-subscribe <id> --platform discord --chat-id <id> --delivery-mode notify+wake` |

Tests: `make test-kanban-workflow` (fake runner and temp git repositories, plus
the real `hermes kanban` and `hermes peer` CLIs on a temporary board against a
local fake peer gateway when Hermes is installed; never the live board or real
peers; and `test_repo_lease.py`: two processes racing for a lease, dead/reused
owner recovery, writer classification, scanner contract, manifests, preflight).
