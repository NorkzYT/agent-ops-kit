# Implementation handoff (draft): In-app first-step guide for new trials

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-trial-prd` · company `example-a` · project `example-web` · data `synthetic`
- Evidence pack `pack-trial-prd` sha256 `52da8867b3966e0a…` as of 2026-11-04

This handoff carries no approval: implementation, issue publication, merge and release each need their own approval on the receiving path.

## Proposed next reviewers
- gstack.plan-design-review
- existing.engineering-path
Design review of the panel first; engineering only after the product manager accepts this draft.

## First-version scope and its criteria
- `r-guide` Guide panel with three steps at first login. — criteria: ac-steps, ac-activation
- `r-flag` Random half assignment with the arm recorded. — criteria: ac-split
- `r-reopen` Dismiss and reopen from the help menu. — criteria: ac-reopen

## Open before building
- **hypothesis** `st-h-guide`: A guide at first login does the job the delayed welcome email no longer does. [E1, E2]
- **assumption** `st-a-capacity`: One designer and one engineer can build the first version in about two sprints.
- **unknown** `st-u-effect`: The guide's effect on activation is unknown until the A/B test runs. — resolve: The flagged rollout in this PRD's first version is the A/B test from the discovery plan.

Left out on purpose: Contacts (not known); Detailed UX flows; Rollout beyond the test
