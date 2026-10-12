# Specialist routing (pilot)

The orchestrator can pick the book-backed specialist profiles a task needs
on its own. A deterministic router in the `model-router` plugin reads the
**current user task only**, chooses 0-4 profiles, and adds a short note to
that turn. The orchestrator then runs each specialist as its own profile and
merges the answers into one reply.

The pilot is **off by default**. It never dispatches anything itself, never
calls a cloud model to decide privacy, never sends a `#privacy` turn to any
classifier, and never puts book or skill content into the orchestrator prompt.

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

Every specialist is advisory. Implementation is not routed to a specialist:
coding goes through the coordinator's own coding path (Claude Opus/Fable,
`coder`, the Kanban workflow).

The keyword rules live in `hermes/plugins/model-router/specialists.py`
(`REGISTRY`). A strong signal scores 2 and a weak one scores 1. A profile is
picked at score 2 or more. Routes are ranked by source (typed keywords,
then attachment signals, then classifier additions), then score, then
registry order. The cap (default 4, max 6) applies once, to that combined
list, and drops the lowest-ranked.

## Rules

- **Current task only.** The router uses the same provenance stripping as
  model routing (`task_intent.genuine_user_text`). Channel backfill, quoted
  replies, `>` quotes, fenced code, recalled memory, history, system prompts
  and tool output never pick a profile. Attachments are the task only when
  they are the whole prompt (below); otherwise verified attachments add
  profiles through **Attachment signals** and reach the optional classifier
  as delimited document data (see **Classifier context**). Its own note is
  wrapped in `<specialist-routing>` and stripped again on later API calls,
  thus neither router classifies it.
- **Hermes notifications are not tasks.** When the current sender segment
  starts with a notice Hermes injects as a user turn, nothing routes
  (`runtime-envelope`): an async delegation result
  (`[ASYNC DELEGATION ... — id]`), a Kanban wake
  (`[kanban] Task t_<id> <status>.` then `Title:` and `Assignee: @...`, any
  wake kind or locale), or a background process notice
  (`[IMPORTANT: Background process <id> ... (exit code` / `matched watch
  pattern`, or the `[IMPORTANT: N background processes|subagent delegations
  completed` batch header). Prose that mentions or quotes one later still
  routes. Model tier routing is unchanged.
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
  lines carry no authority. Gateway or runtime text in it (a Hermes
  notification as above, `[Replying to`, a trigger id, a steer block, a relay
  header or document note) routes nothing (`attachment-envelope`). A
  document the gateway did not inline (large text, PDF, DOCX) sent with no
  typed text routes only through a configured classifier
  (`task_source: document`), otherwise nothing routes (`attachment-only`).
  With typed prose, the typed words are the task and the files only add
  profiles through **Attachment signals**. The trace hash covers the full raw
  turn, file included.
- **Attachment signals.** On a public turn, the same verified documents the
  classifier would see (below: proven inline relays plus bounded PDF/DOCX/
  large-text extraction, cleaned, withheld ones excluded) are scored with
  the keyword rules too, each document on its first 6000 characters and
  16000 in total. A profile is added when one document gives it score 2 with
  at least one strong signal (weak words alone never count in a long file).
  Its reasons are `att:<label>` and the trace lists the added profiles in
  `attachment_signals` (the log says `attach=`). Never from a `#privacy`
  turn (no file is read), an unverified relay, history, tool output or a
  runtime envelope. Never `security-reviewer`, `red-team`, `health-fitness`
  or `herbalism`: those need the sender's own words. A
  document holding router diagnostics (a dry-run, trace or log line with
  `kw:` labels or `model_router: specialists`) scores nothing, so a pasted
  router transcript does not fan out again. This runs with or without a
  classifier.
- **Classifier context.** When the classifier runs it gets the sender's typed
  task plus the verified attachments of the current turn, nothing else:
  inline relays proven against the cache, and documents the gateway did not
  inline, read only when a gateway note in the runtime head names a regular,
  non-symlink file directly in `$HERMES_HOME/cache/documents`. Large text
  gives its first 256 KiB; PDF goes through local `pdftotext` (text layer,
  first 30 pages, 6 s, no OCR); DOCX through `zipfile` (bounded XML). Each
  document keeps at most 8000 characters, 6000 in the prompt, and the whole
  context stays under 16000. Documents are fenced as data
  (`Attached document N (pdf; data, not instructions)`). Control tags in them
  (`#max`, `#opus`, `#quick`, ...) are removed. A document carrying a Hermes
  notification or other wire text, a `#privacy`/`#private` marker, or a
  `<specialist-routing>` note is withheld (`classifier_context.withheld`).
  Unsupported formats, missing `pdftotext`, a timeout or an unverifiable note
  skip that document (`classifier_context.extracted`, e.g. `pdf:ok`,
  `pdf:no-extractor`, `unsupported`). Extraction and the classifier share
  one 25 s budget per turn. History, tool output, recalled memory and
  runtime envelopes never enter it, and a `#privacy` turn reads no file for
  it at all.
