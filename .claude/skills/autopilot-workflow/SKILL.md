---
name: autopilot-workflow
description: Universal task execution pipeline for coding agents. Mandatory for all coding tasks from any channel or cron. Ensures triage, planning, verification, testing, and reporting.
---

# Autopilot Workflow — Universal Task Execution Pipeline

Use this skill for every coding task received via any channel (chat, cron, or direct session). It ensures consistent quality regardless of the agent or model.

## Continuous Execution Rule

**Execute the entire pipeline in one continuous flow.** Do not stop between steps. Do not announce what you're about to do and then wait for the user to say "Continue."

- If you say "Let me fix X:" — fix it immediately in the same response
- If you hit an error — diagnose and retry immediately, don't announce the retry and stop
- The only reason to pause is needing a decision that ONLY the user can provide
- For cron/unattended tasks: no human is watching, so you MUST complete everything autonomously
- For chat tasks: the user expects E2E execution, not a step-by-step dialogue

## Pipeline Steps

### 1. Triage Complexity

Before touching code, classify the task:

- **What changed?** (1-5 bullet summary)
- **Files likely affected:** (list)
- **Complexity class:** Simple / Medium / Complex
- **Estimated turns:** (rough count)

Use the `model-router` skill to decide whether to escalate to Opus.

### 2. Create Status Cron (if >5 minutes)

For any task expected to take more than 5 minutes:

```
/recheckin <delay> "Status update: <task summary>"
```

**Include the cron job ID in your next message** (or state the CLI did not return one). This is mandatory — never promise a timed follow-up without a real cron job.

### 3. Plan

Write a short plan (3-10 lines) before implementing:

- What will change and why
- Which files to modify
- What tests to run
- Any risks or dependencies

For complex tasks, write the plan to `memory/YYYY-MM-DD.md` as a checkpoint.

### 4. Implement

- Follow existing code patterns (read before writing)
- Make the smallest change that satisfies the task
- One logical change per commit

**Quality principles to apply:**

- Modularity
- Abstraction & Encapsulation
- Separation of Concerns
- SOLID (Single-responsibility, Open/Closed, Liskov, Interface-segregation, Dependency-inversion)
- DRY (Don't Repeat Yourself)
- KISS (Keep It Simple, Stupid)

**Execution model:** For each change, follow Reason → Act → Observe → Repeat:
- REASON: What needs to change and why
- ACT: Make the surgical edit
- OBSERVE: Re-read the changed file, verify it matches intent
- REPEAT: If mismatch, fix before moving on

### 5. Self-Verify (Quality Gates)

After implementation, apply the `quality-gates` skill:

- Re-read every changed file (catch regressions)
- Run build command from TOOLS.md
- Run test command from TOOLS.md
- If 4+ files changed: run a self-review pass

### 6. Commit

- Use conventional commit format: `type(scope): description`
- Commit on feature branch, not main
- Never include `Co-Authored-By` trailers
- Never commit untested code

### 7. Report

Use this template for completion reports:

```
## Task Complete: <title>

**What changed:**
- <bullet 1>
- <bullet 2>

**Files modified:**
- `path/to/file.ext` — <what changed>

**Tests:**
- <test results summary>

**Status:** Done / Needs review / Blocked
```

Post the report to the channel where the task originated.

## When to Skip Steps

- **Trivial fixes** (typo, single-line change): Skip steps 2-3, still verify and report.
- **Documentation-only changes**: Skip build/test, still commit and report.
- **Cron/unattended tasks**: Always complete the full pipeline — no human is watching.

## Integration with Other Skills

- `model-router` — Called in step 1 for complexity triage
- `quality-gates` — Called in step 5 for self-verification
- `session-hygiene` — Monitors turn count throughout; may interrupt to suggest session split
