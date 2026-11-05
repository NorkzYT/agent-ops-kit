---
name: gstack-plan-design-review
description: Use for a designer's review of a UI plan before building.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [design, review, interaction-states, accessibility]
    category: creative
    requires_toolsets: [file]
---

# Design plan review (adapted from gstack)

Rate a UI plan — lean PRD, design brief, design package — on the seven design
passes before implementation, with the gap behind each rating, an interaction
state table, a journey storyboard bound to plan text and the decisions that
would otherwise haunt implementation. You (the coordinator) write one analysis
JSON; `scripts/capability_workflow.py` checks it and renders
`design-plan-review.md`.

Method adapted from gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 (MIT;
see `references/gstack-license.md` and `references/method-map.md`). Mockup
generation, the design binary, outside design voices, plan-file edits,
telemetry and update checks do not run.

## When to use

Use when a UI plan or design package needs a designer's review before build.

## Procedure

1. **Step 0.** Initial completeness 0-10 with the reason and what a 10 looks
   like for this plan; design system present (a reviewed design package or
   brief) or absent (recommend `gstack-design-consultation`).
2. **Seven passes, none skipped:** information architecture, interaction
   states, journey, AI slop, design system, responsive and accessibility,
   unresolved decisions. Each is rated; below 10 names the gap; a 10 has no
   findings.
3. **State table.** Per feature: loading, empty, error, success, partial and,
   for access-controlled features, permission denied — what the user sees, or
   null when the plan does not say. Missing states keep the pass below 10 and
   need a finding.
4. **Journey.** Each step, the feeling, and the plan text that supports it
   (verbatim) or null. An unsupported step keeps the journey pass below 10.
5. **AI slop.** Hard-rejection hits quoted from the plan; an unresolved hit
   caps that pass below 8.
6. **Findings and decisions.** Quote the plan; calibrate confidence; a finding
   approves no remedy until a supplied answer settles its decision.
7. **Mockups** are never generated here: `requested` needs the `spend`
   approval in the next action.
8. **Validate and render** with `scripts/capability_workflow.py validate|render`.

## Contract and example

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/plan_design_review`.
- Synthetic scenario: `hermes/capabilities/evals/trial-guide-plan-review/` (reviews the
  trial lean PRD, design brief and package; resume and member states missing).

## Limits

Plan text only: no browser render, assistive-technology run or usability
session. No plan, design file or source is edited.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
