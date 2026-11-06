# Product requirements: Off-hours billing queue and billing reply macros (full mode)

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-support-prd` · company `example-b` · project `example-ops` · data `synthetic`
- Evidence pack `pack-support-prd` sha256 `d43fd3d9b878ec6d…` as of 2026-11-04

## 1. Summary
Billing questions wait far longer for a first reply than other tickets, mostly outside the two billing agents' shifts. This routes off-hours billing tickets to a rota queue and gives rota agents reviewed reply macros, without new headcount.

## 3. Background
- **synthetic** `b-gap`: Billing tickets had a median first response of 31 hours versus 9 hours for other tickets in October 2026. [E1]
- **synthetic** `b-staff`: Only 2 agents are trained on billing and both work weekday day shifts. [E2]
- **synthetic** `b-friday`: Billing questions arriving on Friday after 15:00 wait until Monday. [E2]
- **synthetic** `b-constraint`: No new support headcount before 2027-01-01; the Q4 2026 tooling budget is 3000 USD. [E3]
Built on: `t-support-billing.problem-brief` (problem-brief)

## 4. Objective
Bring billing first response close to other tickets this quarter without hiring.
- `kr-billing` Median first response for billing tickets: target <= 12 hours within 28 days; baseline `b-gap` (synthetic)
- `kr-other` Median first response for other tickets: target <= 12 hours within 28 days; baseline `b-gap` (synthetic)

## 5. Market segments
- `seg-customer` Customers with a billing question: Get a billing question answered before it blocks a payment or renewal.
- `seg-agent` Support agents without billing training: Answer a billing ticket correctly without waiting for a billing specialist. Constraints: Rota shifts must fit current contracts.

## 6. Value propositions
### Customers with a billing question
- Why: A billing question often blocks paying or renewing.
- What before: Questions sent on Friday afternoon wait until Monday for one of 2 specialists.
- How: Off-hours billing tickets go to a rota queue that is staffed every day.
- What after: They get a first reply within the same half day.
- Alternatives: Waiting, emailing again, or contacting sales.
**For customers with a billing question, the rota queue answers off-hours tickets the same day instead of after the weekend.**

### Support agents without billing training
- Why: They see billing tickets they cannot answer and leave them in the queue.
- What before: They pass every billing ticket to the 2 trained agents.
- How: Reviewed reply macros cover common billing questions.
- What after: They answer common billing questions themselves and escalate only the rest.
- Alternatives: Asking a specialist in chat or leaving the ticket.
**For agents without billing training, reviewed macros turn common billing tickets into ones they can close.**

## 7. Solution
7.2 Key features:
- `f-route` Route billing-tagged tickets created outside weekday day shifts to an off-hours billing queue.
- `f-rota` Rota calendar that names the agent covering the off-hours billing queue each day.
- `f-macros` Billing reply macros written and reviewed by the 2 billing-trained agents.

7.4 Assumptions and open questions:
- **hypothesis** `b-h-coverage`: Thin billing coverage outside weekday day shifts drives much of the billing delay. [E1, E2]
- **assumption** `b-a-rota`: Two non-billing agents can join an off-hours rota after a short training.
- **unknown** `b-u-csat`: Whether slow billing replies lower customer satisfaction is unknown. — resolve: Add a one-question satisfaction prompt on closed billing tickets during the pilot.
- **unknown** `b-u-macros`: How many billing questions a reply macro could answer is unknown. — resolve: Tag a two-week sample of billing tickets by question type before writing macros.
- question: Do off-hours rota shifts need extra pay or time off in lieu?

## 8. Release
First version:
- `r-route` Off-hours routing rule and queue.
- `r-rota` Rota calendar with daily coverage.
- `r-macros` First set of reviewed billing macros.
Later:
- `r-crosstrain` Cross-training more agents, if the pilot misses its target.
Not doing:
- `ng-hire` Hiring.
- `ng-policy` Changing billing policy or refunds.
- `ng-auto` Automatic replies sent to customers without an agent.
Timeframe: One sprint to configure, then a 28-day pilot.

## Constraints
- No new headcount before the date in b-constraint.
- Tooling spend stays within the Q4 budget in b-constraint.

## Risks
- `rk-fatigue` Rota agents tire of off-hours shifts. Mitigation: Rotate daily and cap each agent at two off-hours days a week.
- `rk-season` Seasonal billing volume changes hide the effect. Mitigation: Compare against the same weeks of October and report volume next to the median.
- `rk-wrong-answer` A macro gives a wrong billing answer. Mitigation: Only reviewed macros are available; unclear cases are escalated.
