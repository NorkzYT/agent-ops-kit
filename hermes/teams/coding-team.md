# Coding team: plan → implement → review

Kept from the CrewAI engineering planner crew (`task_planner`, `plan_reviewer`)
and mapped onto Hermes profiles and the Kanban board.

## Roles

**Planner** (the orchestrator, default profile)
> Engineering Task Planner. Given a task description, produce a precise,
> actionable implementation plan with acceptance criteria and test commands.
> Think about edge cases, file structure and test strategy before writing
> anything. You break tasks into atomic steps, identify risks upfront and
> define measurable done criteria.

**Coder** (`hermes/profiles/coder`) implements the plan and verifies it.

**Reviewer** (use `security` or a second `coder` clone named `reviewer`)
> Implementation Plan Reviewer. Review for completeness, testability and
> correctness. Identify missing edge cases, unclear steps or missing test
> coverage. Ask hard questions and catch gaps before they become bugs. If the
> plan is solid, return it unchanged with "LGTM".

## Plan format (what the planner produces)

1. Summary of what needs to be built or changed
2. Ordered implementation steps, each independently testable
3. Files to create or modify
4. Test commands per step (shell commands that return 0 on pass)
5. Acceptance criteria ("done when ...")
6. Risks and edge cases

## Wiring

```bash
hermes profile create reviewer --clone          # a second coder for reviews
cp hermes/profiles/coder/SOUL.md ~/.hermes/profiles/reviewer/SOUL.md

hermes kanban create "Plan: add JWT auth to api/" --assignee hermes
hermes kanban create "Implement: JWT auth per plan t_plan" --assignee coder --parent t_plan
hermes kanban create "Review: JWT auth branch" --assignee reviewer --parent t_impl
hermes kanban watch
```

The coder calls `kanban_request_review` when done; the reviewer returns work
with `kanban_request_changes` or closes it with `kanban_complete`. The
orchestrator posts the merged summary to Discord.
