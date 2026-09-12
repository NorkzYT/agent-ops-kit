# Profiles and teams

A **profile** is a specialist agent: a name, its own instructions, its own
knowledge, its own memory, optionally its own Discord bot. It is the Custom-GPT
idea done with files. A **team** is several profiles working one Kanban board.

## Create a profile

```bash
make hermes-profile NAME=marketing
```

This clones the default configuration (same proxies, same toolsets) into
`~/.hermes/profiles/marketing/`, installs `hermes/profiles/marketing/SOUL.md`
as its instructions, creates a `knowledge/` folder, and writes its `.env`.
Talk to it with `marketing chat` or `hermes -p marketing chat`.

Shipped profiles: `coder`, `marketing`, `strategy`, `security`, `research`,
`windows-operator`. Start a new one by copying `hermes/profiles/_template`.

## Give it knowledge from books

Put books and notes in the profile's knowledge folder as `.md` or `.txt` (one
book per file, chapters as headings; convert EPUB with `pandoc book.epub -t
gfm -o book.md`). Then, in that profile's chat:

```
/learn ~/.hermes/profiles/herbalism/knowledge
```

Hermes builds a knowledge-base skill: a short `SKILL.md` index plus one
distilled file per topic that loads only when a question needs it. A 400-page
book does not sit in every prompt. Re-running `/learn` merges new material into
the existing skill. Ask the profile "which source says that?" and it cites the
file.

## Give it its own Discord bot

Create a second application in the Discord developer portal, then:

```bash
PROFILE_DISCORD_BOT_TOKEN=<token> make hermes-profile NAME=marketing
marketing gateway install
```

The profile now answers as its own bot, with the same allowed-users list.
Profiles share the Honcho `peerName`, so what one learns about you is known
to all.

## Teams on the Kanban board

Hermes ships a board (`hermes kanban init` once). Tasks have an assignee
profile, optional parent tasks, and review states. The gateway dispatches:
when a task becomes ready, it spawns `hermes -p <assignee>` with the task
attached; the worker calls `kanban_complete`, `kanban_request_review` or
`kanban_block`; a reviewer answers with `kanban_request_changes` or
`kanban_complete`. The orchestrator posts the synthesis to Discord.

```bash
hermes kanban create "ICP and segments for Kairo"          --assignee research
hermes kanban create "Positioning and messaging matrix"    --assignee marketing --parent t_xxx
hermes kanban create "Review pricing and positioning"      --assignee strategy  --parent t_yyy
hermes kanban watch
```

Recipes with role prompts kept from the earlier CrewAI crews are in
`hermes/teams/`: `coding-team.md` (plan, implement, review) and
`marketing-team.md` (research, position, plan, review).

**Consensus.** The pipeline is draft, critique, revise, final. Each profile
sees the previous outputs, disagreements are written into the task comments,
and the orchestrator resolves them in the final answer. This is more
controllable than a free-form debate and leaves an audit trail on the board.

Template settings: `kanban.dispatch_in_gateway: true`, a tick every 60 s, at
most two tasks running at once. Raise `max_in_progress` when the host has
headroom.

## Working from Super Productivity

Pattern: a cron job (see `hermes/cron-jobs.md`) reads your task list each
morning, posts each task with a proposed plan to Discord, and waits. Only after
an explicit yes does it create the Kanban tasks. The orchestrator's rule is in
`SOUL.md`: never start without approval, never deviate from the approved plan.
Reading the tasks needs a small skill that knows your Super Productivity
export or sync location; write it with `/learn` on a sample export or by
describing the file format to Hermes.

## Where things live

```
hermes/profiles/<name>/SOUL.md     tracked in this repo (the prompt)
~/.hermes/profiles/<name>/         runtime: config, .env, knowledge/, skills/, memory
~/.hermes/kanban.db                the board
```
