---
name: kanban-workflow
description: Standard engineering path for Discord work the Mac/Windows workers must build or test — the coordinator writes and pushes an exact SHA (Claude Opus/Fable), peers test it and return evidence, automatic fix/retest rounds, independent review, final Discord report
version: 2.5.0
metadata:
  hermes:
    tags: [kanban, discord, workers, review, workflow]
    category: devops
    requires_toolsets: [terminal, kanban]
---

# Kanban workflow

One central writer, evidence-only peers:

```
Discord request (e.g. the existing Notion CSV task set)
  -> [code N]  coordinator: Claude Opus/Fable writes the code, commit, push, record the exact SHA
  -> [mac N] [win N]  relays: the workers' own Hermes check out that SHA, build, test, install, launch -> evidence
  -> [round N]  every lane passed -> Review: (independent reviewer, exact diff + evidence)
                any lane failed   -> [code N+1] with the failure evidence -> retest ... (no operator step)
  -> Report:  gate on the final SHA + approval -> report in the same Discord thread
```

The Mac and Windows workers never edit, commit or push. The coordinator (this
host, profile `default`) is the only code writer. Every step is a Kanban card,
thus the work survives restarts; Discord only gets status lines and the report.

Script: `~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py`
(repo: `hermes/skills/kanban-workflow/scripts/workflow.py`). Runbook:
`docs/discord-kanban-workflow.md`.

## When to use it

- Engineering work that must be built or tested on macOS or Windows (iOS/Android apps, Xcode, Gradle, MSBuild, installers).
- A task set from a CSV or tracker export that the workers must validate (`!work`, "on the Mac and Windows").
- iOS/iPadOS app work: the staged mobile run (`--mobile ios`, Mac only).

Keep the old paths for everything else: a coding task that only needs this host
goes to `delegate_task`; GUI-only Windows work (computer use, BrowserStack) goes
to the `windows-operator` desktop worker. Never route coding to a local Ollama
profile; Ollama stays for explicit `#privacy` turns only.

## Intake (one command, from the Discord turn)

Read the task set first (for a Notion CSV: the rows in scope, verbatim titles and
acceptance notes). Write it to a file when it is long, then run from the terminal
tool in the Discord turn. The terminal carries the Discord chat, thread and
message ids, thus the script derives the idempotency key from the message. It
prints native ops (exit 5, see "Board transport"); creating the cards with your
`kanban_create` tool from this Discord session subscribes this thread to them.

```
terminal(command="python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py intake --title '<short title>' --body-file /tmp/<task-set>.md --repo <clone URL> --base main --lane mac --lane windows", timeout=120)
```

