# Acceptance map: In-app first-step guide for new trials

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-trial-prd` · company `example-a` · project `example-web` · data `synthetic`
- Evidence pack `pack-trial-prd` sha256 `52da8867b3966e0a…` as of 2026-11-04

| Criterion | Scope item | Given / when / then | Verification | Trace |
|---|---|---|---|---|
| `ac-steps` | r-guide | Given a new trial user in the guide arm at first login, when the workspace opens, then the guide panel shows exactly 3 steps | automated-test | chosen requirement: Three steps match the shortest path to a first project. |
| `ac-split` | r-flag | Given 1000 test sign-ups, when assignment runs, then between 45 and 55 percent are in the guide arm and every trial has an arm recorded | automated-test | chosen requirement: Random assignment is the only way to learn the guide's effect (st-u-effect). |
| `ac-reopen` | r-reopen | Given a user who dismissed the guide, when they open the help menu, then the guide reopens at the step they left | manual-check | chosen requirement: Experienced users must be able to leave the guide without losing it. |
| `ac-activation` | r-guide | Given the 28-day test window has ended, when activation is compared by arm, then 7-day activation in the guide arm is at least 36 percent | metric-check | evidence: st-activation (synthetic) |
