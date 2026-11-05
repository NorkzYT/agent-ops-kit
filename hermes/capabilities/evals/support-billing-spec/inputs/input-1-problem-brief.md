# Problem brief (full mode)

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-support-billing` · company `example-b` · project `example-ops` · data `synthetic`
- Evidence pack `pack-support-billing` sha256 `d42720d8320764f4…` as of 2026-11-02

## Decision
Should the support lead change billing ticket coverage this quarter, and which change should be piloted first? (owner: support lead)

## Problem
Billing tickets wait more than three times longer for a first reply than other tickets, while headcount cannot grow before 2027.
Affected: Customers with billing questions and the two billing-trained agents; process: Support ticket triage and first response
Symptom: `s-symptom` Billing tickets had a median first response of 31 hours versus 9 hours for other tickets in October 2026.

## Evidence
- **synthetic** `s-share`: Billing was 38 percent of the 1240 October tickets. [E1, c-share]
- **synthetic** `s-reopen`: 23.8 percent of billing tickets were reopened. [E1, c-reopen]
- **synthetic** `s-ratio`: Billing first response is 3.4 times slower than other tickets. [E1, c-ratio]
- **synthetic** `s-staff`: Only 2 agents are trained on billing and both work weekday day shifts. [E2]
- **synthetic** `s-friday`: Billing questions arriving on Friday after 15:00 wait until Monday. [E2]
- **synthetic** `s-constraint`: No new support headcount before 2027-01-01; the Q4 2026 tooling budget is 3000 USD. [E3]

## Executed arithmetic
- `c-share`: billing / total * 100 = 38 percent (recomputed)
- `c-reopen`: reopened / billing * 100 = 23.8 percent (recomputed)
- `c-ratio`: billing_h / other_h = 3.4 ratio (recomputed)

## Assumptions, hypotheses and estimates
- **assumption** `s-a-macros`: Most billing questions are repetitive enough for reply macros.
- **hypothesis** `s-h-coverage`: Thin billing coverage outside weekday day shifts drives much of the billing delay. [E2]
- **hypothesis** `s-h-skill`: Billing tickets queue behind the two trained agents because other agents cannot answer them. [E2]

## Unknowns
- **unknown** `s-u-csat`: Whether slow billing replies lower customer satisfaction is unknown. — resolve: Add a one-question satisfaction prompt to closed billing tickets for four weeks.
- **unknown** `s-u-churn`: Whether billing delays contribute to cancellations is unknown. — resolve: Ask finance for cancellation reasons tagged billing over the last two quarters.

## Why tree (branches allowed; no fixed depth)
- `n-coverage` [causal-hypothesis] Thin billing coverage outside weekday day shifts drives much of the billing delay. — test: Compare median first response for billing tickets that arrive Monday-Thursday daytime with those arriving Friday after 15:00; if the gap is small, reject.
  - `n-friday` [observed-association] Billing questions arriving on Friday after 15:00 wait until Monday.
- `n-skill` [causal-hypothesis] Billing tickets queue behind the two trained agents because other agents cannot answer them. — test: Measure how often non-billing agents pick up and then reassign billing tickets; if reassignment is rare, reject.
  - `n-staff` [observed-association] Only 2 agents are trained on billing and both work weekday day shifts.
Stop: Both branches reach a testable cause; deeper whys (why only two agents were trained) are not needed for the next decision.

## Partition
- Criterion: Where the delay arises: when tickets arrive (time coverage) versus who can answer them (skill coverage).
- Overlaps: A Friday-evening ticket is affected by both: no trained agent works then.
- Omitted: Tool latency and ticket tagging errors are not covered by the supplied evidence.
- Interaction: The branches interact: adding trained agents only helps off-hours tickets if they also cover those hours, so the arrival-time split decides which lever to pilot first.

## SCQA
- Situation: Billing was 38 percent of the 1240 October tickets.; No new support headcount before 2027-01-01; the Q4 2026 tooling budget is 3000 USD.
- Complication: Billing tickets had a median first response of 31 hours versus 9 hours for other tickets in October 2026.; 23.8 percent of billing tickets were reopened.
- Question: Which coverage change can bring billing first response near other tickets without new headcount?
- Candidate answer (not a conclusion): Pilot the lever the arrival-time split favors; candidate only until measured.

## Issue map (criterion: Source of the waiting time)
| Priority | Branch | Why-tree nodes | Actionable |
|---|---|---|---|
| 1 | Time coverage gap | n-coverage, n-friday | yes |
| 2 | Skill coverage gap | n-skill, n-staff | yes |
Overlaps: Off-hours tickets sit in both branches.
Omitted: Tooling and tagging quality.

## Customer/process causal map
Outcome: Billing median first response — Median hours from ticket creation to first agent reply, billing-tagged tickets.; baseline `s-symptom`; window 28 days
- leading `lp-offhours`: Billing tickets arrive when no trained agent works
- leading `lp-skill`: Only two agents can answer billing
- opportunity `op-rota`: Off-hours billing rota (addresses lp-offhours)
- opportunity `op-train`: Cross-training with macros (addresses lp-skill)
- edge lp-offhours → outcome [causal-hypothesis] falsify: Arrival-time split shows no material wait difference.
- edge lp-skill → outcome [causal-hypothesis] falsify: Reassignment of billing tickets by other agents is rare.

## Measurable hypothesis
- population: Billing-tagged support tickets
- behavior_or_outcome: Median first response
- threshold: <= 12 hours
- window_days: 28
- intervention: Friday-afternoon and evening billing rota from existing agents
- rationale: Off-hours arrivals wait until Monday when no trained agent works.
- measurement: Ticket export median first response, billing tag, before and after the pilot
- counter_metric: Median first response for non-billing tickets must not rise above 12 hours
- failure_rule: If billing median stays above 18 hours after 28 days, stop the rota and pilot cross-training.

## Alternatives
- `alt-crosstrain` (option): Cross-train two more agents on billing with a reply macro library. — Uses training time now; reduces dependence on two people; macro quality is an assumption.
- `alt-rota` (option): Add a Friday-afternoon and evening billing rota from existing agents. — Targets the arrival-time hypothesis directly; shifts existing workload and needs agent agreement.
- `alt-nothing` (do-nothing): Keep current coverage and only start measuring satisfaction. — No cost now; the delay continues while the satisfaction unknown is resolved.

## Success measure
Median first response for billing tickets: target <= 12 hours within 28 days; baseline `s-symptom` (synthetic)

## Questions for the requester (material, not retrievable)
- [agent-availability] Which agents could join a Friday-afternoon rota without overtime?

## Next action
Split the October billing tickets by arrival time before choosing a pilot: if Friday-after-15:00 arrivals do not wait materially longer, prefer cross-training over a rota. (measure, cheap, owner support lead)

## Stage log
- scqa: done
- why-tree: done
- issue-map: done
- causal-map: done
- hypothesis: done
- one-page-prd: done
