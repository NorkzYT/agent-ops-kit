# Claude Code Constitution

> This is the root context file. Keep it under 200 lines. Domain-specific guidance lives in subdirectory CLAUDE.md files.

## Universal Rules

1. **Smallest change that satisfies the task.** No drive-by refactors.
2. **Discovery first:** Search/read before deciding. Use `rg` and `Read` tools.
3. **Follow existing patterns.** Match the repo's style, naming, and structure.
4. **Always verify:** Run repo checks (tests/lint/build) or provide explicit manual steps.
5. **No network or destructive commands** unless explicitly approved by the user.

## Quality Principles to Apply

• Modularity  
• Abstraction & Encapsulation  
• Separation of Concerns  
• SOLID (Single-responsibility, Open/Closed, Liskov, Interface-segregation, Dependency-inversion)  
• DRY (Don’t Repeat Yourself)  
• KISS (Keep It Simple, Stupid)

## Model Calibration (Claude 4.6+)

Current models follow instructions literally, so calm, plain wording works best — `ALL-CAPS`, "CRITICAL", and "YOU MUST" now *overtrigger* (overthinking, over-caution, over-delegation). When writing prompts or acting:

- **State steps plainly.** A required step reads as a normal instruction, not a shout.
- **Decide, don't stall.** For minor reversible choices (naming, defaults, equivalent approaches), pick a sensible option and note it. Still ask before scope changes or destructive/outward-facing actions.
- **Be concise.** Brief narration between tool calls, a short summary at the end; don't narrate routine actions.
- **Don't over-delegate.** Do the work directly in one pass when you can; spawn subagents only for genuinely independent or parallel streams.
- **Scale to the task.** Match verbosity and effort to complexity.

## Logging Convention

When adding logs, use: `internalLog.{debug,info,warn,error}`

## Reference Pointers (Just-in-Time Loading)

Instead of embedding full docs, load on-demand:

| Topic | Reference |
|-------|-----------|
| Workflow guide | Read `.claude/WORKFLOW.md` |
| Agent catalog | Read `.claude/agents/CLAUDE.md` |
| Hook documentation | Read `.claude/hooks/CLAUDE.md` |
| Session state | Read `.claude/docs/session-state.md` |
| Sentinel zones | Read `.claude/docs/sentinel-zones.md` |
| Browser verification | Read `.claude/skills/browser-automation/SKILL.md` |
| Hermes stack (Discord, Honcho, proxies) | Read `docs/hermes.md` |

## Hierarchical Context Architecture

```
.claude/
├── CLAUDE.md              # Constitution (this file)
├── agents/
│   ├── CLAUDE.md          # Agent-specific guidance
│   └── *.md               # Individual agents
├── hooks/
│   ├── CLAUDE.md          # Hook-specific guidance
│   └── *.py               # Hook implementations
├── docs/
│   ├── session-state.md   # Three-file pattern docs
│   └── sentinel-zones.md  # Protected code zones
└── context/               # Session state (gitignored)
    └── <task>/
        ├── plan.md        # High-level plan
        ├── context.md     # Key learnings
        └── tasks.md       # Granular checklist
```

## Execution Model (Hermes Delegation + Kanban)

Complex work is delegated to specialist profiles rather than run inline. Create a task on the shared Kanban board and assign it to the profile that fits:

```
hermes kanban create "Build a REST API with tests" --assignee coder
```

The `coder` profile picks up the task and runs the full pipeline: plan → implement → verify → commit → report. For finer control, the `delegate_task` tool hands a scoped unit of work to a specialist profile and returns its result. The `closer` agent is the final gate for completion.

## Default Agents

## Cost-Optimized Model Routing

- **Default model:** Opus for planning, triage, and implementation (`.claude/settings.json`). The lean process tiers (see `.claude/eval/FINDINGS.md`) fund the stronger model: spend on reasoning, not pipeline overhead.
- **Plan first:** Start with a short plan and complexity triage before doing work.
- **Downshift simple tasks to Sonnet:** mechanical 1-3 file changes that follow an existing pattern (docs, config tweaks, small bug fixes) don't need Opus.
- **Keep browser/vision usage explicit:** only use browser verification when UI behavior changed.

This buys the quality where it matters while protecting weekly usage on mechanical work.

If a session is running on a smaller model (downshifted or launched that way), escalate complex multi-file architectural work to the **autopilot-opus** subagent:
```
Use the autopilot-opus subagent (Task tool with subagent_type=autopilot-opus) for this task
```

For simpler tasks (1-3 files, following existing patterns), work directly -- no sub-agent needed.

For parallel multi-part tasks, use the **parallel-orchestrator** subagent.

For planning without execution, use the **orchestrator** subagent (forced delegation).

## Sentinel Zones (Do Not Modify Without Explicit Approval)

Protected areas that require explicit user approval before modification:
- `.env*` files (except `.env.example`, `.env.sample`, `.env.template`)
- `**/secrets/**`, `**/*secret*`, `**/*credentials*`
- `**/prod/**`, `**/production/**` configurations
- `**/*.pem`, `**/*.key` (certificate material)
- Code marked with `LEGACY_PROTECTED`, `DO_NOT_MODIFY`, or `SECURITY_CRITICAL` comments

See `.claude/docs/sentinel-zones.md` for full configuration.

## Session Resilience (Three-File Pattern)

For complex tasks spanning multiple sessions:

1. **plan.md** - High-level architectural plan (the North Star)
2. **context.md** - Scratchpad of key learnings, decisions, gotchas
3. **tasks.md** - Granular checklist of remaining work

Store in `.claude/context/<task-name>/` directory.

Use `Document & Clear` cycle: After major milestones, persist state to files before proceeding.

## Security Checks

For code handling input/auth/data:
- Spawn `security-auditor` agent for vulnerability analysis
- Spawn `threat-modeling-expert` for architecture-level concerns
- Check `.claude/hooks/guard_bash.py` for blocked patterns

## Timed Follow-Up Promises (Discord)

- Do not promise delayed follow-ups in natural language unless a real scheduler job is created.
- In Discord flows Hermes owns the scheduler: create the job with `hermes cron create` before saying "I'll check back in X".
- The same reply must include the cron job ID (or explicitly state the CLI did not return one).
- If scheduling fails, do not promise a delayed callback. Ask the user to ping again or continue monitoring now.

## Subdirectory Context Loading

Claude Code automatically loads CLAUDE.md from subdirectories when working in those areas:
- Working in agents? → `.claude/agents/CLAUDE.md` is loaded
- Working in hooks? → `.claude/hooks/CLAUDE.md` is loaded

This enables domain-specific guidance without bloating the root Constitution.

---

*This Constitution is the foundation. For detailed guidance, follow the Reference Pointers above.*
