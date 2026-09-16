# Claude Autopilot Task Template (copy/paste)

Fill in the 4 sections. Then paste the block below into Claude Code.

(Replace <...> placeholders with real text; remove unused sections.)

> When you use the autopilot subagent, it runs the full pipeline (plan → implement →
> verify → review → close) and works until the Definition of Done is met.

```text
Use the autopilot subagent.

1) GOAL (what you want)
- <One sentence goal>

2) DEFINITION OF DONE (how we know it’s finished)
- [ ] <Pass/fail result #1>
- [ ] <Pass/fail result #2>
- [ ] <Tests/lint/build pass OR specific manual check steps pass>

3) CONTEXT (optional and helpful)
- Product/area: <name>
- Constraints: no refactors; no network/destructive commands unless approved; follow repo patterns
- Suspected files/keywords (if you have them): <paths or terms>

4) DETAILS (paste everything relevant)
<<<
- Errors/logs:
  <paste>
- Repro steps (if bug):
  1) <step>
  2) <step>
- Expected vs actual:
  Expected: <...>
  Actual: <...>
- For architecture tasks, include requirements:
  - Scope IN: <...>
  - Scope OUT: <...>
  - Offline needs (storage/sync/conflicts): <...>
  - Environments (local/dev/stage/prod): <...>
>>>
```

---

## Two examples of “what to replace”

### Example A (bug)

**GOAL:** “Fix export crash and ensure WS products get eligibility quickly.”  
**DoD:**

- [ ] Export CSV/HTML does not throw `replace is not a function`
- [ ] WS products show eligibility counts correctly
- [ ] Verified by running the scanner on 5 pages and exporting

### Example B (architecture MVP)

**GOAL:** “Produce offline-first architecture + repo/env plan and minimal scaffolding docs.”  
**DoD:**

- [ ] Docs created: offline strategy + repo structure + env strategy
- [ ] Includes conflict resolution + failure modes
- [ ] `.env.example` + setup instructions added (if repo needs it)

---

## One rule that makes this work

When in doubt, put more into **DETAILS**. Autopilot is strongest when it has the raw error text, the exact UI text, and the real constraints.

---

## How the pipeline works

1. **You paste** the task template with `Use the autopilot subagent.`
2. **Autopilot triages** the task and routes it to the right process tier.
3. **Work proceeds** through the pipeline: plan -> implement -> verify -> review.
4. **Autopilot keeps working** if verification fails or the DoD is not yet met.
5. **Task completes** when the DoD is satisfied — for complex work the `closer` confirms it and produces the PR-ready summary.

For larger, long-running efforts, delegate to a specialist profile and track it on the shared Kanban board:

```
hermes kanban create "<task>" --assignee coder
```
