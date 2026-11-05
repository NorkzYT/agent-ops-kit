# Experiment plan (existing product)

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-trial-discovery` · company `example-a` · project `example-web` · data `synthetic`
- Evidence pack `pack-trial-discovery` sha256 `611ace1b07f67715…` as of 2026-11-04

## `ex-fake-door` — fake-door (effort low)
Tests: as-uptake
Setup: Show a 'Start from a template' button on the empty workspace to half of new trials; a click explains templates are coming and is logged.
Hypothesis: New trial accounts that land on an empty workspace — Click 'Start from a template' in their first session, >= 25 percent of exposed trials within 14 days after Fake-door template button
Measured by: Template-button click events divided by exposed trials; counter-metric: First-project creation rate among exposed trials
Failure rule: Drop the template idea if fewer than 10 percent click within 14 days.
Risk mitigation: The button says the feature is not built yet; exposure is half of new trials behind a flag that can be switched off.
If it passes: Write a lean PRD for the template picker.
If it fails: Drop the template idea and keep the guide branch.
Result: not run (unknown until measured; never simulated)

## `ex-guide-ab` — ab-test (effort medium)
Tests: as-guide
Setup: Show the guide at first login to a random half of new trials; the welcome email stays unchanged in both arms.
Hypothesis: New trial accounts — Create a first project within 7 days, >= 36 percent in the guide arm within 28 days after In-app first-step guide at first login
Measured by: First-project events by assignment arm; counter-metric: Support chats per trial
Failure rule: Stop if the guide arm is not at least 3 points above control after 28 days.
Risk mitigation: Control keeps today's experience; the guide can be dismissed and the flag rolled back.
If it passes: Keep the guide for all new trials.
If it fails: Remove the guide and revisit the event-triggered email idea.
Result: not run (unknown until measured; never simulated)
