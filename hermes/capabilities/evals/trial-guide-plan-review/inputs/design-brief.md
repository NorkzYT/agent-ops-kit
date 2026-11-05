# Design brief: First-step guide

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-trial-design` · company `example-a` · project `example-web` · data `synthetic`
- Evidence pack `pack-trial-design` sha256 `7935bcb28c35273a…` as of 2026-11-04

## Product brief reused
- `t-trial-prd.lean-prd` (lean-prd, sha256 `c3a8b264d1eb8a76…`)

## Problem
New trial users wait for a delayed welcome email to find the setup guide, so many never start a first project in the trial.

## Audience
New trial users in their first session, who have not yet created a project. Evidence: `st-scene` (synthetic), `st-guide` (synthetic)

## Use scene
New trial users in their first session; On laptops in office daylight, beside an empty workspace. Lighting bright → theme **light** (light or dark follows the use scene).

## Memorable thing
You can get a first project running in three steps, without waiting for an email. (requester answer, source D2)

## Surface mode
operate

## Goals
- A first project exists by the end of the first session.
- The guide can be left and reopened from Help without losing the current step.

## Constraints
- The welcome email schedule stays unchanged during the test.
- Only the flagged half of new trials sees the guide.

## Non-goals
- Changing the welcome email.
- Any public ranking of trial users.

## Direction
- Aesthetic brutally-minimal; decoration minimal; layout grid-disciplined; colour restrained; motion minimal-functional
- Why: An operate surface beside an empty workspace: type and spacing carry the hierarchy because the guide must not compete with the product, and one teal accent marks the next step because the thing to remember is three steps.
- Deliberate risk: No illustration in the empty workspace. Because An honest empty state with one action keeps attention on step one instead of decoration.
- Deliberate risk: Step numbers set in the mono face. Because The count of steps is the promise, so it is set like data rather than decoration.

## User journey
| Step | Goal | Screen | Action | Success signal | If it fails | Evidence |
|---|---|---|---|---|---|---|
| `j-name` | Create a first project | Setup guide, step 1 | Types a project name and selects Create project. | The project appears in the workspace and the guide moves to step 2. | error state | st-guide |
| `j-skip` | Leave the guide for later | Setup guide, step 1 | Selects Skip the guide. | The guide closes and the empty workspace stays usable. | error state | - |
| `j-reopen` | Pick the guide up again | Help menu | Opens Help and selects Reopen the setup guide. | The guide opens at the step the user left. | empty state | st-h-findable |

## Evidence
- **synthetic** `st-scene`: New trial users start on laptops in office daylight, and the guide sits beside an empty workspace. [D1]
- **synthetic** `st-fonts`: The company design kit licenses Source Sans 3 and JetBrains Mono for product UI. [D1]
- **synthetic** `st-guide`: 9 of 40 reviewed trial support chats asked where the welcome guide was. [D3]
- **synthetic** `st-memorable`: The requester wants people to remember that a first project takes three steps, without waiting for an email. [D2]

## Assumptions and hypotheses
- **hypothesis** `st-h-findable`: A guide that opens at first login and can be reopened from Help removes the question of where the guide is. [D3]
- **assumption** `st-a-laptop`: Most first sessions happen at laptop widths; phone widths are a secondary check.

## Unknowns
- **unknown** `st-u-usability`: Whether trial users can complete the guide without help is unknown. — resolve: Run a moderated usability session on the preview with trial users after the outreach approval.
- Public research: denied by the evidence pack policy; these gaps stay open until it is approved

## Next action
Review the preview with the product trio, then decide whether to request a moderated usability session. (decide, cheap, owner Product designer)
