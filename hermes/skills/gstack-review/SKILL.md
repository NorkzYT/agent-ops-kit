---
name: gstack-review
description: Use for an independent pre-landing audit of an exact diff.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [engineering, code-review, audit, checklist]
    category: software-development
    requires_toolsets: [file]
---

# Code review, audit only (adapted from gstack)

Audit one exact diff (a `code-change-handoff` artifact) with the gstack
pre-landing checklist and report findings bound to the lines they quote. You
(the coordinator, or the existing restricted review path) write one analysis
JSON; `scripts/capability_workflow.py` parses the diff, checks PR type,
categories and quotes, and renders `review-report.md` and
`review-findings.json`. This skill has no write path: there is no fix-first
step here.

Method adapted from gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 (MIT;
see `references/gstack-license.md` and `references/method-map.md`). Specialist
and adversarial dispatch, outside voices, auto-fix, the preamble, telemetry
and update checks do not run. Reuse the kit's existing review routes
(`requesting-code-review`, the restricted reviewer) for who reviews; this
skill defines what the review must contain.

## When to use

Use when an exact diff needs an independent pre-landing checklist audit.

## Procedure

1. **PR type** from the changed paths: docs-only, ci-infra, scripts-tooling,
   application (code under `scripts/` and skill Markdown included) or mixed.
2. **Scope check:** quote the stated intent; list scope creep (files in the
   diff) and missing requirements.
3. **Categories.** On application or mixed diffs every category runs. On
   other types, gate integrity, shell injection, completeness gaps,
   distribution/CI and repo conventions always run; the rest may be
   not-applicable, which never counts as passed. Each is `findings`, `clear`
   or `not-applicable`.
4. **Findings.** One defect class per finding with siblings; quote the exact
   diff line and cite `file:line` in the new file. Confidence: no quote → 5 or
   lower; 3-4 go to the appendix. Data loss, trust-boundary bypass and
   uncapped spend are critical on sight. Recommendations are prose, never
   patches; `fix_route` says whether the engineering path can apply it
   mechanically or needs a decision.
5. **Tests.** A test result counts only from a supplied real capture of the
   exact command with its exit status; otherwise it is "not run".
6. **Validate and render** with `scripts/capability_workflow.py validate|render`.

## Contract and example

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/code_review`.
- Synthetic scenario: `hermes/capabilities/evals/rota-export-code-review/` (shell
  injection, export deletion, scope creep; no test log).

## Limits

Read-only over the diff: no edit, commit, push, PR comment or issue. Repairs
are a separate card on the existing engineering path and need a re-review.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
