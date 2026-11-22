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
| [browseros-neo.md](browseros-neo.md) | Pilot: BrowserOS neo in Docker, noVNC viewer over SSH (real-account login NO-GO), Hermes MCP, pause/resume, opt-in `hermes-enable`, preferred-browser skill |
| [marketing-video.md](marketing-video.md) | Marketing videos: pinned local HyperFrames default, explicit legacy Remotion lane (no fallback), claims/rights/project-asset gates, QA, `make marketing-video-*` |
| [../tools/ui-audit-pilot/README.md](../tools/ui-audit-pilot/README.md) | Pilot: axe-core, Lighthouse CI (filesystem upload only) and the Impeccable detector (advisory) on owned 127.0.0.1 fixtures; `make ui-audit` |
| [../tools/rtk-pilot/README.md](../tools/rtk-pilot/README.md) | Experiment: RTK output-compression replay on captured fixtures (pinned binary, no hook/proxy); result on the dev VM: unmeasured (SIGILL) |
| [profiles-and-teams.md](profiles-and-teams.md) | Specialist agents with their own knowledge, and Kanban teams |
| [discord-kanban-workflow.md](discord-kanban-workflow.md) | The standard engineering path: Discord request → Kanban → the coordinator writes and pushes an exact SHA (Claude Opus/Fable) → Mac/Windows peers test it and return evidence → automatic fix/retest rounds → independent review → report in the same thread; setup and runbook |
| [engineering-completion.md](engineering-completion.md) | The coordinator owns a task until it is complete: stages and criteria, source-bound evidence, bounded recovery and genuine blocks, implementation handoff vs final completion, ledger metrics; the current milestone and deferred backlog |
| [mobile-validation.md](mobile-validation.md) | iOS/iPad-first staged validation (`--mobile ios`): coordinator SHA → Mac iPhone + iPad simulators (build, test, install, launch, evidence) → BrowserStack real devices (disabled, fail-closed until activated; API-verified pass only); Windows deferred; activation steps |
| [dev-test-runner.md](dev-test-runner.md) | **Rejected/disabled candidate** (fails closed) + approved-design proposal needing operator approval. Typed `dev_test_run` tool for Kanban workers: root-owned, digest-pinned runner for registered Go/PostgreSQL tests in a Landlock/seccomp sandbox, scanner-recovery policy, credential and production-database guards, threat limits, install and smoke |
| [dev-test-mode.md](dev-test-mode.md) | `agent_ops_kit.dev_test` in Hermes config (`hermes config set`; `AOK_*` env aliases): per-request database-test path (`compose` never calls `dev_test_run`; `dev_test_run` only), durable `Dev-test-mode:` card header, native-op migration of an existing card, fail-closed values, what is enforced vs instruction only |
| [hermes-autonomous-development.md](hermes-autonomous-development.md) | (Historical; superseded by the native board transport on the official runtime.) Audit and repair of autonomous Kanban development on the Ubuntu dev VM: incident root causes (`t_12595b96`), the supervised worker terminal and the three Hermes runtime patches, repository leases + competing-writer classification + change manifests (`repo_lease.py`), headless approvals, threat boundaries, tests, rollout and remaining gates |
| [capability-registry.md](capability-registry.md) | Source-only capability catalog: registry, upstream lock, schemas, company/project manifests, computed readiness, read-only `make capability-plan`; what is and is not claimed |
| [capability-activation.md](capability-activation.md) | Milestone E proposal (not applied): exact selected-file manifest, inert bundle, rehearsal-only apply/rollback, installed-Hermes compatibility, activation gates |
| [knowledge-reference-migration-plan.md](knowledge-reference-migration-plan.md) | Plan (not applied) for moving profile knowledge references to the configured canonical store with identity checks and rollback |
| [specialist-routing.md](specialist-routing.md) | Pilot: deterministic per-task routing to the book-backed profiles, privacy precedence, readiness, `make specialist-route` dry-run, `make specialist-status`/`-install`/`-enable` activation |
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
