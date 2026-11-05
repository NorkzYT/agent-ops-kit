# Product requirements: In-app first-step guide for new trials (lean mode)

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-trial-prd` · company `example-a` · project `example-web` · data `synthetic`
- Evidence pack `pack-trial-prd` sha256 `52da8867b3966e0a…` as of 2026-11-04

## 1. Summary
A short guide opens at first login and walks new trial users through a first project. It ships behind a flag to a random half of new trials so its effect on 7-day activation can be measured, because the welcome email no longer arrives in time.

## 3. Background
- **synthetic** `st-activation`: 248 of 800 September 2026 trials activated within 7 days, 31 percent. [E1, k-act]
- **synthetic** `st-delay`: Trials whose welcome email was delayed more than 24 hours activated at 20 percent. [E1, k-delay]
- **synthetic** `st-guide`: 9 of 40 reviewed trial support chats asked where the welcome guide was. [E2]
Built on: `t-trial-discovery.discovery-brief` (discovery-brief)

## 4. Objective
Get more new trial users to a working first project in their first week, without waiting for email.
- `kr-activation` 7-day activation rate in the guide arm: target >= 36 percent within 28 days; baseline `st-activation` (synthetic)

## 5. Market segments
- `seg-new-trial` New trial users in their first week: Get a first project running without outside help, to judge the product inside the trial. Constraints: The welcome email stays unchanged during the test.

## 6. Value propositions
### New trial users in their first week
- Why: They need a working first project to decide whether to buy before the trial ends.
- What before: They wait for a welcome email that a nightly batch can delay by more than a day, then hunt for the guide.
- How: A three-step guide opens at first login and walks them through a first project.
- What after: They create a first project in the first session.
- Alternatives: The delayed welcome email, searching the help centre, or a support chat.
**For new trial users who want a first project fast, the in-app guide replaces waiting for email with three steps at first login.**

## 7. Solution
7.2 Key features:
- `f-guide` Three-step guide panel at first login: name the project, add a first task, invite a teammate or skip.
- `f-flag` Assignment flag that shows the guide to a random half of new trials and records the arm.
- `f-reopen` Dismiss the guide, and reopen it from the help menu.

7.4 Assumptions and open questions:
- **hypothesis** `st-h-guide`: A guide at first login does the job the delayed welcome email no longer does. [E1, E2]
- **assumption** `st-a-capacity`: One designer and one engineer can build the first version in about two sprints.
- **unknown** `st-u-effect`: The guide's effect on activation is unknown until the A/B test runs. — resolve: The flagged rollout in this PRD's first version is the A/B test from the discovery plan.

## 8. Release
First version:
- `r-guide` Guide panel with three steps at first login.
- `r-flag` Random half assignment with the arm recorded.
- `r-reopen` Dismiss and reopen from the help menu.
Later:
- `r-templates` Starter template picker, only if its fake-door test passes.
Not doing:
- `ng-email` Changing the welcome email schedule.
- `ng-ranking` Any public ranking of trial users.
Timeframe: About two sprints for the first version, then a 28-day test window.

## Constraints
- The welcome email schedule stays as it is until the test ends.
