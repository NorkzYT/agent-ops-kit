---
name: discord-dev-bridge
description: Map short Discord commands to engineering actions (ship, test, review, status)
version: 2.0.0
metadata:
  hermes:
    tags: [discord, workflow, shortcuts]
    category: devops
    requires_toolsets: [terminal, delegation]
---

# Discord dev bridge

Short operator commands and what they mean. Treat the text after the command
as the task.

| Command | What to do |
|---------|------------|
| `!ship <task>` | Delegate the full engineering loop (`autonomous-engineering`) to a coding subagent in the current repo; report branch, tests, commits. |
| `!test` | Run the repo's test suite (`npm test`, `pytest`, `go test ./...`, whatever `TOOLS.md`/`Makefile` says); report pass/fail counts and failing names. |
| `!review <PR#>` | `gh pr diff <PR#>`, review for correctness and risk; report summary, issues, recommendation. |
| `!status` | `git status`, current branch, ahead/behind, open tasks in `.claude/context/`, Kanban tasks assigned to you. |
| `!deploy` | Deployment readiness only: tests pass, clean tree, branch up to date. Never deploy. |
| `!ask <question>` | Answer from the codebase with file references. |
| `!browse <url>` | Open the page with the browser tools and return a screenshot plus a two-line summary. |
| `!cron list` | `hermes cron list`. |
| `!memory <query>` | Search Honcho (`honcho_search`) and session history. |
| `!board` | `hermes kanban list`, grouped by status. |

## Formatting
- Discord replies: short paragraphs, code blocks for logs and diffs, threads for anything long.
- Colours are not available; lead with `OK`, `FAIL` or `WARN`.

## Safety
- Same guard rails as everywhere: no `sudo`, no destructive removal, no commits to `main`.
- `!ship` works inside the repo named in the message or the current terminal cwd only.
