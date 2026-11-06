# Spec: Route off-hours billing tickets to a rota queue

- Status: **draft** (acceptance is a separate reviewer decision)
- Provenance: Coordinator-authored analysis on SYNTHETIC fixture data (not real research); validated and rendered by scripts/capability_workflow.py, which authors no content
- Task `t-support-spec` · company `example-b` · project `example-ops` · data `synthetic`
- Evidence pack `pack-support-spec` sha256 `14f6b9e88ff85c71…` as of 2026-11-04

## Framing
feature · mode draft
Built on (accepted, re-read by digest): `t-support-prd.lean-prd` (lean-prd)

## Why this matters
- **assumption** `s-why`: Off-hours billing tickets wait for the next weekday shift because only the Billing queue receives them.
- **synthetic** `s-rota`: The off-hours rota starts with 2 agents; billing macros follow in a later spec. [S2]

## Verified current state
- **synthetic** `s-all-hours`: Billing-tagged tickets are assigned to the queue Billing at all hours; no routing rule depends on time of day. [S1]
- **synthetic** `s-hours`: Business hours are Monday to Friday 08:00-17:00 in the Europe/Berlin calendar. [S1]
- **synthetic** `s-condition`: Routing rules can use a business-hours condition. [S1]

## Scope
In scope:
- A business-hours routing rule for billing-tagged tickets
- An off-hours billing queue
- A billing rota agent group
Out of scope (do not touch):
- Billing reply macros (r-macros): They follow in a later spec after question tagging.
- Rota calendar (r-rota): The support lead keeps it in the existing scheduling tool.

## Proposed change
Add an Off-hours billing queue and a billing rota group, then a routing rule that assigns billing-tagged tickets created outside the Europe/Berlin business hours to that queue. Daytime billing assignment stays as it is.

## Acceptance criteria
- [ ] `ac-offhours` Given a ticket tagged billing is created on Friday at 18:30 Europe/Berlin, when the routing rules run, then the ticket is assigned to the Off-hours billing queue (integration)
- [ ] `ac-daytime` Given a ticket tagged billing is created on Tuesday at 10:00 Europe/Berlin, when the routing rules run, then the ticket is assigned to the queue Billing, as today (integration, regression)
- [ ] `ac-boundary` Given a ticket tagged billing is created on a weekday at exactly 17:00 Europe/Berlin, when the routing rules run, then the ticket is assigned to the Off-hours billing queue (unit)
- [ ] `ac-untagged` Given a ticket without the billing tag is created on Friday at 18:30, when the routing rules run, then its queue assignment is the same as before the change (integration, regression)

## Traceability
- `ac-offhours` → `r-route`, `f-route` (t-support-prd.lean-prd)
- `ac-daytime` → `r-route` (t-support-prd.lean-prd)
- `ac-boundary` → `f-route` (t-support-prd.lean-prd)
- `ac-untagged` → `f-route` (t-support-prd.lean-prd)

## Testing plan
- integration: Replay four fixture tickets through the helpdesk rule sandbox and compare assigned queues. — covers `ac-offhours`, `ac-daytime`, `ac-untagged`
- unit: Evaluate the business-hours condition at 16:59, 17:00 and 08:00. — covers `ac-boundary`

## Files
| Path | Change | Why |
|---|---|---|
| `helpdesk/queues/offhours-billing.json` | add | New queue. |
| `helpdesk/groups/billing-rota.json` | add | Rota agents who work the queue. |
| `helpdesk/routing/billing-offhours.json` | add | Business-hours routing rule. |

## Dependencies
- dep-queue (independent): Create the off-hours queue and rota group
- dep-rule ← dep-queue: Add the routing rule

## Rules for the implementing agent
- Do not change daytime billing assignment.
- Use the existing Europe/Berlin business-hours calendar; do not add a second calendar.
- Do not send automatic replies to customers.

## Rollback
Disable the off-hours routing rule; billing tickets fall back to the queue Billing. The queue and group can stay.

## Effort
S: Three configuration objects in the existing helpdesk; no code.

## Open questions
- (public-holidays) Should public holidays in the Europe/Berlin calendar count as off-hours for billing routing?

## Unknowns
- none recorded
- Public research: denied by the evidence pack policy; these gaps stay open until it is approved

## Handoff (draft, carries no approval)
Targets: gstack.plan-eng-review, existing.engineering-path. Plan review first; the helpdesk change is a configuration card on the existing engineering path.
Nothing here approves implementation, issue filing, publication, merge or release; each needs its own approval on the receiving path.
