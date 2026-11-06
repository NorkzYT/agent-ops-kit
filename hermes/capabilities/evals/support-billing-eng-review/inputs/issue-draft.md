# Route off-hours billing tickets to a rota queue

Local draft: nothing was filed. No publication requested. Redaction gate passed (no secret-shaped strings).

## Context
- **assumption** `s-why`: Off-hours billing tickets wait for the next weekday shift because only the Billing queue receives them.
- **synthetic** `s-rota`: The off-hours rota starts with 2 agents; billing macros follow in a later spec. [S2]

## Current State
- **synthetic** `s-all-hours`: Billing-tagged tickets are assigned to the queue Billing at all hours; no routing rule depends on time of day. [S1]
- **synthetic** `s-hours`: Business hours are Monday to Friday 08:00-17:00 in the Europe/Berlin calendar. [S1]
- **synthetic** `s-condition`: Routing rules can use a business-hours condition. [S1]

## Proposed Change
Add an Off-hours billing queue and a billing rota group, then a routing rule that assigns billing-tagged tickets created outside the Europe/Berlin business hours to that queue. Daytime billing assignment stays as it is.

## Acceptance Criteria
- [ ] `ac-offhours` Given a ticket tagged billing is created on Friday at 18:30 Europe/Berlin, when the routing rules run, then the ticket is assigned to the Off-hours billing queue (integration)
- [ ] `ac-daytime` Given a ticket tagged billing is created on Tuesday at 10:00 Europe/Berlin, when the routing rules run, then the ticket is assigned to the queue Billing, as today (integration, regression)
- [ ] `ac-boundary` Given a ticket tagged billing is created on a weekday at exactly 17:00 Europe/Berlin, when the routing rules run, then the ticket is assigned to the Off-hours billing queue (unit)
- [ ] `ac-untagged` Given a ticket without the billing tag is created on Friday at 18:30, when the routing rules run, then its queue assignment is the same as before the change (integration, regression)

## Testing Plan
- integration: Replay four fixture tickets through the helpdesk rule sandbox and compare assigned queues. — covers `ac-offhours`, `ac-daytime`, `ac-untagged`
- unit: Evaluate the business-hours condition at 16:59, 17:00 and 08:00. — covers `ac-boundary`

## Rules for the implementing agent
- Do not change daytime billing assignment.
- Use the existing Europe/Berlin business-hours calendar; do not add a second calendar.
- Do not send automatic replies to customers.

## Rollback Plan
Disable the off-hours routing rule; billing tickets fall back to the queue Billing. The queue and group can stay.

## Effort Estimate
S: Three configuration objects in the existing helpdesk; no code.
