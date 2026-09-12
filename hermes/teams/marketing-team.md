# Marketing team: research → position → plan → review

Kept from the CrewAI growth crew. The five roles live in
`hermes/profiles/marketing/SOUL.md`; `research` and `strategy` join as inputs
and reviewer.

## Roles (from the original crew)

- **market_researcher** — Product Market Research Lead. Identify the highest-value market segments and acquisition angles for {tool_name} so the business can grow qualified subscribers. Analyses user behaviour, workflow pain points and competitive alternatives to find messaging that resonates.
- **positioning_copywriter** — Conversion Copy and Positioning Specialist. Turn insights into clear positioning and persuasive messaging that communicates concrete outcomes and decision confidence: time saved, better decisions, repeatable profit opportunities.
- **channel_strategist** — Growth Channel Strategist. Select the channels and campaign structure that maximise awareness and qualified subscriber acquisition with constrained resources; balances owned, earned and paid for B2B and SMB SaaS.
- **funnel_analyst** — Funnel and Metrics Analyst. Design a measurement-ready funnel with practical KPIs and decision thresholds for weekly iteration.
- **weekly_operator** — Weekly Growth Operations Manager. Produce an actionable weekly plan the founder can execute quickly: explicit priorities and stop/continue rules.

## Wiring

```bash
make hermes-profile NAME=research
make hermes-profile NAME=marketing
make hermes-profile NAME=strategy

hermes kanban create "ICP + segments for <product>"        --assignee research
hermes kanban create "Positioning + messaging matrix"      --assignee marketing --parent t_icp
hermes kanban create "4-week channel plan + funnel KPIs"   --assignee marketing --parent t_pos
hermes kanban create "Review pricing/positioning decisions" --assignee strategy  --parent t_plan
hermes kanban watch
```

Task prompts are in `marketing-team-tasks.md`.
