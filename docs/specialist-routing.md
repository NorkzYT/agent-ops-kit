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
- **Inline attachments are not the task.** A text file the platform inlines
  (`[Content of NAME]:` plus the file text, with no end marker) is cut out by
  comparing it with the cached copy the gateway note names. Only a regular,
  non-symlink file directly in `$HERMES_HOME/cache/documents` is read, at most
  1 MiB. Every typed paragraph routes. If the file and the relay do not match
  exactly, the note path is outside the cache, or the boundary is ambiguous,
  the turn routes nothing (`attachment-unverified`). The legacy
  `document_cache/` layout and plugin terminal backends with a custom cache
  base always fail closed this way.
- **A bare upload is the prompt.** When the sender typed nothing (for example
  only `prompt.txt` was sent), the verified relay text routes instead and the
  trace says `task_source: attachment`. The note to the orchestrator and
  `make specialist-route` mark it `attachment-derived`; the live log line says
  `source=attachment`. The same checks apply first, so an unverified relay
  still routes nothing. Inside the file, `#max`/`#opus`/`#quick` control
  lines carry no authority. Gateway or runtime text in it (an async
  delegation envelope, `[Replying to`, a trigger id, a steer block, a relay
  header or document note) routes nothing (`attachment-envelope`). The
  optional classifier never sees file text (`skipped:attachment`). A document
  the gateway did not inline (large text, PDF, DOCX) is never read for
  routing (`attachment-only`). Typed prose always wins over the file. The
  privacy scan and the trace hash cover the full raw turn, file included.
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

  Or let `make knowledge-learn NAME=<profile>` do it headless with that exact
  name. The router's `ready` only means the skill exists;
  `make knowledge-learn-status` shows how many synced files it has learned
  ([knowledge-learn.md](knowledge-learn.md)).

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
make specialist-route TASK=- < prompt.txt          # the task text from a file
make specialist-route                              # no task: usage, exit 0
python3 scripts/specialist-route.py --profiles-root /tmp/empty "..."   # fixture root
```

The JSON trace is followed by a summary:

```
--- summary ---
task 91d1e4d8c8b9d337  source typed  privacy public  tier elevated  classifier off
selected 2/4:
  security-reviewer          short  knowledge no-knowledge  reasons kw:review,kw:authz,kw:security  [HIGH-RISK advisory-only]
  marketing                  kanban knowledge ready  reasons kw:marketing  [advisory]
profiles root /home/you/.hermes/profiles
```

`source` is `typed` or `attachment-derived`. With no route the summary says
why, for example `no specialist selected (no-match)`; dropped profiles are
listed with their reason.

In any Hermes chat, after the plugin reloads:

```
/route-specialists Design an offer and pricing for my course
```

Trace fields: `routes[]` (`profile`, `score`, `reasons`, `high_risk`,
`advisory_only`, `execution`, `toolsets`, `constraint`, `knowledge`),
`dropped[]` (`privacy:local-only`, `cap`, `not-allowed`,
`readiness:<status>`, `classifier:not-eligible`), `privacy`, `tier`, `task`
(hash), `task_source` (`sender` | `attachment`), `attachment` (separation
label), `classifier` and, when nothing routes, `reason` (`no-match`,
`all-dropped`, `no-current-task`, `runtime-envelope`, `attachment-unverified`,
`attachment-only`, `attachment-envelope`, `status-or-continuation`).

## Turn it on

1. Dry-run a few real tasks (above) and check the routes.
2. Build each profile's knowledge skill. A profile reports `needs-learn`
   until you do this:

   ```bash
   make knowledge-refresh
   make knowledge-learn NAME=<profile>      # headless; or /learn by hand, see above
   ```

3. Check where you are. Read-only: plugin files against the repo, the
   `specialist_*` settings, the gateway, whether a restart is pending, and
   knowledge readiness per profile.

   ```bash
   make specialist-status
   ```

4. Install. This copies `hermes/plugins/model-router/` into
   `~/.hermes/plugins/` and sets the keys with `hermes config set`, then reads
   every key and file back. It **never restarts** the gateway, so the running
   bot keeps its old settings. Preview first:

   ```bash
   make specialist-install DRY_RUN=1
   make specialist-install                        # MAX=4 REQUIRE_READY=0 CLASSIFIER=none PROFILES=all
   ```

   It sets `specialist_routing: true`. A key you pass (`MAX=0-6`,
   `REQUIRE_READY=1|0`, `CLASSIFIER=none|local`, `PROFILES=a,b` or `all`) is
   written. A key you do not pass keeps its current value, or gets the default
   when it is missing. With `REQUIRE_READY=1` it stops if no allowed profile
   has a knowledge skill yet, because nothing would route.

   `hermes config set` rewrites `config.yaml` without its comments, so the
   target saves `config.yaml.bak.<timestamp>` first. It stores `none` as
   `null`, which the plugin reads as `none`. A replaced plugin copy goes to
   `~/.hermes/backups/agent-ops-kit/`. The installer needs `model-router` in
   `plugins.enabled` and stops otherwise; it never edits that list.

5. Enable, **from your own terminal**. This runs step 4, then restarts the
   gateway and waits until a fresh `model_router registered (...
   specialists=on)` line appears in `~/.hermes/logs/agent.log`.

   ```bash
   make specialist-enable                         # asks before restarting
   make specialist-enable YES=1                   # no question (YES counts only on the command line)
   ```

   It restarts only when the config or plugin changed after the gateway
   started, and only a running gateway (a stopped one is left for
   `hermes gateway start`). It refuses to restart from inside Hermes: a
   gateway session (`HERMES_GATEWAY_SESSION` and similar), a process under
   the gateway, or a turn served by `claude-max-proxy.service`. Restarting
   from there kills the turn that asked. It prints the command to run instead.

   | Exit | Meaning |
   |------|---------|
   | 0 | done (restarted and verified, not needed, or you answered no) |
   | 1 | preflight, write or read-back failed |
   | 3 | installed and verified, not restarted (refused here, no TTY without `YES=1`, gateway not running) |
   | 4 | restarted, but no `specialists=on` line within `WAIT` seconds (default 90) |

   The hook registers only in the `default` profile
   (`specialist_orchestrators`), never in a specialist or a subagent turn.

6. Watch the decisions. They are content-free (task hash, profiles, labels):

   ```bash
   make specialist-logs                # last 20 decisions (LINES=<n>)
   make specialist-logs-follow         # same, then follow live
   ```

   ```
   2026-10-07 08:22:10,111 INFO hermes.plugin.model_router: model_router: specialists task=1a2b3c4d privacy=public tier=medium source=attachment routes=marketing:short:ready dropped=- classifier=off reason=-
   ```

   `source` is `sender` or `attachment`, `routes` is
   `profile:execution:readiness`, and `reason` says why nothing routed. Only
   the plugin logger's own records are shown (anchored on timestamp, level
   and logger name), never another component's line that quotes them. Lines
   written before this change have no `source`/`reason` fields.
   `make router-logs` shows every `model_router` line.

Turn it off with `make specialist-disable` (sets `specialist_routing: false`
and reads it back, no restart), then `make hermes-restart` from your own
terminal. To undo everything, copy the `config.yaml.bak.<timestamp>` back.

## Verify

```bash
make test-specialists      # specialist-router + activation tests (stub hermes, temp HERMES_HOME)
make test-specialist-activate  # activation tests only (stub hermes/systemctl, temp HERMES_HOME)
make test-router           # whole offline suite
```
