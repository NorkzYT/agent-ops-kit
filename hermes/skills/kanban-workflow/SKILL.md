---
name: kanban-workflow
description: Standard path for Discord work that needs a Mac/Windows worker or a review gate — Kanban cards, worker lanes on the workers' own Hermes (peer API), review, final Discord report
version: 1.1.0
metadata:
  hermes:
    tags: [kanban, discord, workers, review, workflow]
    category: devops
    requires_toolsets: [terminal, kanban]
---

# Kanban workflow

The standard path for a Discord request that needs the Mac worker, the Windows
worker, or more than one lane:

```
Discord request -> lane cards (mac-worker, windows-worker, ...) -> review gate -> request card -> report in the same Discord thread
```

The Mac and Windows lanes run on those machines' own Hermes agents. The host's
`mac-worker`/`windows-worker` profiles only relay: they send the card's remote
task with `hermes peer run`, record the remote run id on the card, poll it, and
hand the result to the reviewer here. SSH is not part of normal work.

Every step lives on the Kanban board, thus the work survives restarts and the
operator can follow it with `hermes kanban watch` or the dashboard.

Script: `~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py`
(repo: `hermes/skills/kanban-workflow/scripts/workflow.py`). Runbook:
`docs/discord-kanban-workflow.md`.

## When to use it

- The request needs macOS or Windows tooling (Xcode, simulators, MSBuild, PowerShell, installers).
- The request names a worker ("on the Mac", "on Windows", `!work`).
- The request needs two or more lanes that run in parallel.

Keep the old paths for everything else: a single coding task on this host goes
to `delegate_task`; GUI-only Windows work (computer use, BrowserStack) goes to
the `windows-operator` desktop worker.

## Intake (one command, from the Discord turn)

Run it from the terminal tool in the Discord turn. The terminal carries the
Discord chat, thread and message ids, thus the script subscribes that thread
and derives the idempotency key from the message.

```
terminal(command="python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py intake --title '<short title>' --body '<the request, verbatim>' --lane mac --lane windows --repo <url or path>", timeout=120)
```

- `--lane`: `mac`, `windows`, or any profile name (for example `coder`, which runs on this host). Repeat per lane.
- `--peer mac=<peer>[/<profile>]`: only when the operator names a different peer; the defaults are `mac` and `windows`.
- `--reviewer`: default `reviewer` (or `HERMES_WORKFLOW_REVIEWER`). It must differ from every lane.
- Add `--dry-run` first when the lanes are unclear; it prints the plan and creates nothing.
- Outside Discord (CLI, cron), pass `--key <unique>`; add `--discord-chat-id`/`--discord-thread-id` to get the report in a channel.

The script refuses (exit 2, no cards) when a lane or the reviewer profile does
not exist, when a Mac/Windows lane has no peer registered with a key, or when its
relay terminal is not local. Report the refusal and its setup command to the
operator as is. Do not work around it: never run the lane on this host, over SSH,
or as a local lane instead.

## Reply

After intake, reply in one short message: the request card id, one line per
lane card (`id  assignee`), and "I will post the report here when review
passes." The board does the rest:

- Relays run `workflow.py dispatch` and `workflow.py wait`, then end with `kanban_request_review`; the reviewer approves or returns the card (a return starts the next remote attempt).
- A lane that hits a login, MFA or approval on the worker blocks with `human-approval:`; the thread gets a ⏸ line.
- When every lane is done, the request card runs `workflow.py gate`, writes the report and completes.
- Its completion wakes you in this thread. Post the report from the request card's result (`kanban_show <request id>`), in plain words.

## Rules

- Never merge, deploy, release, or sign in on the operator's behalf. The report lists what needs the operator's approval.
- Never unblock a `human-approval:` card yourself; the operator does it after finishing the step.
- `hermes kanban unblock <id>` and `hermes kanban reopen-review <id>` are the operator's recovery tools.
- Status at any time: `python3 <script> status <request id>` (peer and remote run id per Mac/Windows lane); review gate: `python3 <script> gate <request id>`; one remote poll: `python3 <script> wait <lane id> --timeout 0`.
- Never read, print or ask for a peer key. `hermes peer` reads it from the lane profile's `.env`.
