# Problem brief (causal mode)

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-trial-activation` · company `example-a` · project `example-web` · data `synthetic`
- Evidence pack `pack-trial-activation` sha256 `23fb5d0389b18c3d…` as of 2026-10-30

## Decision
Which onboarding change should be reverted or tested first to recover trial activation? (owner: product manager)

## Problem
Trial activation fell after two onboarding changes shipped days apart; the export shows both conditions associated with lower activation but cannot separate their effects.
Affected: New trial accounts in their first 7 days; process: Onboarding: welcome email and first workspace
Symptom: `t-symptom` 31 percent of September 2026 trials activated within 7 days.

## Evidence
- **synthetic** `t-email`: Trials with a welcome email delayed more than 24 hours activated at 20 percent versus 37.6 percent for the rest. [D1, k-delayed, k-not-delayed]
- **synthetic** `t-sample`: Trials created without sample data activated at 23 percent versus 39 percent with sample data. [D1, k-nosample, k-sample]
- **synthetic** `t-both`: Trials with both conditions activated at 15 percent. [D1, k-both]
- **synthetic** `t-release`: The email batch change (2026-08-28) and opt-in sample data (2026-09-01) shipped four days apart. [D2]

## Executed arithmetic
- `k-overall`: a / n * 100 = 31 percent (recomputed)
- `k-delayed`: a / n * 100 = 20 percent (recomputed)
- `k-not-delayed`: (a - d) / (n - m) * 100 = 37.6 percent (recomputed)
- `k-nosample`: a / n * 100 = 23 percent (recomputed)
- `k-sample`: (a - d) / (n - m) * 100 = 39 percent (recomputed)
- `k-both`: a / n * 100 = 15 percent (recomputed)

## Assumptions, hypotheses and estimates
- **hypothesis** `t-h-email`: The nightly email batch delays the first-step guide and lowers activation. [D1, D2]
- **hypothesis** `t-h-sample`: An empty workspace without sample data stalls first project creation and lowers activation. [D1, D2]
- **assumption** `t-a-mix`: Trial sign-up mix did not change between August and September.

## Unknowns
- **unknown** `t-u-rct`: The separate causal effect of each change is unknown. — resolve: Run a two-by-two staggered rollback (immediate email, default sample data) with random assignment for 14 days.

## Why tree (branches allowed; no fixed depth)
- `w-email` [observed-association] Trials with a welcome email delayed more than 24 hours activated at 20 percent versus 37.6 percent for the rest.
  - `w-email-why` [causal-hypothesis] The nightly email batch delays the first-step guide and lowers activation. — test: Randomize immediate versus batched welcome email; if activation does not differ, reject.
- `w-sample` [observed-association] Trials created without sample data activated at 23 percent versus 39 percent with sample data.
  - `w-sample-why` [causal-hypothesis] An empty workspace without sample data stalls first project creation and lowers activation. — test: Randomize default versus opt-in sample data; if first-project creation does not differ, reject.
- `w-separate` [unknown-link] The separate causal effect of each change is unknown.
Stop: Each branch reaches a testable hypothesis; the export is observational, so further whys would be speculation.

## Partition
- Criterion: Onboarding step where the trial stalls: communication (welcome email) versus first-use content (sample data).
- Overlaps: Trials with both conditions belong to both branches.
- Omitted: Sign-up mix changes and seasonality (recorded as an assumption).
- Interaction: Both-condition trials activated at 15 percent, below either condition alone, so the effects may compound; because the changes shipped four days apart, only random assignment can estimate each effect and their interaction.

## Alternatives
- `x-email` (option): Revert the welcome email to immediate send first. — Cheap to revert; if sample data is the main driver, activation barely moves.
- `x-sample` (option): Restore default sample data first. — Addresses the larger observed gap; touches first-run experience design.
- `x-factorial` (option): Randomized two-by-two rollback of both changes for 14 days. — Separates the effects and their interaction; needs enough trials per cell and a short delay.

## Success measure
7-day trial activation rate: target >= 36 percent within 28 days; baseline `t-symptom` (synthetic)

## Next action
Design the randomized two-by-two rollback, with per-cell sample size computed from the 800-trial September volume, before reverting either change. (test, moderate, owner product manager)
