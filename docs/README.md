# Documentation

The root `README.md` is the fast path. These pages hold the detail.

## The stack

| Page | What it covers |
|------|----------------|
| [install.md](install.md) | Prerequisites, the six-command install, what each step creates, updating |
| [hermes.md](hermes.md) | The Hermes orchestrator: config layout, models, Discord, cron, delegation |
| [honcho.md](honcho.md) | Long-term memory: what Honcho does, how it is wired, peers and workspaces |
| [proxies.md](proxies.md) | ChatGPT and Claude Max subscriptions as OpenAI-compatible APIs |
| [browser-use.md](browser-use.md) | Browser automation: local Chromium vs Browser Use Cloud, rules |
| [profiles-and-teams.md](profiles-and-teams.md) | Specialist agents with their own knowledge, and Kanban teams |
| [windows-vm-worker.md](windows-vm-worker.md) | The desktop worker inside the Windows VM (computer use) |
| [troubleshooting.md](troubleshooting.md) | Symptoms, causes, fixes |

## The Claude Code kit (`.claude/`)

Claude Code is the coding worker behind claude-max-proxy. The `.claude/` bundle
installs into any repo and gives Claude Code its hooks, agents and skills.

| Page | What it covers |
|------|----------------|
| [workflow.md](workflow.md) | Session persistence, notifications, guardrails, plan mode |
| [editor.md](editor.md) | External editor (`Ctrl+G`) and VS Code remote notes |
| `../.claude/CLAUDE.md` | The constitution the coding worker follows |
