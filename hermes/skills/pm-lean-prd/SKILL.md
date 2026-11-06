---
name: pm-lean-prd
description: Use when an accepted opportunity needs a lean, testable PRD.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [product, prd, requirements, acceptance-criteria]
    category: productivity
    requires_toolsets: [file]
---

# Lean PRD (adapted from PM Skills)

Write the shortest product requirements document that lets the team decide
and start the first version. You (the coordinator) write one analysis JSON;
`scripts/capability_workflow.py` checks it against its contract, the evidence
pack and the reused upstream artifacts, then renders draft artifacts. The
script writes no PRD content.

Method adapted from PM Skills @ 2a12295a7717569baa7be181b4cfebe3ca38a6bb (MIT;
notice and file list in `references/pm-skills-license.md`). The upstream
`/write-prd` command is a Claude plugin command: it does not run in Hermes.
See `references/method-map.md`.

## When to use

Use when an accepted problem or discovery needs a lean, testable PRD.

## When not to use it

- No accepted, current opportunity exists (discovery brief, problem brief,
  decision brief or feature brief for this company/project): run
  `problem-definition` (quick) or `pm-product-discovery` first. The validator
  refuses a PRD that does not reuse one by artifact ID and digest. With one,
  do not replay the earlier stages: ask only what is still materially missing.
- A typo or reproducible bug: the existing engineering path, no PRD.
- A PRD is never an instruction to implement, open issues or publish.

## Modes

- `lean` (default): rendered body ≤ 1000 words; Contacts left out unless
  known; every section as short as it can be.
- `full`: on request; adds at least one risk with a mitigation and explicit
  constraints, and may run longer.

## Procedure

1. **Reuse and gather.** Cite the accepted upstream artifact(s) in
   `reused_artifacts`. Build the evidence pack as in `problem-definition`.
   If the problem, the target users or the success metric is still missing,
   ask at most three questions (only facts the evidence does not hold); the
   result waits for the answers. If told to go ahead, record the gaps as
   assumptions or unknowns.
2. **Sections** (numbering kept from the method; Contacts omitted when
   unknown): 1 Summary; 3 Background (cited statements); 4 Objective with 1-3
   key results (metric, baseline statement, target, window); 5 Market segments
   by job or problem, at most three; 6 one value proposition per segment —
   why, what before, how, what after, alternatives, statement; 7 Solution —
   features that deliver each segment's "how", and 7.4 assumptions and open
   questions (every `unknown` statement must be listed); 8 Release — first
   version, later, not doing, and a relative timeframe (no calendar dates).
3. **Discovery decisions bind.** When a discovery brief is reused, its
   declined ideas may appear only under "not doing"; an `idea_id` must be an
   idea from that brief.
4. **Acceptance criteria.** Every first-version item has at least one
   given/when/then criterion with a verification method. Each traces either to
   evidence (observed or synthetic statements only) or to an explicitly chosen
   requirement with its rationale. Metric checks state a number; vague results
   ("intuitive", "better", "works") are rejected.
5. **Handoff.** Name the next reviewers (`gstack.plan-design-review`,
   `gstack.plan-eng-review`, `existing.engineering-path`). The handoff is a
   draft and carries no approval.
6. **Validate and render** into a fresh task output directory:

```bash
python3 $KIT/scripts/capability_workflow.py validate --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS
python3 $KIT/scripts/capability_workflow.py render --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS --out $OUT
```

The output holds `lean-prd.md`, `acceptance-map.md`,
`implementation-handoff.md`, the analysis, evidence and copied input chain,
`artifacts.json` (new artifacts `draft`) and `result.json`. If an upstream
artifact later changes, the PRD's artifacts become stale in the index and the
planner will not reuse them.

## Contract and examples

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/lean_prd`.
- Synthetic, coordinator-authored scenarios: `hermes/capabilities/evals/trial-activation-prd/`
  (lean, built on the accepted discovery brief, declined idea excluded) and
  `support-billing-prd/` (full, built directly on a full-mode problem brief,
  one open question).

## Limits

No shell, network, issue tracker, repository write, publication or install
comes with this skill. Acceptance of the PRD is a separate reviewer decision.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