- `--repo`: the clone URL (`git -C <path> remote get-url origin`). Never a local path: the workers cannot reach it.
- `--branch`: the feature branch the coordinator writes (default `feat/<title>`); reuse the existing branch to continue earlier work.
- `--checkout`: only when the operator names an existing checkout on this host for the code (e.g. `/opt/repos/example-app`). Canonical absolute path to the top of a git working tree whose origin is `--repo`; intake refuses anything else. Every code card then works in that checkout instead of a fresh clone and runs `preflight` before editing: it takes a repository lease for this run (owner: board, card, run, worker process), checks remote, `index.lock`, branch (protected, diverged, in another worktree), classifies every process the registered privileged CWD scanner reports by ancestry and an explicit list (the coordinator's own chain up to its verified worker and that chain's descendants, known editors, language servers, dev servers, idle shells and read-only git are not writers; another autonomous agent, a git mutation in progress, any other program or script, or anything it cannot inspect is), and accepts uncommitted paths only when this card recorded them with `checkpoint` (full change identity: content, mode, staged object, rename origin; hashes outside the repo). `checkpoint` itself is gated: it needs this run's live lease, a clean writer scan and the baseline a passing `preflight` took under that lease, and adopts only what changed since; a dirty path that was never the card's never passes `preflight`, so it is never laundered in (the operator's explicit `adopt` is the only other route). Every lease acquire/renew/release runs under a per-repository lock, so concurrent recovery of a dead owner's lease has exactly one winner. Limit: same-uid cooperative evidence, not proof of who wrote the bytes. `record-sha` repeats the lease and writer checks and rejects untracked files. Nothing is ever reset, stashed or cleaned. Peers still validate the exact SHA from `--repo`. Omit it for the default fresh clone per card. One-time per checkout: `python3 <script dir>/repo_lease.py register-scanner /opt/repos/example-app -- sudo -n /usr/local/libexec/hermes-example-cwd-scan` (the helper must be root-owned; without a registration the unprivileged `/proc` fallback runs and fails closed when other users' processes are not inspectable).
- `--lane`: `mac`, `windows`, or a lane profile with `--peer <lane>=<peer>`. Repeat per lane.
- `--mobile ios`: iOS/iPad apps (`!work ios`). Use with `--lane mac` only. The Mac validates the exact SHA in a detached worktree of its clone under the Mac clone root (`HERMES_MAC_REPO_ROOT`, placeholder `/Users/mac-operator/Documents`) on an iPhone and an iPad simulator. When both pass and the operator activated BrowserStack, `advance` opens `[devices N]` (the Mac runs the XCUITest suite on real devices on BrowserStack) and `[devices-round N]` (pass → review, fail → next fix round). BrowserStack is disabled until then (`skipped: disabled`); a partial activation blocks the round. The Windows lane is deferred until the Windows worker is repaired, thus the script refuses `--lane windows` here. Runbook: `docs/mobile-validation.md`.
- `--max-rounds`: fix/retest rounds before the loop stops for the operator (default 5).
- `--coder`: default `default` (this coordinator). `--reviewer`: default `reviewer`; it must differ from the coder and every lane.
- Add `--dry-run` first when the lanes are unclear; it prints the plan and creates nothing.
- Outside Discord (CLI, cron), pass `--key <unique>`; add `--discord-chat-id`/`--discord-thread-id` to get the report in a channel.

The script refuses (exit 2, no cards) when the repo is not a clone URL, a lane
has no peer, a profile is missing, a peer is not registered with a key, a relay
terminal is not local, or the reviewer is not independent. Report the refusal and
its setup command to the operator as is. Do not work around it.

## Board transport (native ops, exit 5)

The scripts only read the board (`hermes kanban show --json`); every board write is
yours, with your native `kanban_*` tools, which carry your runtime identity and refuse
a `delegate_task` child. A workflow command that needs a write prints

```
{"converged": false, "ops": [{"op_id", "tool", "args", "why"}, ...], "rerun": [...], "bind_root": bool}
```

and exits 5. Call each op's `tool` (`kanban_create`, `kanban_comment`, `kanban_link`)
with its `args` verbatim, in order; never the `hermes kanban` CLI, never extra or edited
arguments. Then run `rerun` exactly; when `bind_root` is true add
`--bind <task_id the kanban_create returned>` (the id is data: it is read back and must
match the planned card). Repeat until the exit is not 5, then act on that result
(exit 0: `kanban_complete` with its `summary`/`metadata`; 4: `kanban_block` with its
`blocker`). A result is printed only after every op was read back, so metadata never
names a card or record that is not on the board. An op emitted a third time did not
land: exit 4 `native apply did not land: ...`; a card that exists but differs from the
plan (title, assignee, header, parents, creator, a native-v1 body byte for byte) is
exit 4 `board-mismatch: ...`, never a second create.

A finished command's rerun is `sync <card> --op <token>` (one token per output). Once its
ops landed, sync re-evaluates that command read-only (source, HEAD, remote branch, the
card's status and run, gates, verdicts). Anything that moved after the output, a new
write it would need, or a missing, consumed or superseded token is exit 4 with
`"stale": true`: nothing is certified, do not `kanban_complete`; run the original
command (`retry`) again explicitly.

- Card ids: new bodies say `<this card's id>`; older bodies say `$HERMES_KANBAN_TASK`. Both mean your own card id (`kanban_show`); no shell variable carries it, type the id.
- Notifications: cards you create from the Discord session are subscribed `notify+wake` (upstream default) and later rounds inherit it. A wake for a card other than the report is a status line in the thread: post at most one line, take no action, never route or create work from it.
- Records on a card count only when their native author is the role that writes them (code-sha/completion: the coder; peer-run/evidence: the lane; round: the orchestrator; verdict: the reviewer, never the coder or a lane). The old `kanban-workflow` author counts only on preserved pre-migration records of legacy cards.
- The reviewer of a verdict is the profile of the review card's running run on the board, never `HERMES_PROFILE`; `verdict` also needs that card's bound worker (below), else exit 3 and nothing is recorded. The gate counts only a verdict landed on the review card by the reviewer's runtime profile; a ledger-only line (unsent, or written by any same-UID shell) is no approval.

## Reply

After intake, reply in one short message: the report card id, the code card id,
one line per validation card (`id  assignee`), and "I will post the report here
when review passes." The board does the rest:

- The code card runs here: Claude Opus (or `#fable` for hard debugging) through `delegate_task`, then commit, push and `record-sha`.
- Relays run `dispatch` and `wait` and complete their card with the evidence, pass or fail. A login, MFA, signing or device stop blocks the card with `human-approval:`/`device:`; the thread gets a ⏸ line.
- Round cards run `advance`: review when every lane passed, else the next fix round. The thread gets `SHA_PUSHED`, `MAC_PASSED`/`WIN_FAILED`, `FIX_REQUIRED`, `VALIDATED`, `REVIEW_APPROVED` lines.
- The report card runs `gate` and completes; its completion wakes you in this thread. Post the report from its result (`kanban_show <report id>`), in plain words.

## Completion (the coordinator owns the request until it is done)

The code card's coordinator is the persistent engineering coordinator; no other
coordinator or board chain is created. Its state is `completion` records on the
request's own cards (ledger first, `completion.py`), thus a restarted run resumes:

```
python3 <script> completion status <card> [--repo-dir DIR]   # stages, "next" unmet criteria, owner, held cards
python3 <script> completion criteria <code> --item "<criterion>" ... --check unit --check lint
python3 <script> completion plan <code> --step "<step>" ...
python3 <script> completion delegate <code> --id <delegate id> --state started|complete|partial|failed|interrupted [--remaining R]
python3 <script> completion evidence <code> --repo-dir DIR --kind test --check unit --result pass|fail --command "<cmd>" --exit N
python3 <script> completion evidence <code> --repo-dir DIR --kind review --check diff --result pass|fail
python3 <script> completion recover <code> --stage test --class routine --signature S --diagnosis D --action A   # 0 go, 2 repeat refused, 4 stop
python3 <script> completion block-check <code> --reason "<reason>"   # 0: a genuine stop; 2: routine, recover instead
python3 <script> completion resolve <code> --stop <rid> --reason "<operator decision>"   # closes a recorded stop (approval: only this)
python3 <script> completion handoff <code> --repo-dir DIR             # 0: implementation handoff -> kanban_complete
python3 <script> completion final <report>                            # report card: 0 iff gate + handed-off SHA
python3 <script> completion metrics --json                            # ledger-only delivery metrics, no baseline
python3 <script> completion capabilities --repo-dir DIR               # stacks + approved capabilities; runs nothing
```

- Stages: understand → plan → implement → test (debug, fix, retest) → review → commit → push = **implementation handoff** (the code card completes, its validation children start); → validate (every lane passed the exact remote SHA) → complete (independent review + gate) = **final completion**. A delegate returning is never completion.
- Evidence is bound to the source identity (HEAD + content of every tracked and untracked file, modes, symlink targets; digests only). Any later change makes it stale; the stage is unmet again. A commit of the same bytes keeps it.
- Handoff re-reads the remote branch (`git ls-remote`); a local claim is not a push.
- Recovery: an identical (signature, diagnosis, action) repeat is refused; routine attempts are bounded per stage; exhaustion is the only routine block (`recovery-exhausted:`, work preserved). Allowed block prefixes: `human-approval:`, `device:`, `checkout:`, `capability:`, `dependency:`, `recovery-exhausted:`.
- A running owner is never taken over (not on a timeout, not after a delegate exits). The lease is cooperative same-uid evidence, not OS containment. Held cards are reported, never unblocked or promoted.
- Runbook and backlog: `docs/engineering-completion.md`.

## Rules

- Run workflow commands with the terminal tool of the card's own worker, never inside `delegate_task` or a cron run; apply their ops with your own native tools (a delegate child's native writes are refused by Hermes). No environment variable is identity: the shell carries no card id and an explicit id is verified against the board. Finishing, blocking and review requests stay your `kanban_*` tool calls.
- Repository steps on a `--checkout` card (`preflight`, `checkpoint`, `adopt`, `release`, `record-sha`, completion writes) need a task-scoped worker identity from the board: the card's current run, its `worker_bound` managed-worker interpreter as an ancestor of your shell with the recorded start time, and no other running card bound to that process chain. Without it they exit 3 `capability: task-scoped worker identity: <detail>`: change nothing and `kanban_block` with that reason. `verdict` needs the review card's bound worker the same way. The board transport (intake, dispatch, wait, advance, gate, status) keeps working. This is cooperative observation, not a protected exclusive writer.
- Unfinished work is never a reason to block or to stop: collect a running `delegate_task` child, diagnose and fix a failing test until it passes within the card's budget (retry an unchanged command once, only for a transient failure), `checkpoint` uncommitted work you made and continue. Pushing your commits to the card's branch is authorized; merges, deploys and secrets are not. Block only for a human decision (credentials, an approval named in the gates, production or destructive actions), a confirmed competing writer from `preflight`, a missing approved capability, a dependency on another card that cannot resolve from here, or exhausted bounded recovery, and only after `completion block-check` accepts the reason. A refused board call is not terminal: read its error and fix the cause.
- In a designated checkout: `preflight` before you edit (exit 0 proceed; 2 fix the named check or block with its detail verbatim; 3 unknown visibility, block), `checkpoint` after every write and before any further `preflight` (after a commit, `preflight` again first), `release` when the card ends. Never remove a lease, a lock or a file to make a check pass; never run `git reset`, `clean`, `stash`, `restore` or `rm` there.
- Go/PostgreSQL tests follow the code card's step 3, and step 3 follows the card's `Dev-test-mode`: its header only (a comment never sets a mode; an existing request changes mode only by the operator's own `hermes kanban edit --body` from `dev-test-mode body-plan`, never by an agent). A card without a mode on a worker whose setting names one, or a round card whose mode differs from its code card's, exits 3: block, do not assume the default. `compose`: never call `dev_test_run`, even when it is listed or a scanner `RECOVERY:` names it; use the project's existing Docker Compose development services under the card's inspection rules against an isolated test database (compose `exec -T`/`run --rm --no-deps`, host `go test -p 1 ./...` with race and vet, or the repository's make target or script), select `--check db-tests` when the change touches database code and record it with `--backend compose`. `dev_test_run`: only the `dev_test_run` tool; without it, block. No mode (default): call the `dev_test_run` tool (`profile`, `packages`, `run`, `race`, `vet`, `timeout_seconds`) when it is listed (it needs this run's repository lease); otherwise `kanban_block` naming the exact command, the scanner finding and the missing capability. `dev_test_run` exists only with the reviewed runtime patch 0004, which does not apply to the official runtime 71736c9f (no such tool today); do not assume one will appear. Never put a database URL, password or credential file on a terminal command line, never read that file, a project `.env` or a test container's environment, never ask for `single_query_mode: approve`, yolo or a disabled scanner. These are instructions, not OS isolation. Runbooks: `docs/dev-test-mode.md`, `docs/dev-test-runner.md`.
- Scanner recovery: a rejected command is not a blocker when an approved equivalent exists. Read the rejection; when it carries `RECOVERY:` (e.g. `dev_test_run(profile=...)`), make that call once as a new request, verify its result and continue; `kanban_block` refuses until you tried it. On a `Dev-test-mode: compose` card a `RECOVERY:` naming `dev_test_run` is not an approved equivalent: use the card's Compose path or block as step 3 says. Never retry the rejected command, wrap it in a script or encode it. No approved equivalent: block naming the exact command, the finding and the missing capability.
- Never merge, deploy, release, sign, submit to a store or sign in on the operator's behalf. The report lists what needs the operator's approval.
- Never unblock a `human-approval:` card yourself; the operator does it after finishing the step.
- Status at any time: `python3 <script> status <any card id>` (every round, SHA, lane state, peer run id); gate: `python3 <script> gate <report id>`; one remote poll: `python3 <script> wait <validation id> --timeout 0`.
- Never report a skipped stage (BrowserStack `skipped: disabled`) or the deferred Windows lane as tested. BrowserStack "passed" exists only when the gate prints it with a build id verified via the BrowserStack API.
- Never ask for, read or print BrowserStack credentials; only the operator writes `~/.hermes/browserstack.env`.
- Never read, print or ask for a peer key. `hermes peer` reads it from the lane profile's `.env`.
