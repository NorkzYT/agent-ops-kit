---
name: session-hygiene
description: Context rot prevention for coding agents. Track turn count, write progress checkpoints, suggest session splits before quality degrades.
---

# Session Hygiene — Context Rot Prevention

Use this skill passively throughout every session. It prevents the quality degradation that occurs after ~30 coding turns in a single agent session.

## Why Sessions Degrade

Even with automatic context compaction:

- Summarization loses nuance at high turn counts
- Tool call results accumulate and dilute key context
- Early decisions get compressed away, causing contradictions
- After ~30 coding turns, error rates increase significantly

## Turn Count Awareness

Track your approximate coding turn count mentally. A "coding turn" is any turn where you read, edit, or run code — not casual conversation.

| Turn Range | Status | Action |
|------------|--------|--------|
| 0-15 | Fresh | Work normally |
| 15-20 | Warm | Start writing progress notes |
| 20-25 | Hot | Write checkpoint, suggest fresh session |
| 25-30 | Degrading | Complete current sub-task, then split |
| 30+ | Risky | Wrap up immediately, split session |

## Checkpoint Protocol

At 15-20 coding turns, or after any major milestone:

### 1. Write Progress to Daily Notes

Write a checkpoint to `memory/YYYY-MM-DD.md`:

```markdown
## Checkpoint: <task name> (<time>)

**Done:**
- <completed items>

**In progress:**
- <current item and its state>

**Next:**
- <remaining items>

**Key decisions:**
- <important context that must survive session split>

**Blockers:**
- <anything blocking progress>
```

### 2. Update MEMORY.md (Critical Decisions Only)

If you made decisions that affect future sessions (architecture choices, naming conventions, discovered bugs), add them to `MEMORY.md`. Keep it concise — this is curated wisdom, not raw logs.

## Session Split Protocol

At 20-25 coding turns, proactively suggest a fresh session:

```
I've been working for ~20 coding turns and want to keep quality high.
I've written a progress checkpoint to memory/YYYY-MM-DD.md.

Suggest: Start a fresh session with `/new` to continue.
Summary of what's done and what's next: <2-3 sentences>
```

**Do not wait for the user to notice degradation.** Proactively suggest the split.

## Multi-Step Task Strategy

For tasks with 5+ sub-tasks:

1. Break into phases of 3-5 sub-tasks each
2. Complete one phase per session
3. Write a checkpoint at the end of each phase
4. Start fresh for the next phase

This is better than a 40-turn marathon that degrades halfway through.

## Before Compaction

If you sense compaction is about to happen (context getting large, repeated tool calls):

1. Write critical decisions to `MEMORY.md`
2. Write current task state to `memory/YYYY-MM-DD.md`
3. These survive compaction; your working memory doesn't

## Integration with autopilot-workflow

Session hygiene runs passively alongside the autopilot-workflow pipeline:

- During step 1 (Triage): Estimate how many turns the task will take
- During step 4 (Implement): Monitor turn count
- After step 7 (Report): If >20 turns used, suggest session split for next task
