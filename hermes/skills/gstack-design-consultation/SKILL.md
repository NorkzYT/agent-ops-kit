---
name: gstack-design-consultation
description: Use when an accepted brief needs a design system + preview.
version: 0.1.0
author: Agent-Ops-Kit
license: GPL-3.0 adapter of an MIT-licensed method (notice in references/)
metadata:
  hermes:
    tags: [design, design-system, tokens, accessibility, preview]
    category: creative
    requires_toolsets: [file]
---

# Design consultation (adapted from gstack)

Propose a coherent design for one product surface: brief, user journey,
semantic tokens, interaction states, responsive behaviour, accessibility
requirements, asset rights and a working preview. You (the coordinator) write
one analysis JSON; `scripts/capability_workflow.py` validates it against its
contract, the evidence pack and the reused product brief, then renders the
artifacts. The script writes no design content and adds no facts.

Method adapted from gstack @ 622e8b1f76d293b647eca7b33d9dd060ab618b21 (MIT;
notice and file list in `references/gstack-license.md`). Only the method is
used: no gstack preamble, script, design binary, browser, telemetry, update
check or outside voice runs. See `references/method-map.md`.

## When to use

Use when an accepted product brief needs a design system and a preview.

## When not to use it

- No accepted, current lean PRD, opportunity brief, feature brief or design
  brief exists for this company/project: run `pm-lean-prd` (or problem
  definition) first. The validator refuses a consultation that does not reuse
  one by artifact ID and digest.
- Judging an existing UI or a finished preview: use `gstack-design-review`.
- Writing `DESIGN.md` or any file into a repository: that is a code change on
  the existing engineering path, after the package is accepted.

## Procedure

1. **Context.** Cite the accepted brief in `reused_artifacts`. Build the
   evidence pack as in `problem-definition`. Name the surface mode — what the
   visitor's win looks like here: `persuade`, `operate`, `read` or
   `experience`.
2. **Memorable thing.** Ask, as its own question, "What is the one thing
   you want someone to remember after they first see this product?" Record the
   answer with its source, or leave it `pending` with a `memorable-thing`
   question; never answer it yourself.
3. **Use scene.** Who, where, under what light. Light or dark follows the
   scene (`bright` → light, `dim` → dark, or `both`), not the product category.
4. **Proposal.** One direction: aesthetic, decoration, layout, colour approach,
   motion, a rationale ("X because Y") and one to three deliberate risks.
   Coherence notes are advice and never block.
5. **Type.** Display and body roles (label, mono optional). Never a banned
   face; never an overused face as display; an overused face as body only
   DM Sans, Instrument Sans or IBM Plex Sans on an operate/read surface.
   `verified` needs a supplied source tagged `font-license` that names the
   face; otherwise `pending` (the result then needs input). No face or URL is
   invented and no web font is loaded.
6. **Tokens.** Twelve semantic colours per palette (bg, surface, text,
   text-muted, accent, on-accent, border, focus, success, warning, danger,
   info). The validator computes WCAG contrast: text and status colours 4.5:1
   on bg and surface, on-accent 4.5:1, border and focus 3:1. A 4px spacing
   scale, a radius hierarchy (not one large radius everywhere), motion
   durations of 50–700ms with named easings.
7. **Screens and states.** At most six screens, one primary action each.
   Every screen has default, empty, loading and error states; a screen that
   needs permission also has permission-denied. Messages say what happened and
   what to do; no "Oops", no exclamation marks on success, no buzzwords or
   placeholder people. Metrics and claims cite an evidenced statement and use
   only its numbers.
8. **Journey.** Steps that reach every screen; each names a designed failure
   state.
9. **Responsive and accessibility.** Mobile-first breakpoints starting at 0
   with one at or above 1024px. WCAG 2.2 AA requirements for contrast, focus,
   keyboard, target size, reduced motion, landmarks, form errors, text resize
   and status messages; keyboard, target size, text resize and status
   messages cannot be marked statically verified.
10. **Assets.** Every face, image, icon or logo with its rights; `pending`
    rights put the result in needs-input. Other rights cite a supplied source
    tagged `font-license` (faces) or `asset-license` (others) that names the
    asset; the rights are reported as recorded from it, not verified.
11. **Validate and render** into a fresh task output directory:

```bash
python3 $KIT/scripts/capability_workflow.py validate --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS
python3 $KIT/scripts/capability_workflow.py render --analysis $ANALYSIS --evidence $EVIDENCE --artifacts $ARTIFACTS --out $OUT
```

The output holds `design-brief.md`, `design-package.md`, `design-tokens.json`,
`design-preview.html`, the analysis, evidence and input chain, `artifacts.json`
(new artifacts `draft`) and `result.json`. The preview is one HTML file with
inline CSS, a `default-src 'none'` content security policy, no script, no
remote asset and every supplied string escaped; it is audited statically after
rendering and refused if a state, token or breakpoint is missing. Open it
locally to look at it; nothing here claims it was rendered in a browser.

Research claims ("users said", "interviews showed") need a supplied interview
or dataset source. Synthetic fixtures stay labeled synthetic in every output.

## Contract and examples

- Contract: `hermes/capabilities/schemas/workflow-outputs.schema.json#/$defs/design_consultation`.
- Synthetic, coordinator-authored scenarios: `hermes/capabilities/evals/trial-guide-design/`
  (light, verified fonts, completes) and `billing-rota-design/` (dark, four
  breakpoints, fonts and memorable thing pending).

## Limits

No shell, network, browser, image generation, repository write, publication or
install comes with this skill. Acceptance of the package is a separate
reviewer decision; `gstack-design-review` audits it.

## Source root

`$KIT` (and each `scripts/...` path above) is the capability source root: the
reviewed Agent-Ops-Kit checkout, or the activated bundle at
`$HERMES_HOME/agent-ops-kit/capability-root` (docs/capability-activation.md).
The scripts find the registry and schemas relative to themselves and need
`jsonschema` (scripts/requirements-capabilities.txt). If
`$KIT/scripts/capability_workflow.py` is missing, report the capability as not
installed; do not improvise its checks.
