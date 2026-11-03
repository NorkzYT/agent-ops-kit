---
name: kanban-workflow
description: Standard engineering path for Discord work the Mac/Windows workers must build or test — the coordinator writes and pushes an exact SHA (Claude Opus/Fable), peers test it and return evidence, automatic fix/retest rounds, independent review, final Discord report
version: 2.3.0
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
message ids, thus the script subscribes that thread and derives the idempotency
key from the message.

```
terminal(command="python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py intake --title '<short title>' --body-file /tmp/<task-set>.md --repo <clone URL> --base main --lane mac --lane windows", timeout=120)
```

- `--repo`: the clone URL (`git -C <path> remote get-url origin`). Never a local path: the workers cannot reach it.
- `--branch`: the feature branch the coordinator writes (default `feat/<title>`); reuse the existing branch to continue earlier work.
- `--checkout`: only when the operator names an existing checkout on this host for the code (e.g. `/opt/repos/validate`). Canonical absolute path to the top of a git working tree whose origin is `--repo`; intake refuses anything else. Every code card then works in that checkout instead of a fresh clone and runs `preflight` before editing: it takes a repository lease for this run (owner: board, card, run, worker process), checks remote, `index.lock`, branch (protected, diverged, in another worktree), classifies every process the registered privileged CWD scanner reports by ancestry and an explicit list (the coordinator's own chain up to its verified worker and that chain's descendants, known editors, language servers, dev servers, idle shells and read-only git are not writers; another autonomous agent, a git mutation in progress, any other program or script, or anything it cannot inspect is), and accepts uncommitted paths only when this card recorded them with `checkpoint` (full change identity: content, mode, staged object, rename origin; hashes outside the repo). `checkpoint` itself is gated: it needs this run's live lease, a clean writer scan and the baseline a passing `preflight` took under that lease, and adopts only what changed since; a dirty path that was never the card's never passes `preflight`, so it is never laundered in (the operator's explicit `adopt` is the only other route). Every lease acquire/renew/release runs under a per-repository lock, so concurrent recovery of a dead owner's lease has exactly one winner. Limit: same-uid cooperative evidence, not proof of who wrote the bytes. `record-sha` repeats the lease and writer checks and rejects untracked files. Nothing is ever reset, stashed or cleaned. Peers still validate the exact SHA from `--repo`. Omit it for the default fresh clone per card. One-time per checkout: `python3 <script dir>/repo_lease.py register-scanner /opt/repos/validate -- sudo -n /usr/local/libexec/hermes-validate-cwd-scan` (the helper must be root-owned; without a registration the unprivileged `/proc` fallback runs and fails closed when other users' processes are not inspectable).
- `--lane`: `mac`, `windows`, or a lane profile with `--peer <lane>=<peer>`. Repeat per lane.
- `--mobile ios`: iOS/iPad apps (`!work ios`). Use with `--lane mac` only. The Mac validates the exact SHA in a detached worktree of its clone under `/Users/norkz/Documents` on an iPhone and an iPad simulator. When both pass and the operator activated BrowserStack, `advance` opens `[devices N]` (the Mac runs the XCUITest suite on real devices on BrowserStack) and `[devices-round N]` (pass → review, fail → next fix round). BrowserStack is disabled until then (`skipped: disabled`); a partial activation blocks the round. The Windows lane is deferred until the Windows worker is repaired, thus the script refuses `--lane windows` here. Runbook: `docs/mobile-validation.md`.
- `--max-rounds`: fix/retest rounds before the loop stops for the operator (default 5).
- `--coder`: default `default` (this coordinator). `--reviewer`: default `reviewer`; it must differ from the coder and every lane.
- Add `--dry-run` first when the lanes are unclear; it prints the plan and creates nothing.
- Outside Discord (CLI, cron), pass `--key <unique>`; add `--discord-chat-id`/`--discord-thread-id` to get the report in a channel.

The script refuses (exit 2, no cards) when the repo is not a clone URL, a lane
has no peer, a profile is missing, a peer is not registered with a key, a relay
terminal is not local, or the reviewer is not independent. Report the refusal and
its setup command to the operator as is. Do not work around it.

## Reply

After intake, reply in one short message: the report card id, the code card id,
one line per validation card (`id  assignee`), and "I will post the report here
when review passes." The board does the rest:

- The code card runs here: Claude Opus (or `#fable` for hard debugging) through `delegate_task`, then commit, push and `record-sha`.
- Relays run `dispatch` and `wait` and complete their card with the evidence, pass or fail. A login, MFA, signing or device stop blocks the card with `human-approval:`/`device:`; the thread gets a ⏸ line.
- Round cards run `advance`: review when every lane passed, else the next fix round. The thread gets `SHA_PUSHED`, `MAC_PASSED`/`WIN_FAILED`, `FIX_REQUIRED`, `VALIDATED`, `REVIEW_APPROVED` lines.
- The report card runs `gate` and completes; its completion wakes you in this thread. Post the report from its result (`kanban_show <report id>`), in plain words.

## Rules

- Run workflow commands with the terminal tool of the card's own worker, never inside `delegate_task` or a cron run (Hermes refuses Kanban writes there; the script refuses to start). The worker's own terminal carries the card id as `$HERMES_KANBAN_TASK` together with `HERMES_KANBAN_SUPERVISED_TERMINAL=1` (runtime patch 0001); it may record progress on its own card and build the cards it creates, never finish, block or review a card from the shell: those are your `kanban_*` tool calls.
- Unfinished work is never a reason to block or to stop: collect a running `delegate_task` child, diagnose and fix a failing test until it passes within the card's budget (retry an unchanged command once, only for a transient failure), `checkpoint` uncommitted work you made and continue. Pushing your commits to the card's branch is authorized; merges, deploys and secrets are not. Block only for a human decision (credentials, an approval named in the gates, production or destructive actions), a confirmed competing writer from `preflight`, or a dependency on another card that cannot resolve from here. A refused board call is not terminal: read its error and fix the cause.
- In a designated checkout: `preflight` before you edit (exit 0 proceed; 2 fix the named check or block with its detail verbatim; 3 unknown visibility, block), `checkpoint` after every write and before any further `preflight` (after a commit, `preflight` again first), `release` when the card ends. Never remove a lease, a lock or a file to make a check pass; never run `git reset`, `clean`, `stash`, `restore` or `rm` there.
- Registered Go/PostgreSQL tests (e.g. Validate's matching service): call the `dev_test_run` tool (`profile`, `packages`, `run`, `race`, `vet`, `timeout_seconds`). It runs the operator-registered profile through the root-owned, digest-pinned runner in a Landlock/seccomp sandbox and returns exit code, pass/fail/skip counts and failing output; it needs this run's repository lease (`preflight`). Never put a database URL or a credential file on a terminal command line, never read that file or a test container's environment (both are blocked), never ask for `single_query_mode: approve`, yolo or a disabled scanner. Runbook: `docs/dev-test-runner.md`.
- Scanner recovery: a rejected command is not a blocker when an approved equivalent exists. Read the rejection; when it carries `RECOVERY:` (e.g. `dev_test_run(profile=...)`), make that call once as a new request, verify its result and continue; `kanban_block` refuses until you tried it. Never retry the rejected command, wrap it in a script or encode it. No approved equivalent: block naming the exact command, the finding and the missing capability.
- Never merge, deploy, release, sign, submit to a store or sign in on the operator's behalf. The report lists what needs the operator's approval.
- Never unblock a `human-approval:` card yourself; the operator does it after finishing the step.
- Status at any time: `python3 <script> status <any card id>` (every round, SHA, lane state, peer run id); gate: `python3 <script> gate <report id>`; one remote poll: `python3 <script> wait <validation id> --timeout 0`.
- Never report a skipped stage (BrowserStack `skipped: disabled`) or the deferred Windows lane as tested. BrowserStack "passed" exists only when the gate prints it with a build id verified via the BrowserStack API.
- Never ask for, read or print BrowserStack credentials; only the operator writes `~/.hermes/browserstack.env`.
- Never read, print or ask for a peer key. `hermes peer` reads it from the lane profile's `.env`.
