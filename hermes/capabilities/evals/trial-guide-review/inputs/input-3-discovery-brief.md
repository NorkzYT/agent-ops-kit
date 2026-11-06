# Discovery brief (existing mode)

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-trial-discovery` · company `example-a` · project `example-web` · data `synthetic`
- Evidence pack `pack-trial-discovery` sha256 `611ace1b07f67715…` as of 2026-11-04

## Discovery question
Which first-session change would get more new trials to a first project within 7 days?
Branch: existing product (continuous discovery)
Decision this informs: Which onboarding idea earns a lean PRD and which ideas are dropped before any build.

## Desired outcome
7-day trial activation rate: target >= 36 percent within 28 days; baseline `d-activation` (synthetic)

## Problem frame reused
- `t-trial-activation.problem-brief` (problem-brief, sha256 `80a74a81d5930580…`)

## Evidence
- **synthetic** `d-activation`: 248 of 800 September 2026 trials activated within 7 days, 31 percent. [E1, k-act]
- **synthetic** `d-nosample`: Trials created without sample data activated at 23 percent. [E1, k-nosample]
- **synthetic** `d-chats`: 14 of 40 reviewed trial support chats asked how to start a first project, 35 percent. [E2, k-chats]
- **synthetic** `d-guide`: 9 of 40 reviewed trial support chats asked where the welcome guide was. [E2]

## Executed arithmetic
- `k-act`: a / n * 100 = 31 percent (recomputed)
- `k-nosample`: a / n * 100 = 23 percent (recomputed)
- `k-chats`: a / n * 100 = 35 percent (recomputed)

## Opportunities
- `op-start`: "I open an empty workspace and do not know what to build first." [d-nosample, d-chats]
- `op-guide`: "I cannot find the getting-started help when I first log in." [d-guide]

## Ideas
| Idea | Perspective | Opportunity | Status |
|---|---|---|---|
| Starter template picker: Offer three ready-made projects on the empty workspace. | product | op-start | carried |
| In-app first-step guide: A short guide panel opens at first login and walks through a first project. | design | op-guide | carried |
| Activation leaderboard: Show trial teams how they rank against other new teams. | product | op-start | declined |
| Spreadsheet import: Create a first project from an uploaded spreadsheet. | engineering | op-start | proposed |
| Event-triggered welcome email: Send the welcome email from the sign-up event instead of the nightly batch. | engineering | op-guide | proposed |

## Idea decisions
- `i-templates` **carried**: Starter template picker
- `i-guide` **carried**: In-app first-step guide
- `i-leaderboard` **declined**: Activation leaderboard — Requester declined it: it ranks trial users publicly (E3).
- `i-import` **proposed**: Spreadsheet import
- `i-event-email` **proposed**: Event-triggered welcome email

## Checkpoints
- idea-selection: answered (source E3) — Which of these five ideas should be stress-tested?
- assumption-focus: answered (source E3) — Which risky assumptions should be tested first?

## Assumptions, hypotheses and estimates
- **hypothesis** `d-h-templates`: Trial users who start from a template create a first project sooner than users who start empty. [E1, E2]
- **hypothesis** `d-h-guide`: A guide shown at first login does the job the delayed welcome email no longer does. [E1, E2]
- **assumption** `d-a-fixtures`: Existing sample-data fixtures can seed three starter templates.

## Evidence gaps
- **unknown** `d-u-rct`: The separate effect of any onboarding change is unknown. — resolve: Random assignment in the guide A/B test below.
- **unknown** `d-u-templates`: How many trial users would choose a template is unknown: no template feature or usage event exists. — resolve: Log clicks on a fake-door template button before building anything.
- Public research: denied by the evidence pack policy; these gaps stay open until it is approved

## Next action
Add the template-button click event, then start the fake-door test on half of new trials. (test, cheap, owner product manager)
