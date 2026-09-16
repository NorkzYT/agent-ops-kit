---
name: model-router
description: Complexity-based model routing for coding agents. Opus is the default; triage tasks into Simple/Medium/Complex to downshift simple work to Sonnet and to pick the right process weight.
---

# Model Router — Complexity-Based Routing

Use this skill at the start of every coding task. The kit defaults to **Opus** (`.claude/settings.json`): the lean process tiers (see `.claude/eval/FINDINGS.md`) fund the stronger model, so routing means deciding when to **downshift** simple work to Sonnet — not when to beg for Opus.

## Triage Template

Before starting work, classify the task:

```
## Triage: <task title>
- Scope: <1-2 sentence summary>
- Files: <estimated count and which modules>
- Pattern: Existing / New / Architectural change
- Risk: Low / Medium / High (regression potential)
- Classification: Simple / Medium / Complex
```

## Classification Rules

### Simple (Downshift to Sonnet)

Mechanical work that doesn't need Opus-level reasoning. Run it on Sonnet (or stay put if the session is already on a smaller model) and work directly.

- 1-2 files affected
- Follows an existing pattern in the codebase
- Low regression risk
- Examples: bug fix, config change, docs update, test addition, styling tweak

### Medium (Stay on Opus, single review pass)

Work directly on the default model with one focused review pass. Extra care on verification.

- 3-4 files affected
- Bounded scope (clear start and end) — several small deliverables are still Medium when there is no architectural change (deliverable count alone is not complexity; see `.claude/eval/FINDINGS.md`)
- Moderate regression risk
- Examples: new endpoint with tests, refactor within one module, feature following existing patterns

### Complex (Stay on Opus, full pipeline)

Run the full autopilot pipeline for higher assurance. If the session is currently on a smaller model (downshifted or launched that way), escalate to the Opus pipeline.

- 4+ files across different modules/packages
- Requires architectural decisions (new patterns, service boundaries, a new subsystem/abstraction)
- High regression risk (core business logic, auth, data layer)
- Benefits from specialist review (security, performance, type system)

**How to escalate from a smaller model:**

```
Use the autopilot-opus subagent (Task tool with subagent_type=autopilot-opus) for this task: <description>
```

If the workspace has a Claude Code installation, this invokes the `autopilot-opus` agent via the Claude CLI bridge.

## Decision Matrix

| Signal | Simple | Medium | Complex |
|--------|--------|--------|---------|
| Files changed | 1-2 | 3-4 | 4+ |
| Modules touched | 1 | 1-2 | 3+ |
| Pattern | Existing | Existing | New/Architectural |
| Deliverables | 1 | several (bounded) | any, if architectural |
| Regression risk | Low | Medium | High |
| **Model** | Sonnet (downshift) | Opus | Opus |
| **Process** | Direct, inline verify | Direct + one review pass | Full pipeline |

## Common Mistakes

- **Forgetting to downshift:** A docs tweak or one-file pattern-following fix on Opus burns weekly usage for no quality gain. Simple work belongs on Sonnet.
- **Downshifting complex work:** A "simple" task that touches auth + database + API + tests is Complex. Count the distinct modules, not just the files — keep it on Opus.
- **Skipping triage:** Always write the triage template, even for tasks that seem obvious. It takes 10 seconds and prevents wrong-model execution.

## Cost Awareness

- Opus costs ~5x more tokens than Sonnet, so downshifting the mechanical 40-60% of tasks is what keeps the Opus default affordable.
- The eval evidence (`.claude/eval/FINDINGS.md`) showed process overhead, not model strength, was the wasted spend — pay for reasoning, trim ceremony.
- When in doubt, stay on Opus: a wrong answer costs more than the token delta. Downshift only when the task is clearly mechanical.