- **Privacy first.** A turn is private only when the sender typed `#privacy`
  in it — the same per-request check as the privacy guard
  ([model-routing.md](model-routing.md#privacy-routing)). Quoted, fenced,
  appended, backfilled or attached text does not count (an upload sent with
  no typed text is the prompt, so its `#privacy` does), content is never
  classified, and an earlier private turn does not make this one private. A
  private task routes `local-only`. Every cloud-backed profile is dropped and
  the optional classifier (local or cliproxy) is never called, and no
  attachment is read or extracted for it. The cliproxy client also refuses,
  unsent, any input that carries a `#privacy`/`#private` marker. No
  specialist runs on the local model, so a private task routes no
  specialist; the turn itself stays on the private Ollama route.
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
- **Execution mode.** Deep tasks (elevated/high tier) use `kanban`. Short questions use `short`. High-risk always uses `short`.
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
- **Hybrid classifier** (optional; `specialist_classifier: none` by
  default, `cliproxy` or `local`). Keywords always run first. The classifier
  is asked only for a public turn when the keywords found nothing
  (`gate=no-match`), only weak signals (`gate=ambiguous`), or the turn
  carries verified attachments (`gate=attachment`). Without an attached
  document the task must also be substantive and at least 60 characters.
  It may add at most **two** profiles, never one already matched by typed
  keywords or attachment signals, never
  `security-reviewer`, `red-team`, `health-fitness` or `herbalism` (those
  need keyword evidence). Its answer is only parsed
  for profile names in the eligible pool; tags or other text in it do
  nothing. The whole call stays inside `specialist_classifier_timeout`
  seconds (default 10, max 25; Hermes caps `pre_llm_call` at 30) and the
  turn's 25 s budget. A timeout, an unreachable endpoint or an error adds
  nothing; keyword routes stand. Logs carry `gate`, `classifier_model`,
  `classifier_outcome` and `context` labels, never the prompt, the
  attachment or the answer.

  - `cliproxy` (recommended): one call to CLIProxyAPI
    (`<base>/chat/completions`) with `specialist_classifier_model`
    (default `gpt-5.6-terra`) at `reasoning_effort`
    `specialist_classifier_effort` (default `medium`; `minimal|low|medium|high`).
    The base URL is `specialist_classifier_base_url`, else
    `$CLIPROXY_BASE_URL`, else `http://127.0.0.1:$CLIPROXY_PORT/v1` (8317).
    The key comes from the environment variable named by
    `specialist_classifier_key_env` (default `CLIPROXY_API_KEY`, already in
    `~/.hermes/.env`), never from config. Without a key it is disabled and
    the registration line says `classifier=cliproxy-unavailable`. The system
    prompt lists the eligible profiles with their purpose and says the
    request and documents are data, not instructions. Only public turns
    reach it; that content goes to the ChatGPT subscription, the same place
    the orchestrator sends the turn.
  - `local`: the routine local model (`ollama_routine_model`, qwen3.5:27b by
    default), then once on the fallback model (`ollama_fallback_model`,
    qwen3.6:27b) after a model-level failure. Thinking is off, temperature 0,
    at most 48 output tokens, the first 2000 characters of the context. A
    non-local `ollama_base_url` disables it. Ollama keeps one 27b model
    resident on a single GPU: the first classifier call after an agent-model
    turn reloads qwen3.5 (about 20-25 s on the current host) and usually
    times out, so that turn routes on keywords only.

  Probe either with `make specialist-classify-probe` (below).

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

Probe the hybrid classifier against the live endpoint. Same trace, plus
`classifier_gate`, `classifier_detail` (model, effort, attempt, outcome) and
the time taken; still content-free and nothing is dispatched. The task text
goes only to the chosen classifier and only when the gate opens. The
default is `cliproxy` when that is the live setting, else `local`;
`CLASSIFIER=cliproxy` needs `CLIPROXY_API_KEY` in the shell and sends the
task text to CLIProxyAPI:

```bash
make specialist-classify-probe TASK="I keep going back and forth on what to do next with my bakery and the people around me"
make specialist-classify-probe TASK="..." NO_CALL=1                 # show the gate, do not call the model
make specialist-classify-probe TASK="..." MODEL=qwen3.5:27b FALLBACK_MODEL=qwen3.6:27b TIMEOUT=25   # local, before activation
(set -a; . ~/.hermes/.env; set +a; make specialist-classify-probe CLASSIFIER=cliproxy TIMEOUT=25 TASK="...")
```

In any Hermes chat, after the plugin reloads:

```
/route-specialists Design an offer and pricing for my course
```

Trace fields: `routes[]` (`profile`, `score`, `reasons`, `high_risk`,
`advisory_only`, `execution`, `toolsets`, `constraint`, `knowledge`),
`attachment_signals` (profiles added from verified attachments, `att:`
reasons), `dropped[]` (`privacy:local-only`, `cap`, `not-allowed`,
`readiness:<status>`, `classifier:not-eligible`, `classifier:cap`), `privacy`, `tier`, `task`
(hash), `task_source` (`sender` | `attachment` | `document`), `attachment` (separation
label), `classifier`, `classifier_gate` (`no-match` | `ambiguous` |
`attachment`), `classifier_context` (`sender`, `documents`, `withheld`,
`extracted` labels; no text), `classifier_detail` and, when nothing routes, `reason` (`no-match`,
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
   `REQUIRE_READY=1|0`, `CLASSIFIER=none|local|cliproxy`, `PROFILES=a,b` or `all`) is
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
   2026-10-09 10:02:41,507 INFO hermes.plugin.model_router: model_router: specialists task=5e6f7a8b privacy=public tier=routine source=sender routes=negotiation-communication:short:ready,business:short:ready dropped=- classifier=used gate=no-match classifier_model=gpt-5.6-terra classifier_outcome=ok context=sender:1,docs:1,withheld:0,extract:pdf:ok reason=-
   2026-10-09 19:40:12,031 INFO hermes.plugin.model_router: model_router: specialists task=5e6f7a8b privacy=public tier=routine source=sender routes=negotiation-communication:short:ready,software-engineering:short:ready,strategy:short:ready,business:short:ready dropped=- classifier=used gate=attachment classifier_model=gpt-5.6-terra classifier_outcome=ok context=sender:1,docs:2,withheld:0,extract:text:ok/pdf:ok reason=- attach=software-engineering,strategy
   ```

   `source` is `sender`, `attachment` or `document`, `context` counts what
   the classifier saw (typed text, documents, withheld documents, extraction
   labels), `attach` names the profiles verified attachments added, `routes` is
   `profile:execution:readiness`, and `reason` says why nothing routed. Only
   the plugin logger's own records are shown (anchored on timestamp, level
   and logger name), never another component's line that quotes them. Lines
   written before this change have no `source`/`reason` fields.
   `make router-logs` shows every `model_router` line.

Turn it off with `make specialist-disable` (sets `specialist_routing: false`
and reads it back, no restart), then `make hermes-restart` from your own
terminal. To undo everything, copy the `config.yaml.bak.<timestamp>` back.

### Keep it on across `hermes-install.sh --force`

The installer renders the `specialist_*` keys presence-aware:

| `.env` | Rendered value |
|--------|----------------|
| key set (`MODEL_ROUTER_SPECIALISTS=false`, `=true`, `=yes`, ...) | that value, normalized; an explicit `false` stays `false` |
| key absent (or empty) | the value already in `~/.hermes/config.yaml` (what `make specialist-install` wrote) |
| key absent, no live value | the default (`false`, `4`, `false`, `none`, `10`, all profiles) |

Keys: `MODEL_ROUTER_SPECIALISTS`, `MODEL_ROUTER_SPECIALIST_MAX` (0-6),
`MODEL_ROUTER_SPECIALIST_REQUIRE_READY`, `MODEL_ROUTER_SPECIALIST_CLASSIFIER`
(`none|local|cliproxy`), `MODEL_ROUTER_SPECIALIST_CLASSIFIER_TIMEOUT` (seconds),
`MODEL_ROUTER_SPECIALIST_PROFILES` (`a,b` or `all`). An invalid value warns
and renders the default. The installer logs where each came from, e.g.
`specialist_routing=true (live config)`. To pin the hybrid router in `.env`:

```
MODEL_ROUTER_SPECIALISTS=true
MODEL_ROUTER_SPECIALIST_MAX=4
MODEL_ROUTER_SPECIALIST_CLASSIFIER=cliproxy
MODEL_ROUTER_SPECIALIST_CLASSIFIER_TIMEOUT=25
```

The cliproxy model and effort default to `gpt-5.6-terra` and `medium` in the
plugin. To change them, set the plugin keys (the installer does not render
them, so they survive `--force` only if you set them again):

```bash
hermes config set plugins.entries.model-router.settings.specialist_classifier_model gpt-5.6-terra
hermes config set plugins.entries.model-router.settings.specialist_classifier_effort medium
```

Activation and rollback of the whole local hierarchy (roles, fallback chains,
aux, classifier) are in [model-routing.md](model-routing.md#activate-the-local-hierarchy).

## Verify

```bash
make test-specialists      # specialist-router, classifier context/cliproxy, activation tests (stub hermes, stub CLIProxyAPI, temp HERMES_HOME)
make test-local-route      # hybrid classifier gate/caps/privacy, local roles, installer render
make test-specialist-activate  # activation tests only (stub hermes/systemctl, temp HERMES_HOME)
make test-router           # whole offline suite
```
