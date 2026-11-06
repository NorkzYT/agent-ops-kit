---
name: pm-product-discovery
description: Use when an accepted problem needs ideas, risks and tests.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [product, discovery, assumptions, experiments]
    category: productivity
    requires_toolsets: [file]
---

# Product discovery (adapted from PM Skills)

Move from an accepted problem frame to a small set of stress-tested ideas and
cheap experiments. You (the coordinator) do the thinking and write one
analysis JSON; `scripts/capability_workflow.py` checks it against its contract,
the evidence pack and the reused problem frame, then renders draft artifacts.
The script writes no analysis content.

Method adapted from PM Skills @ 2a12295a7717569baa7be181b4cfebe3ca38a6bb (MIT;
notice and file list in `references/pm-skills-license.md`). The upstream
`/discover` command is a Claude plugin command: it does not run in Hermes and
nothing here expands it. See `references/method-map.md`.

## When to use

Use when an accepted problem needs ideas, risky assumptions and tests.

## When not to use it

- No accepted, current problem or decision brief exists for this
  company/project: run `problem-definition` (quick) first. The validator
  refuses a discovery analysis that does not reuse one by artifact ID and
  digest.
- A known code bug or a dictated change: use the existing engineering path.
- Competitor or market questions: that is public research, a separate
  capability, and only when the task's egress allows it.

## Pick the branch

| Mode | Use for | Risk categories | Experiment methods |
|------|---------|-----------------|--------------------|
| `existing` | continuous discovery on a live product with real users | value, usability, viability, feasibility | prototype-test, fake-door, technical-spike, ab-test, wizard-of-oz, behavioral-survey, data-analysis |
| `new` | initial discovery: should this product exist at all | the four above plus ethics, go-to-market, strategy, team (at least one go-to-market) | landing-page, explainer-video, email-campaign, pre-order, concierge-mvp |

## Procedure

1. **Reuse the frame.** Cite the accepted problem/decision brief in
   `reused_artifacts` (ID, kind, digest). Name the discovery question, the
   decision it informs and one desired outcome metric with baseline, target
   and window.
2. **Evidence pack.** Same rules as `problem-definition`: every fact is a
   statement with a class; numbers come from sources or executed
   computations; topics listed in `not_available` stay `unknown` with a
   resolution. Never simulate interviews, sign-ups or test results.
3. **Opportunities.** Frame each as the customer's need or pain ("I can't
   …"), not a feature, and cite the statements behind it.
4. **Ideas from the product trio.** Generate ideas from the product, design
   and engineering perspectives (all three must appear) and tie each to an
   opportunity.
5. **Checkpoint: idea selection.** Ask the requester which ideas to carry.
   Until their answer is in the evidence pack (`answer_source_id`: interview
   notes, a document, or a fixture in a synthetic task), every idea
   stays `proposed`. A `declined` idea records the reason and may not return
   in assumptions, experiments or a later PRD.
6. **Assumptions for carried ideas.** Devil's advocate from all three
   perspectives; every carried idea needs a value assumption. Rate impact and
   risk (uncertainty × cost of being wrong) and follow the matrix: high/high
   = `test` (ranked 1..n), high/low = `accept-risk`, low/low = `defer`,
   low/high = `drop-idea`.
7. **Checkpoint: assumption focus.** The requester confirms what to test
   first; no experiments are designed while it is pending.
8. **Experiments.** Every `test` assumption gets at least one experiment from
   the branch's method list. Each states the semantic hypothesis (population,
   behavior, threshold, window, intervention, measurement, counter-metric,
   failure rule), effort, and what happens if it passes or fails. Measure
   behavior, not opinions; in a new product, a tested viability assumption
   needs a skin-in-the-game test (pre-order or concierge). A/B and fake-door
   tests on a live product need `risk_mitigation`. Methods that reach people
   outside the task list the `outreach`/`publication` approval; the result
   then waits for it. `result` is always `not-run`.
9. **Validate and render** into a fresh task output directory:

```bash
python3 $KIT/scripts/capability_workflow.py validate --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS
python3 $KIT/scripts/capability_workflow.py render --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS --out $OUT
```

`$ARTIFACTS` is the Artifact v1 index holding the accepted problem frame; its
files are verified against their digests. Fix every reported error in the
analysis. The output holds `discovery-brief.md` (with the idea decisions a
lean PRD later checks), `assumption-register.md`, `experiment-plan.md`,
`analysis.json`, `evidence-pack.json`, the copied inputs, `artifacts.json`
(all new artifacts `draft`) and `result.json`.

## Contract and examples

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/product_discovery`.
- Synthetic, coordinator-authored scenarios: `hermes/capabilities/evals/trial-activation-discovery/`
  (existing branch) and `landscaper-discovery/` (new branch, approvals pending,
  public research denied). Their `inputs/` were rendered by
  `problem-definition` from the earlier eval scenarios and accepted as a
  synthetic fixture decision.

## Limits

No shell, network, customer contact, publication, spend or install comes with
this skill. A rendered plan is a draft; running any experiment is a separate,
approved step. Schema validity is necessary, not sufficient.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
