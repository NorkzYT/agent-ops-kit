# Implementation handoff (draft): Off-hours billing queue and billing reply macros

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-support-prd` · company `example-b` · project `example-ops` · data `synthetic`
- Evidence pack `pack-support-prd` sha256 `d43fd3d9b878ec6d…` as of 2026-11-04

This handoff carries no approval: implementation, issue publication, merge and release each need their own approval on the receiving path.

## Proposed next reviewers
- existing.engineering-path
Helpdesk configuration only; the support lead accepts this draft before any change is made.

## First-version scope and its criteria
- `r-route` Off-hours routing rule and queue. — criteria: ac-route, ac-daytime, ac-target
- `r-rota` Rota calendar with daily coverage. — criteria: ac-rota
- `r-macros` First set of reviewed billing macros. — criteria: ac-macros

## Open before building
- **hypothesis** `b-h-coverage`: Thin billing coverage outside weekday day shifts drives much of the billing delay. [E1, E2]
- **assumption** `b-a-rota`: Two non-billing agents can join an off-hours rota after a short training.
- **unknown** `b-u-csat`: Whether slow billing replies lower customer satisfaction is unknown. — resolve: Add a one-question satisfaction prompt on closed billing tickets during the pilot.
- **unknown** `b-u-macros`: How many billing questions a reply macro could answer is unknown. — resolve: Tag a two-week sample of billing tickets by question type before writing macros.
- question: Do off-hours rota shifts need extra pay or time off in lieu?

Left out on purpose: Contacts (not known); Satisfaction survey design
