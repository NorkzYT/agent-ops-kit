---
name: autonomous-engineering
description: End-to-end engineering loop for unattended coding tasks (Discord, cron, Kanban)
version: 2.1.0
metadata:
  hermes:
    tags: [engineering, workflow, git, verification]
    category: devops
    requires_toolsets: [terminal, file]
---

# Autonomous engineering

Use this skill when a coding task arrives from Discord, a cron job or the
Kanban board and must be finished without a human in the loop.

## Session state (three-file pattern)

For multi-session work keep `.claude/context/<task>/` in the repo:
`plan.md` (architecture, rarely changes), `context.md` (learnings, gotchas),
`tasks.md` (granular checklist, updated often).

## The loop

1. **Accept** the task; restate goal and acceptance criteria.
2. **Branch**: `feat/<slug>`, `fix/<slug>` or `chore/<slug>`. Never `main`.
3. **Plan** (3-10 lines): files, dependencies, risks.
4. **Implement**: read before writing, follow repo patterns, smallest change.
5. **Build** with the repo's build command (`TOOLS.md`, `Makefile`, `package.json`).
6. **Run locally** (`yarn dev`, `make up`, `docker compose up`) when the change touches a running service.
7. **Test**: run the suite; for UI changes, open the flow with the browser tools.
8. **Commit**: Conventional Commits, no `Co-Authored-By`, no `--amend`, no force push.
9. **Push** the branch and open a PR for the operator.
10. **Report**: files changed, test counts, branch, remaining work. Pushing is implementation
    handoff, not completion: when validation lanes or a review follow, the task is complete
    only after they pass on the exact pushed SHA (docs/engineering-completion.md).

## Quality Principles to Apply

• Modularity  
• Abstraction & Encapsulation  
• Separation of Concerns  
• SOLID (Single-responsibility, Open/Closed, Liskov, Interface-segregation, Dependency-inversion)  
• DRY (Don’t Repeat Yourself)  
• KISS (Keep It Simple, Stupid)

## Error recovery

- Test failure: diagnose, patch, re-verify until it passes. Each new attempt needs a changed
  diagnosis or approach (never the identical failing command again); the bound is the
  task's budget and, on a `kanban-workflow` card, `workflow.py completion recover`
  (bounded per stage, then `recovery-exhausted:` with the work preserved). A failing test
  is never a reason to stop early or to report a partial result as done.
- Transient network error: wait 30 s, retry the unchanged command once; then diagnose.
- Git conflict: report it; never force-resolve.
- Expired web session: ask the operator to log in; never type credentials.

## Timed follow-ups

Promise a check-in only after `hermes cron create` succeeds, and quote the job id.

## Browser rules

- Screenshots before and after interactions (audit trail).
- Read-only on external sites unless the task says otherwise.
- Re-snapshot after navigation; element refs go stale.
- Two to five seconds between page loads; 30 s page timeout, then report.

## API discovery pattern

Capture network traffic while walking the target flow, extract endpoints, auth
headers and pagination, document them in `context.md`, generate a typed client,
and test only GET requests against the live API. Never send POST/PUT/DELETE to
an external API without explicit approval.

## Report template

```
Task: <description>
Branch: <name>
Status: Complete | Partial | Failed
Changes: <n> files
Tests: <pass>/<total>
Commits: <n>
Remaining: <what is left>
```
