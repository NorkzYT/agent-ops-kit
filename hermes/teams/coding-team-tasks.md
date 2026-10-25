# Coding team: task prompts

Prompts kept from the CrewAI planner crew, ready to paste into
`hermes kanban create "<title>" --assignee <profile>` descriptions.

## plan_task (assignee: hermes)

```
Task to plan: {task_description}
Context files (if any): {context_files}

Produce a structured implementation plan with:
1. Summary of what needs to be built/changed
2. Ordered implementation steps (atomic, each independently testable)
3. Files to create or modify
4. Test commands to verify each step (shell commands that return 0 on pass)
5. Acceptance criteria (done when: ...)
6. Risks and edge cases to handle

Expected output: a structured markdown plan ready to hand to the coder profile.
```

## review_task (assignee: reviewer, parent: plan_task)

```
Review the implementation plan in the parent task. Check:
- Are all steps atomic and independently verifiable?
- Are test commands concrete (actual shell commands)?
- Are edge cases covered?
- Is anything ambiguous?

Output the final reviewed plan with improvements applied. If the plan is
already solid, output it unchanged with an "LGTM" note.
```
