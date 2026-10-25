# Marketing team: task prompts

Kept from the CrewAI growth crew. Fill the placeholders, paste into the Kanban
task description.

## define_icp (assignee: research)
```
Define the ideal customer profile and priority market segments for:
- Tool: {tool_name}
- Tagline: {tool_tagline}
- Target persona: {target_persona}
- Offer: {primary_offer}
Use practical segmentation criteria that can guide channel and offer decisions.
Expected output: a concise ICP with 2-3 high-priority segments, main pain
points and buying triggers.
```

## craft_positioning (assignee: marketing, parent: define_icp)
```
Create positioning and core messaging for the prioritised segments. Emphasise
differentiation around faster execution and better decision making.
Expected output: messaging matrix with value proposition, proof points,
objection handling and CTA guidance for the primary conversion path.
```

## plan_channels (assignee: marketing, parent: craft_positioning)
```
Build a 4-week channel plan to grow qualified awareness and subscription
intent. Include channel rationale, content themes, publishing cadence and
success criteria.
Expected output: ranked channel strategy and 4-week content/campaign outline.
```

## design_funnel (assignee: marketing, parent: plan_channels)
```
Design a measurement framework for awareness -> activation -> paid conversion.
Include KPI definitions and decision thresholds for experiment keep/kill
choices.
Expected output: funnel map with KPI definitions, target ranges and an
experiment scoring model.
```

## weekly_execution_plan (assignee: marketing, parent: design_funnel)
```
Convert all previous outputs into a weekly operations plan optimised for a solo
founder. Provide concrete priorities, dependencies and iteration cadence.
Expected output: weekly action plan with top 3 experiments, required inputs and
a reporting checklist.
```

## review (assignee: strategy, parent: weekly_execution_plan)
```
Review the plan for pricing, positioning and resource realism. Return specific
change requests or approve with LGTM.
```
