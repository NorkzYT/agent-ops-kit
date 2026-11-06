---
name: gstack-plan-eng-review
description: Use for an engineering review of a PRD or spec before build.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [engineering, review, architecture, test-plan]
    category: software-development
    requires_toolsets: [file]
---

# Engineering plan review (adapted from gstack)

Review the current plan artifacts (lean PRD, spec, handoff) before anything is
built: scope challenge, architecture, code quality, tests and performance,
failure modes and a test plan for QA. You (the coordinator) write one analysis
JSON; `scripts/capability_workflow.py` re-reads each reviewed artifact by
digest, checks every quote and renders `eng-review.md` and `test-plan.md`.

Method adapted from gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 (MIT;
see `references/gstack-license.md` and `references/method-map.md`). Outside
voices, reality-row binaries, TODOS.md and plan-file writes, learnings,
telemetry and update checks do not run.

## When to use

Use when a PRD or spec needs an engineering review before implementation.

## Take the plan as given

Review the exact artifacts (any review status, current digest). Do not repeat
discovery: the contract has no questions field. Anything the requester must
choose is a decision with options and a recommendation.

## Procedure

1. **Scope challenge.** Count files and new services from the plan (quote the
   basis). 8+ files or 2+ new services trips the complexity gate: record a
   structure decision (original vs smaller arrangement). Name what already
   exists (reuse or rebuild, quoted) and the minimum change.
2. **Sections, none skipped:** architecture, code quality, tests,
   performance — each `findings` or `no-issues`. Findings quote the plan
   verbatim. Calibrate confidence: without a verbatim quote it is 5 or lower;
   3-4 goes to the appendix; 1-2 only for a speculative critical finding.
   Performance findings state scale or mark it unknown.
3. **Failure modes.** For each new path: a realistic failure, its test, its
   error handling and whether users see a clear error or nothing. No test, no
   handling and silent is a critical gap and needs a critical finding. Every
   failure mode needs a test, a planned test or a recorded acceptance.
4. **Test plan** for QA: critical paths (level, what they cover), edge cases,
   regression behaviour to keep.
5. **Decisions and dispositions.** A finding approves no remedy: accepted,
   rejected or deferred needs an answered decision (a supplied answer reading
   `<decision id>: <option id>`). Review agreement is not approval.
6. **Validate and render** with `scripts/capability_workflow.py validate|render`.

## Contract and example

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/plan_eng_review`.
- Synthetic scenario: `hermes/capabilities/evals/support-billing-eng-review/` (reviews the
  spec rendered by `support-billing-spec`; one critical gap, one pending decision).

## Limits

Audit only: no plan, source or TODO file is edited. Implementation goes to the
existing engineering path; QA consumes `test-plan.md`.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
