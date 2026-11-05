---
name: problem-definition
description: Use when a vague problem needs an evidenced decision brief.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0
metadata:
  hermes:
    tags: [product, strategy, problem-definition, evidence]
    category: productivity
    requires_toolsets: [file]
---

# Problem definition

Turn a vague business, product, process or operational problem into a decision
frame the requester can act on. You (the coordinator) do the thinking and write
one analysis JSON; `scripts/capability_workflow.py` checks it against its
contract and the supplied evidence and renders the draft artifacts. The script
writes no analysis content and adds no facts.

Original Agent-Ops-Kit procedure (GPL-3.0). It names general analytical
methods; no RO-PNA or SoloSquad text, prompt, template or data is used. See
`references/methods-and-provenance.md`.

## When to use

Use when a problem is vague. Build an evidenced decision brief.

## When not to use it

- A known, reproducible code bug or typo: use the existing engineering path
  (`systematic-debugging`, `autonomous-engineering`). An analysis with
  `problem_type: code-defect` is rejected.
- An already accepted, current problem brief exists for this company/project:
  reuse it by artifact ID and digest instead of re-interviewing
  (`reused_artifacts`), and move to the next capability.
- A dictated one-line change: do it; do not invent alternatives.

## Pick the smallest mode

| Mode | Use for | Sections |
|------|---------|----------|
| `quick` (default) | one scoped problem, decision soon | decision, affected, symptom, evidence, unknowns, 2-3 alternatives, success measure, next action; rendered body ≤ 450 words |
| `causal` | recurring failure or performance/process gap | quick + branched why tree + partition (criterion, overlaps, omitted, interaction) |
| `full` | explicitly requested structured analysis | causal + SCQA frame, issue map, customer/process causal map, measurable hypothesis, one-page draft PRD, stage log |

In `full` mode every stage gets a `stage_log` entry: `done`, or `skipped` /
`reordered` with a reason. If the requester asked for the whole exercise
(`requested_full_exercise: true`) nothing may be skipped.

## Procedure

1. **Bind and gather.** Confirm company/project/task from the task envelope.
   Collect supplied evidence into an evidence pack
   (`hermes/capabilities/schemas/evidence-pack.schema.json`): every item has a
   source ID, kind, classification (`real`/`synthetic`), topics and the
   excerpt you rely on; numeric facts go in `values`. List facts that are not
   available in `not_available` with the reason. Do not fetch public sources
   here; public research is `pm-competitor-research`, and only when the task's
   egress allows it.
2. **Frame the decision.** Name the decision question, its owner, the affected
   people or process, and the symptom (an evidenced statement).
3. **Write statements, not prose.** Each statement has one class:
   `observed` (real source), `synthetic` (fixture data, never an observation),
   `assumption`, `hypothesis`, `estimate` (needs a basis or computation) or
   `unknown` (needs a resolution: how to retrieve or measure it). Every number
   in an observed/synthetic statement must appear in its cited sources or come
   from a computation. Do arithmetic as `computations` (`+ - * /` over source
   values); the validator re-executes it and rejects a wrong result.
4. **Keep unknowns unknown.** Each `not_available` topic needs an `unknown`
   statement with that topic and may not appear as a fact or estimate. Never
   simulate interviews, market sizes or conversion results.
5. **Causal work (causal/full).** Build the why tree from the symptom. Branch
   when there are several causes; stop when a branch reaches a testable cause
   or the evidence runs out — no fixed number of whys. Label every link:
   `observed-association` (evidenced), `causal-hypothesis` (a hypothesis
   statement plus a falsification test), `experiment-supported` (only with an
   experiment source) or `unknown-link`. Correlation stays an association.
   Declare the partition criterion, overlaps, omitted cases and, when causes
   branch, how they interact.
6. **Full-mode extras.** SCQA (situation and evidenced complication, the
   question, a *candidate* answer), an issue map over why-tree nodes with
   distinct priorities, a causal map from leading problems to the outcome
   metric, one measurable hypothesis with the semantic fields (population,
   behavior_or_outcome, threshold, window_days, intervention, rationale,
   measurement, counter_metric, failure_rule), and a one-page PRD that stays
   `draft`.
7. **Decide the next step.** At least two alternatives (one may be
   do-nothing), a success measure with baseline, target and window, at most
   three questions — only material facts the evidence does not already cover —
   and one next action. If it would contact anyone outside the task, list the
   `outreach` approval; the result then waits for it.
8. **Validate and render** into a fresh task output directory:

```bash
python3 $KIT/scripts/capability_workflow.py validate --analysis $ANALYSIS --evidence $EVIDENCE
python3 $KIT/scripts/capability_workflow.py render --analysis $ANALYSIS --evidence $EVIDENCE --out $OUT
```

Fix every reported error in the analysis; do not weaken a statement's class to
pass. The output directory receives `decision-brief.md` (quick) or
`problem-brief.md`, `assumption-register.md`, `one-page-prd.md` (full),
`analysis.json`, `evidence-pack.json`, `artifacts.json` (Artifact v1, all
`draft`) and `result.json` (CapabilityResult v1). `needs_input` means open
questions or approvals remain.

## Contract and examples

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/problem_definition`.
- Synthetic evaluation scenarios (coordinator-authored, labeled synthetic):
  `hermes/capabilities/evals/support-billing-backlog/` (quick and full on the
  same problem), `trial-activation-causes/` (two interacting causes,
  observational data), `landscaper-invoicing-unknowns/` (no interviews, market
  size or conversion data).

## Limits

Loading this skill implies no shell, repository change, customer contact,
database query, spend, memory write or install. A rendered brief is a draft:
acceptance is a separate reviewer decision recorded on the artifact. Schema
validity is necessary, not sufficient — read the brief before handing it on.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
