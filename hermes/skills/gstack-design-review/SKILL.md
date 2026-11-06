---
name: gstack-design-review
description: Use for an evidence-bound audit of a design or existing UI.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [design, review, accessibility, audit]
    category: creative
    requires_toolsets: [file]
---

# Design review, audit only (adapted from gstack)

Audit actual supplied design artifacts — a design package from
`gstack-design-consultation` or UI evidence such as a saved page — and report
findings a reviewer can check. You (the coordinator) write one analysis JSON;
`scripts/capability_workflow.py` re-reads every reviewed artifact by digest,
runs its static checks, verifies each finding's evidence, computes grades and
renders the report. This skill has no write path to source: fixes are a
separate card on the existing engineering path.

Method adapted from gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 (MIT;
notice and file list in `references/gstack-license.md`). The upstream fix
loop, commits, browser setup, cookie import, design lint binary, outside
voices, preamble, telemetry and update checks do not run. See
`references/method-map.md`.

## When to use

Use when a design package or existing UI needs an evidence-bound design audit.

## Evidence classes (keep them apart)

- **Static checks** (always run by the validator): markup, inline CSS and
  design tokens — language, title, viewport zoom, landmarks, skip link,
  heading order, image alt, labels and accessible names, error wiring, dead
  links, focus outline removal, positive tabindex, inline handlers, remote
  resources, reduced motion, duplicate ids, copy and template tells, declared
  colour pairs and token contrast.
- **Not evaluable statically**: visual hierarchy, rendered or layered
  contrast, target size, reflow at 320px, keyboard order, screen-reader
  output. They are listed in every report and can never back a finding.
- **Captured evidence**: browser screenshots (`screenshot`, PNG/JPEG),
  assistive-technology runs (`accessibility-scan`) and usability sessions
  (`usability-session`) count only as real captured files in the evidence
  pack with a verified digest. A synthetic fixture is never browser,
  assistive-technology or usability evidence. The tool does not view them; it
  reports them as supplied by the coordinator.

## Procedure

1. **Scope.** List `reviewed_artifacts` (any review status) by ID, kind and
   digest, the review question and the surface mode. Record each evidence
   class as `not-run` or `supplied` with its sources.
2. **First impression** (only with a supplied screenshot): what it
   communicates, what you notice, the first three things the eye reaches, one
   word.
3. **Run validate once** to see the static checks, then write findings with
   the critique format: I notice / I think it matters because / what if.
   Each finding cites evidence: a failing static check, a verbatim excerpt of
   a reviewed artifact, a supplied capture or a statement. Hierarchy and
   performance findings need a browser capture. Text that reports what users,
   participants, a screen reader or a screenshot showed needs a capture of
   the matching type: screen-reader/scanner output needs an
   `accessibility-scan` capture and what people said or did a
   `usability-session` capture; a screenshot backs only what it shows.
4. **Coverage.** Every failing high or medium check is cited by a finding or
   waived with a reason. 5–10 well-evidenced findings beat 20 vague ones;
   name up to five quick wins.
5. **Handoff.** Target `existing.engineering-path`; recommendations are
   prose, never patches.
6. **Validate and render** into a fresh task output directory:

```bash
python3 $KIT/scripts/capability_workflow.py validate --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS
python3 $KIT/scripts/capability_workflow.py render --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS --out $OUT
```

The output holds `design-review-report.md`, `static-checks.json` (bound to
each artifact digest), the analysis, evidence, copied inputs,
`artifacts.json` and `result.json`. Each category starts at 4 points (A); a
high finding subtracts 1 point, a medium 0.5, and the letter is the points
rounded down (3.5 and 3 both read B). Accessibility is graded but is not part
of the weighted design score. Without browser evidence,
hierarchy and performance are not graded and categories nothing looked at are
"not assessed".

## Contract and examples

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/design_review`.
- Synthetic, coordinator-authored scenarios: `hermes/capabilities/evals/queue-page-review/`
  (a supplied page with real markup defects) and `trial-guide-review/` (the
  consultation's own preview and tokens; static checks pass, visual checks
  not run).

## Limits

No shell, network, browser, cookie, repository write, commit or install comes
with this skill. A clean static audit is not a visual, accessibility or
usability verdict.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
