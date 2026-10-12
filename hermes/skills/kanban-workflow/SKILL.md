---
name: kanban-workflow
description: Standard path for Discord work that needs a Mac/Windows worker or a review gate — Kanban cards, isolated worker lanes, review, final Discord report
version: 1.0.0
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

- `--lane`: `mac`, `windows`, or any profile name (for example `coder`). Repeat per lane.
- `--reviewer`: default `reviewer` (or `HERMES_WORKFLOW_REVIEWER`). It must differ from every lane.
- Add `--dry-run` first when the lanes are unclear; it prints the plan and creates nothing.
- Outside Discord (CLI, cron), pass `--key <unique>`; add `--discord-chat-id`/`--discord-thread-id` to get the report in a channel.

The script refuses (exit 2) when a lane or the reviewer profile does not exist,
or when `mac-worker`/`windows-worker` is not on the SSH terminal backend. Report
the refusal to the operator as is. Do not work around it with
`--allow-local-lane`; that flag is for tests and an operator who asks for it.

## Reply

After intake, reply in one short message: the request card id, one line per
lane card (`id  assignee`), and "I will post the report here when review
passes." The board does the rest:

- Lane workers end with `kanban_request_review`; the reviewer approves or returns the card.
- A lane that hits a login, MFA or approval blocks with `human-approval:`; the thread gets a ⏸ line.
- When every lane is done, the request card runs `workflow.py gate`, writes the report and completes.
- Its completion wakes you in this thread. Post the report from the request card's result (`kanban_show <request id>`), in plain words.

## Rules

- Never merge, deploy, release, or sign in on the operator's behalf. The report lists what needs the operator's approval.
- Never unblock a `human-approval:` card yourself; the operator does it after finishing the step.
- `hermes kanban unblock <id>` and `hermes kanban reopen-review <id>` are the operator's recovery tools.
- Status at any time: `python3 <script> status <request id>`; review gate: `python3 <script> gate <request id>`.
