# Acceptance map: Off-hours billing queue and billing reply macros

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-support-prd` · company `example-b` · project `example-ops` · data `synthetic`
- Evidence pack `pack-support-prd` sha256 `d43fd3d9b878ec6d…` as of 2026-11-04

| Criterion | Scope item | Given / when / then | Verification | Trace |
|---|---|---|---|---|
| `ac-route` | r-route | Given a billing-tagged ticket created on Friday at 16:00, when routing runs, then the ticket lands in the off-hours billing queue within 1 minute | automated-test | evidence: b-friday (synthetic) |
| `ac-daytime` | r-route | Given a billing-tagged ticket created on Tuesday at 10:00, when routing runs, then the ticket stays in the normal billing queue | automated-test | chosen requirement: Daytime billing tickets keep going to the trained agents. |
| `ac-rota` | r-rota | Given the rota for the next 28 days, when a supervisor opens the calendar, then every day has exactly 1 named agent for the off-hours billing queue | manual-check | evidence: b-staff (synthetic) |
| `ac-macros` | r-macros | Given the macro library, when a billing-trained agent reviews it, then each macro has a recorded reviewer and review date | manual-check | chosen requirement: Macros reach customers through agents, so a specialist signs off each one. |
| `ac-target` | r-route | Given the 28-day pilot has ended, when the ticket export is compared, then billing median first response is 12 hours or less and other tickets stay at 12 hours or less | metric-check | evidence: b-gap (synthetic) |
