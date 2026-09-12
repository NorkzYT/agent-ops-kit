# Team recipes

A team is a set of profiles plus a Kanban pipeline. Each file here holds the
role prompts (kept from the earlier CrewAI crews) and the commands that wire
them together. Prerequisites: `hermes kanban init` once, the profiles created
with `make hermes-profile NAME=...`, and the gateway running.

| Recipe | Profiles | Shape |
|--------|----------|-------|
| `coding-team.md` | planner (orchestrator) → coder → reviewer | plan, implement, review, merge |
| `marketing-team.md` | research → marketing → strategy | research, position, plan, review |

Pattern for any new team: research tasks in parallel, one synthesis task that
depends on them (`--parent`), one review round, then the orchestrator writes
the final answer. That is the "consensus" step: a pipeline with an explicit
critique, not an open-ended debate.
