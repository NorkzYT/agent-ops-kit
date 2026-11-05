---
name: gstack-spec
description: Use to turn accepted requirements or a bug into a spec.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [engineering, spec, acceptance-criteria, issue-draft]
    category: software-development
    requires_toolsets: [file]
---

# Spec and local issue draft (adapted from gstack)

Turn accepted requirements (usually a lean PRD) or a reproduced bug into a
backlog-ready spec: verified current state, scope and what not to touch,
testable acceptance criteria traced to requirement IDs, a testing plan that
covers every criterion, files, dependencies, rollback and effort. You (the
coordinator) write one analysis JSON; `scripts/capability_workflow.py` checks
it and renders `spec.md` and `issue-draft.md`.

Method adapted from gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 (MIT;
see `references/gstack-license.md` and `references/method-map.md`). Issue
filing, agent spawning, outside-model review, the preamble and telemetry do
not run.

## When to use

Use when accepted requirements or a bug need a scoped spec and local issue draft.

## Reuse, do not repeat

Reuse the accepted lean PRD (or feature/problem brief) by ID and digest and
trace each criterion to its requirement item IDs (`r-…`, `f-…`; header,
statement and artifact IDs do not count). Do
not re-run discovery. A simple bug needs no PRD, discovery or office hours:
cite the symptom and the cause (a hypothesis is shown as unconfirmed) and add
a regression criterion that fails before the fix.

## Procedure

1. **Framing:** feature, bug or refactor. Why it matters (statements).
2. **Current state:** only verified facts (observed or labeled synthetic) or
   labeled unknowns; no assumptions dressed as state.
3. **Scope:** in scope; out of scope with reasons (required for a feature).
4. **Acceptance criteria:** Given/When/Then with an observable outcome (no
   "better", "easy", "works" without a number), a test level and a trace.
5. **Testing plan:** every criterion covered at its own level.
6. **Files, dependencies (acyclic), rules for the implementing agent,
   rollback (feature/refactor), effort with a basis.**
7. **Filing:** always `local-draft`. If publication is wanted, set
   `publish_requested` and list the `publication` approval in the next action;
   nothing is filed here. Secrets are referenced by name only: a secret-shaped
   string fails the redaction gate.
8. **Validate and render** with `scripts/capability_workflow.py validate|render`
   (`--analysis`, `--evidence`, `--artifacts`, `--out`).

## Contract and example

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/spec`.
- Synthetic scenario: `hermes/capabilities/evals/support-billing-spec/` (reuses the
  accepted billing lean PRD; one open question keeps it `needs_input`).

## Limits

Writes only the task output directory. No GitHub/GitLab call, issue, branch,
agent spawn or code edit comes with this skill; implementation goes to the
existing engineering path after its own approval.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
