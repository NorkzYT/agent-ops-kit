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
| [discord-kanban-workflow.md](discord-kanban-workflow.md) | The standard engineering path: Discord request → Kanban → the coordinator writes and pushes an exact SHA (Claude Opus/Fable) → Mac/Windows peers test it and return evidence → automatic fix/retest rounds → independent review → report in the same thread; setup and runbook |
| [mobile-validation.md](mobile-validation.md) | iOS/iPad-first staged validation (`--mobile ios`): coordinator SHA → Mac iPhone + iPad simulators (build, test, install, launch, evidence) → BrowserStack real devices (disabled, fail-closed until activated; API-verified pass only); Windows deferred; activation steps |
| [specialist-routing.md](specialist-routing.md) | Pilot: deterministic per-task routing to the book-backed profiles, privacy precedence, high-risk advisory mode, readiness, `make specialist-route` dry-run, `make specialist-status`/`-install`/`-enable` activation |
| [knowledge-refresh.md](knowledge-refresh.md) | After adding books: `make knowledge-refresh`, the report, `/learn`, Discord prompt |
| [knowledge-learn.md](knowledge-learn.md) | Headless, resumable `/learn` into `<profile>-knowledge`, one book per run: `make knowledge-learn`, `make knowledge-learn-status`, timeouts, logs, raw vs. learned |
| [knowledge-classify.md](knowledge-classify.md) | AI-proposed profiles for new books (already-staged copies detected by content), privacy gate and `/mnt/books` exemption, review checks, `APPLY=1`, `make knowledge-reconcile`, Discord prompt |
| [document-conversion.md](document-conversion.md) | Copy-only EPUB/PDF to Markdown for profile knowledge (`document-converter` skill) |
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
