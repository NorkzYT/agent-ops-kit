---
name: gstack-office-hours
description: Use to test demand, users and approach before writing a PRD.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [product, discovery, office-hours, decisions]
    category: productivity
    requires_toolsets: [file]
---

# Office hours (adapted from gstack)

Interrogate an idea the way a demanding partner would — demand, status quo,
the specific user, the narrowest wedge — and record what is evidence, what is
opinion and what the requester still has to decide. You (the coordinator)
write one analysis JSON; `scripts/capability_workflow.py` checks it and
renders `opportunity-brief.md`. Documents only: nothing is built, filed or sent.

Method adapted from gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 (MIT;
notice and files in `references/gstack-license.md`, mapping in
`references/method-map.md`). The preamble, telemetry, update checks, second
opinion from another model, builder profile and learnings do not run.

## When to use

Use when an idea needs demand, user and approach interrogation before a PRD.

## When not to use

A known bug or a small, already-decided change goes to the existing
engineering path. Do not run office hours to fill in a planning stage.

## Reuse before asking

Read accepted problem briefs, discovery briefs and evidence first. A forcing
question whose answer is already in an accepted artifact is `covered`: quote
the artifact verbatim (`covered_by`) and do not ask it again. Never ask for a
fact the evidence pack already holds.

## Procedure

1. **Mode.** `startup` (a product with demand questions) or `builder` (a side
   project or design partner session: no forcing questions, no demand
   diagnostic, no product stage).
2. **Stage routing (startup).** pre-product → Q1 demand, Q2 status quo, Q3
   specific user; has users → Q2, Q4 narrowest wedge, Q5 observation; paying
   customers → Q4, Q5, Q6 future fit; engineering/infra → Q2, Q4. Every routed
   question is `answered` (statements), `covered` (verbatim quote) or `open`
   (asked once, at most three questions). Do not leave unrouted questions open.
3. **Demand vs opinion.** Demand evidence is observed behaviour (paying,
   expanding use, scrambling without it). Waitlists, sign-ups, "interested",
   surveys and "would use" are opinion and are listed as such.
4. **Premises.** State them; each stays `proposed` until a supplied requester
   answer reads `<premise id>: agree` or `disagree`.
5. **Approaches.** Two or three: exactly one minimal-viable, exactly one ideal
   architecture, optionally one lateral; effort, risk, pros, cons, reuse.
6. **Recommendation.** Recommend one through a decision whose options are the
   approaches. It is advice until a supplied answer reads
   `<decision id>: <option id>`; until then the result is `needs_input`.
7. **Success criteria and assignment.** Measurable metrics with thresholds; one
   concrete next action. Hand off to `pm.lean-prd` (no approval carried).
8. **Validate and render:**

```bash
python3 $KIT/scripts/capability_workflow.py validate --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS
python3 $KIT/scripts/capability_workflow.py render --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS --out $OUT
```

## Contract and example

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/office_hours`.
- Synthetic, coordinator-authored scenario: `hermes/capabilities/evals/trial-office-hours/`
  (reuses the accepted trial discovery and problem briefs; approach pending).

## Limits

No web research, outreach, mockups or second opinion run here. Approval of a
premise or an approach only comes from a supplied, recorded requester answer.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
