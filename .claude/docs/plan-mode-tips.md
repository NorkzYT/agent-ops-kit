# Plan Mode Tips

## Context Rotation Strategy

Instead of using `/clear` when context gets large, use **Plan Mode** to preserve session knowledge across a context reset.

### The Problem

As your conversation grows, the context window fills up with:
- Previous code changes
- Discussion history
- File reads and searches
- Tool outputs

Eventually, you need to reset to continue working efficiently.

### The Solution: Plan Mode Context Rotation

**When to use**: At ~50% context usage (or when you notice Claude's responses slowing down)

**How it works**:

1. **Switch to PLAN mode** and send your next task/prompt
2. **Claude drafts a plan** using all accumulated context from the session
3. **Select "Yes, clear context and bypass permissions"** when prompted
4. Claude executes the plan in a fresh context with the plan as the new baseline

### Why This Works

The plan acts as a **knowledge checkpoint** that:
- Summarizes what's been done so far
- Captures key decisions and context
- Provides a roadmap for next steps
- Eliminates redundant conversation history

### When NOT to Use

- **Early in the session** (< 30% context) - no need yet
- **After a single simple task** - just continue normally
- **When context is still manageable** - save it for when you really need it

### Alternative Approaches

If you don't need a plan:
- Use `/clear` for a hard reset (loses all context)
- Use the **three-file pattern** (`.claude/context/<task>/`) to persist state manually
- Start a new session and reference previous work via file reads

### Best Practices

1. **Trigger before critical work** - Don't wait until context is 90% full
2. **Use descriptive prompts** - Give Claude enough context to write a comprehensive plan
3. **Review the plan** - Make sure it captures everything important before clearing
4. **Combine with session state** - For complex tasks, use plan mode + `.claude/context/` files

### Example Workflow

```
[At 50% context]
User: /plan
User: Continue implementing the API endpoints - we've done auth and user routes,
      now need to add post CRUD, comment system, and testing.

[Claude writes comprehensive plan]

User: [Selects "Yes, clear context and bypass permissions"]

[Fresh context with plan as baseline]
```

## Related

- See `.claude/docs/session-state.md` for the three-file pattern
