# Specialist routing (pilot)

The orchestrator can pick the book-backed specialist profiles a task needs
on its own. A deterministic router in the `model-router` plugin reads the
**current user task only**, chooses 0-4 profiles, and adds a short note to
that turn. The orchestrator then runs each specialist as its own profile and
merges the answers into one reply.

The pilot is **off by default**. It never dispatches anything itself, never
calls a cloud model to decide privacy, and never puts book or skill content
into the orchestrator prompt.

## What it routes to

| Profile | Picked for | High-risk |
|---------|-----------|-----------|
| `strategy` | business model, competitive position, prioritisation, pivots | |
| `business` | unit economics, cash flow, runway, business plans | |
| `offers-sales` | offer design, pricing, lead gen, sales funnels | |
| `marketing` | positioning, messaging, campaigns, channels | |
| `leadership-teamwork` | teams, one-on-ones, feedback, hiring | |
| `software-engineering` | architecture, review standards, testing strategy | |
| `security-reviewer` | security review, threat model, vulnerabilities | yes |
| `red-team` | scoped red-team and pentest planning on owned targets | yes |
| `negotiation-communication` | negotiations, hard conversations, message drafts | |
| `health-fitness` | training, nutrition, recovery | yes |
| `herbalism` | plants, preparations, traditional use | yes |
| `survival` | preparedness, emergency kits, first aid | |
| `martial-arts` | grappling technique, conditioning | |
| `philosophy` | stoicism, ethics, mental models | |
| `local-engineer` | implementation: implement, fix a bug, add tests | |

The keyword rules live in `hermes/plugins/model-router/specialists.py`
(`REGISTRY`). A strong signal scores 2 and a weak one scores 1. A profile is
picked at score 2 or more. Ties keep registry order, and the cap (default 4,
max 6) drops the lowest scores.

## Rules

- **Current task only.** The router uses the same provenance stripping as
  model routing (`task_intent.genuine_user_text`). Channel backfill, quoted
  replies, `>` quotes, fenced code, recalled memory, attachments, history,
  system prompts and tool output never pick a profile. Its own note is
  wrapped in `<specialist-routing>` and stripped again on later API calls,
  thus neither router classifies it.
- **Privacy first.** The raw current message, including any quoted or
  appended text, goes through the privacy guard's deterministic detectors:
  `#private`/`#local`, ID numbers, cards, bank numbers, secrets, credentials,
  and your `private_terms`/`private_paths`. A session the guard has already
  flagged also counts. A private task routes `local-only`. Every cloud-backed
  profile is dropped and the optional classifier is never called. Only
  `local-engineer`, which runs on private Ollama, stays eligible.
- **High-risk is advisory.** `health-fitness`, `herbalism`,
  `security-reviewer` and `red-team` always run `short` with only the
  `skills` toolset. That means no terminal, files, web, browser, messages or
  Kanban, thus no external or high-impact action. The classifier can never
  add a high-risk profile.
- **Isolated profiles.** `kanban` means a board task with the profile as
  assignee. The dispatcher runs it under that profile's own `HERMES_HOME`,
  SOUL, memory and skills. `short` means one bounded answer from
  `hermes -p <profile> -z '<brief>' -t skills`. `delegate_task` is never used
  for specialists because it cannot carry a profile.
- **Execution mode.** Deep tasks (elevated/high tier) and implementation use
  `kanban`. Short questions use `short`. High-risk always uses `short`.
- **Knowledge readiness** is checked in the installed profile, never assumed
  from the repo templates. The check only lists files and reads `SKILL.md`
  frontmatter. It never opens a book.

| Status | Meaning |
|--------|---------|
| `ready` | A `/learn` knowledge skill exists. `stale: true` means `knowledge/` has newer files, so re-run `/learn` |
| `needs-learn` | Books are in `knowledge/`, no knowledge skill yet |
| `no-knowledge` | Installed, empty `knowledge/` |
| `not-installed` | No `~/.hermes/profiles/<name>`: always dropped |

  `/learn` leaves no marker in the skill. A skill counts as the knowledge
  skill when it is named `<profile>-knowledge`. Otherwise it must be outside
  the bundled and shared skills and either mention
  `profiles/<profile>/knowledge` or `knowledge-base` in its frontmatter, or
  have `references/*.md`. To make detection exact, name the skill when you
  learn:

  ```
  /learn ~/.hermes/profiles/strategy/knowledge  name the skill strategy-knowledge
  ```

  Unready profiles still route, with their status shown, unless
  `specialist_require_ready: true`.
- **Ambiguous tasks** (optional): `specialist_classifier: local` lets the
  local Ollama model suggest at most two non-high-risk profiles. It runs only
  when the task is not private, has no keyword match and is long enough to
  be real work. The default is `none`. There is no cloud option.

## Dry run first

Offline. Nothing is dispatched, and the output never contains the task text.

```bash
TASK="Design an offer and pricing for my course, and a launch campaign" make specialist-route
TASK="..." make specialist-route PLAN=1            # also print the note the orchestrator gets
python3 scripts/specialist-route.py --profiles-root /tmp/empty "..."   # fixture root
```

In any Hermes chat, after the plugin reloads:

```
/route-specialists Design an offer and pricing for my course
```

Trace fields: `routes[]` (`profile`, `score`, `reasons`, `high_risk`,
`advisory_only`, `execution`, `toolsets`, `constraint`, `knowledge`),
`dropped[]` (`privacy:local-only`, `cap`, `not-allowed`,
`readiness:<status>`, `classifier:not-eligible`), `privacy`, `tier`, `task`
(hash) and `classifier`.

## Turn it on

1. Dry-run a few real tasks (above) and check the routes.
2. Build each profile's knowledge skill. Today every profile reports
   `needs-learn` until you do this:

   ```bash
   make knowledge-refresh
   hermes -p <profile> chat     # then: /learn ~/.hermes/profiles/<profile>/knowledge  name the skill <profile>-knowledge
   ```

3. Install the updated plugin and enable the pilot. `make hermes-install`
   copies `hermes/plugins/model-router/` into `~/.hermes/plugins/`. It does
   not overwrite an existing `config.yaml`; it writes `config.yaml.agent-ops-kit`
   next to it (rendered from `MODEL_ROUTER_SPECIALISTS` and
   `MODEL_ROUTER_SPECIALIST_MAX` in `.env`). Merge the `specialist_*` keys by
   hand:

   ```yaml
   plugins:
     entries:
       model-router:
         settings:
           specialist_routing: true
           specialist_max_profiles: 4        # 0-6
           specialist_require_ready: false   # true = drop profiles without a knowledge skill
           specialist_classifier: none       # none | local
           specialist_profiles: []           # allowlist; empty = all of the above
   ```

4. Restart the gateway yourself (`make hermes-restart`) when you are ready.
   The hook registers only in the `default` profile
   (`specialist_orchestrators`), never in a specialist or a subagent turn.
5. Watch the decisions. They are content-free:

   ```bash
   make hermes-logs | grep 'model_router: specialists'
   ```

Turn it off with `specialist_routing: false` and a restart.

## Verify

```bash
make test-specialists      # specialist-router tests only
make test-router           # whole offline suite
```
