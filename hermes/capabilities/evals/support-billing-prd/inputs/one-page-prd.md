# One-page PRD (draft, never approved implementation)

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-support-billing` · company `example-b` · project `example-ops` · data `synthetic`
- Evidence pack `pack-support-billing` sha256 `d42720d8320764f4…` as of 2026-11-02

**Problem:** Billing questions wait far longer for a first reply than other tickets.
**Users:** Customers with billing questions; support agents
**Objective:** Bring billing first response close to other tickets without new headcount.

## Scope
- Off-hours billing rota pilot for 28 days
- Arrival-time split of current tickets

## Non-goals
- Hiring
- Changing billing policy or refunds
- Automated replies to customers

## Constraints
- No new headcount before 2027-01-01
- Q4 tooling budget 3000 USD

## Acceptance tests
- Billing median first response &lt;= 12 hours over 28 days in the ticket export
- Non-billing median first response stays &lt;= 12 hours

## Risks
- Rota fatigue among agents
- Seasonal billing volume changes confound the comparison

## Unknowns
- `s-u-csat` Whether slow billing replies lower customer satisfaction is unknown.
- `s-u-churn` Whether billing delays contribute to cancellations is unknown.

Owner: support lead. Next decision: After 28 days: keep the rota, switch to cross-training, or combine.
