# Model routing & privacy

Two concerns, one bundled Hermes plugin (`hermes/plugins/model-router/`) plus
native config:

1. **Complexity routing** — pick the cheapest model that fits each request.
2. **Privacy routing** — keep private data off every cloud provider, fail-closed.

Secrets stay in `.env`; only routing knobs live in `config.yaml`.

## Providers at a glance

| Role | Provider | Endpoint | Models |
|------|----------|----------|--------|
| Orchestrator | CLIProxyAPI (ChatGPT) | `:8317/v1` | `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`, `gpt-6-astra` |
| Coding (delegation) | claude-max-proxy (Claude Max) | `:3456/v1` | `opus`, `sonnet`, `haiku`, `fable`, `default` |
| Local / private / aux | Ollama (OpenAI-compatible) | `:11434/v1` | a pulled chat model (readiness below) |

## Task-scoped routing (`llm_request` middleware)

Each request is classified deterministically from the **current user task
only** — the newest genuine `role: user` turn (Chat Completions `messages` or
Responses `input`). System prompts, assistant output, tool results, pasted
transcripts, fenced code and `>`-quoted lines are never signals, so a short
status question stays cheap no matter how much history the session carries,
and control tags inside quoted logs are inert. The model is rewritten **within
its own family**; only managed aliases are touched — a deliberate exact id is
left alone.

**Wire-format provenance (Discord):** the Hermes gateway ships each user turn
as one flat string with runtime material around the sender's text (verified
read-only against the installed core — `gateway/run_inbound.py`,
`agent/turn_context.py`, `agent/memory_manager.py`): reply/trigger metadata,
media notes and channel backfill are *prepended* (backfill ends with a literal
`[New message]` marker, then an inline `[sender]` prefix), while recalled
memory (`<memory-context>…</memory-context>`) and `--- Attached Context ---`
blocks are *appended*. The classifier reduces the turn to the sender-authored
segment first (`task_intent.genuine_user_text`), in provenance-safe order:
appended blocks are cut at the **first** marker (a sender faking one only
truncates their own text), the backfill is cut at the **last** `[New message]`
marker (forged copies in memory are already gone; forged copies in backfill or
quotes sit earlier), and only the gateway's exact metadata templates are
stripped from the front. So recalled memories, other users' channel messages,
quoted replies and attached files can neither inflate the tier nor grant
authorization — and a genuine leading `#max` still authorizes even when the
gateway wraps it in trigger metadata. Arbitrary bracket lines in a pasted
transcript are *not* stripped, so a `#max` buried under pasted content is not
a control line. `TestCoreWireContract` re-checks the installed core's marker
templates so a Hermes upgrade that changes them fails loudly.

| Task shape | Tier | Orchestrator (GPT) | Effort | Coding |
|------------|------|--------------------|--------|--------|
| status / lookup / report question | routine | `gpt-5.6-luna` | low | `opus` |
| ordinary execution / delegation | medium | `gpt-5.6-luna` | medium | `opus` |
| ambiguous / multi-constraint planning | elevated | `gpt-5.6-terra` | medium | `opus` |
| genuinely hard (debug/architecture/security) | high | `gpt-5.6-sol` | medium–high | `fable` |
| explicitly authorized max | max | `gpt-6-astra` | high | `fable` |

Sol requires **substantive** complexity (two distinct hard categories, or one
plus another strong signal — length, dense constraints, pasted code); a single
keyword routes to Terra at most.

**Control lines (current task only):**

- A **leading standalone** `#max` or `#astra` line (the first non-empty line of
  the task, containing only the tag) authorizes `gpt-6-astra` for that task.
- Inline `#max`, `#deep`, `#hard` are hard-work *hints* (Sol at most) — they no
  longer grant Astra, and they are ignored entirely inside quotes/code fences,
  tool output, or older turns.
- `#quick` / `#routine` / `#cheap` / `#simple` / `#low` force the routine tier.

**Bounded task state:** per session the plugin keeps only an opaque task hash,
tier/effort and the authorization bit (sliding TTL `task_ttl_seconds`, default
30 min, absolute cap 4×, LRU-bounded). A short "continue" retains the running
task's tier (and its authorization); a status question routes low without
resetting it; a new substantive request replaces it — authorization never
bleeds into the next task or session. A request with no user turn at all
classifies as medium, never max.

The absolute cap is enforced against the agent loop itself: re-deriving the
same current task on every tool iteration keeps the task's **original**
`created` stamp (a re-put never restarts the clock, and the hash is computed
from the sender-authored segment only, so it stays stable while backfill and
recalled memory change around it). When a task exhausts its absolute lifetime
its hash is **retired** for that session: the same historical `#max` message
sitting at the tail of the transcript can neither re-store state nor
re-authorize Astra on later iterations, and a "continue" after the deadline
starts fresh at medium. A genuinely new task (different text, fresh hash)
authorizes normally. Known limit: re-sending the *byte-identical* task text in
the same session after retirement is still treated as the retired task —
rephrase (or start a new session) to re-authorize.

**Budget ceiling & degrade:** automatic routing is ceilinged at **Sol**. Given
a live `/v1/models` set, a chosen tier steps *down* to the nearest available
tier; the upward walk (nothing at or below available) can never select Astra
without explicit authorization — with no allowed model the request is left on
its configured model, and an *unauthorized Astra request* that cannot be
downgraded is **refused with a typed response** at the execution seam instead
of being sent. `make route-smoke` probes both proxies and proves every tier
resolves to a live model.

## Reasoning effort

`config.yaml` sets `agent.reasoning_effort: high` as the per-request
**ceiling**. With the plugin's `effort_routing` on (default), managed
orchestrator (GPT) requests get their effort rewritten per task — low for
status questions, medium for ordinary/planning work, high only for genuinely
hard tasks or authorized max — never above the ceiling, and both transport
shapes are kept consistent (Chat Completions `reasoning_effort` /
`extra_body.reasoning.effort`, Responses `reasoning.effort`), so no stale
conflicting `high` rides along. Fields the request does not carry are never
invented, and explicitly disabled reasoning is never re-enabled.

**Coding requests are exempt:** claude-max-proxy reads `reasoning_effort` and
owns the Claude CLI effort mapping; the plugin only ever selects `opus` vs
`fable` there. **Auxiliary** title/compression calls keep their per-entry
`reasoning_effort` from native config.

Model selection (which model) and reasoning effort (how hard it thinks) are
decided independently by the same task classification.

## Observability

Every managed routing decision is logged — including unchanged ones — as a
single `model_router: route …` line carrying only opaque correlation ids
(hashes of the session id and current-task text, plus the core
`api_request_id`), the family, tier, authorization bit, `model old->new`,
`effort old->new`, stable reason labels and an outcome
(`rewritten` / `unchanged` / `no-candidate` / `refuse-scheduled`). Prompt
content, secrets and private values never appear in logs.

At the execution seam, managed requests also get a `model_router: outcome …`
line distinguishing **requested** (what the middleware selected) from
**served** — reported only from actual provider evidence (the response's
`model` attribute) — with `status=completed|failed|stream-opened`, token usage
when the response exposes it, and `usage=unavailable` otherwise. `completed`
is only claimed for a materialized response; when the transport hands back a
lazy stream (generator, async iterator, SDK stream wrapper) the seam logs
`status=stream-opened … completion=unobserved` instead, and the stream is
returned untouched — never advanced or consumed for logging. (In the installed
core this path is defensive: `agent/chat_completion_helpers.py` materializes
streaming responses below the middleware seam.) Failures log the exception
*type* only and re-raise the original exception unchanged.

Known limitations, stated rather than invented: the middleware sees each
provider *attempt* (native fallback hops re-enter it with their own
`api_request_id`/`base_url`), but Hermes core does not expose the relay's
`call_role` to middleware, so fallback attempts are linked only via
`turn_id`/`api_call_count`; and for streaming responses no served-model or
usage evidence exists at this seam, so none is logged. View with
`make router-logs`.

## Fallback chains (native Hermes config)

For **normal, non-private** traffic only. On a 429/503/connection failure Hermes
walks the chain. Each entry is a distinct endpoint, so none duplicates the
primary provider:

- Orchestrator: CLIProxyAPI → claude-max-proxy → Ollama (`fallback_providers`)
- Delegation: claude-max-proxy → Ollama (`delegation.fallback_providers`)

Private traffic never reaches these chains: the privacy guard short-circuits
before any provider call.

## Privacy routing (`llm_execution` middleware) — fail-closed

Privacy routing is **enabled by default** (`MODEL_ROUTER_PRIVACY` defaults to
`true` and renders into `config.yaml`). Setting `MODEL_ROUTER_PRIVACY=false` is a
deliberate, **risky opt-out** that disables the guard entirely; when enabled it
stays fail-closed as described here.

Classification and local-routing errors remain fail-closed. Exceptions from a
permitted downstream provider call propagate unchanged to normal provider error
handling; they are not evidence of private content or an unavailable local model.
A `router-guard` refusal identifies an internal privacy-path failure, not a
provider timeout. Repository changes require installation and a gateway restart
to update an already-running plugin instance.

The guard inspects the **entire** outbound request — system prompt, every
message, and tool results — for private signals:

- explicit `#private` / `#local` tags
- US SSN (area/group/serial validated)
- Luhn-valid PAN (13–19 digits)
- ABA routing number (checksum)
- IBAN (mod-97)
- private keys, provider secrets (`AKIA…`, `sk-…`, `ghp_…`, Slack, Google…),
  bearer tokens, generic `key = secret`
- credential/login pairs and creds-in-URL
- operator-configured `private_terms` and `private_paths`

When a request is private it is **never sent to a cloud provider**. It is
answered by the local Ollama model, or — if local inference is unavailable —
**refused**. Once a session is flagged private it stays local for the rest of
the session. A classifier error is treated as private.

### Why this is fail-closed

Hermes middleware is *fail-open*: if a middleware **raises**, Hermes logs a
warning and continues to the base cloud path. The guard therefore **never
raises** — every path returns a local result or a refusal object, and the cloud
callback (`next_call`) is invoked only for traffic proven public. This was
verified end-to-end through Hermes' own `run_llm_execution_middleware` dispatch:
a synthetic-SSN request produced **zero** cloud calls and a refusal; a benign
request reached the cloud. See `make test-router` (the `TestPrivacyCanary`
cases assert zero bytes to `:8317` and `:3456`).

> No real secrets are used in any test. All sensitive-looking fixtures are
> synthetic values chosen to satisfy the deterministic checksums.

## Auxiliary work (title generation, compression)

Cheap, high-volume helper calls (`auxiliary.title_generation` /
`auxiliary.compression`), rendered from `AUX_PROVIDER` / `AUX_MODEL` /
`AUX_BASE_URL`. Behavior on **Hermes 0.21.2**:

| `AUX_PROVIDER` | `AUX_BASE_URL` | Result |
|----------------|----------------|--------|
| `auto` (default) | empty | Use the **main provider + model**, following the top-level `fallback_providers` policy. Install never breaks when Ollama is absent. |
| any value | **non-empty** | Route to that endpoint; the **provider field is ignored**. Pair with `AUX_MODEL`. |
| `custom` | empty | Custom provider with no endpoint override — only useful if a provider default endpoint applies; prefer setting `AUX_BASE_URL`. |

`AUX_PROVIDER=auto` therefore has one meaningful behavior — inherit the main
provider/model — and any custom routing is driven by `AUX_BASE_URL`, not by the
provider name. To keep title/compression work off the cloud, set the base_url to
a local chat model in `.env`:

```
AUX_MODEL=llama3.1:8b
AUX_BASE_URL=http://127.0.0.1:11434/v1
```

Re-run `make hermes-install` to re-render the `auxiliary.*` entries.

### Auxiliary pre-egress guard — fail-closed

Auxiliary calls run **outside** the main-loop `llm_execution` middleware, so the
model-router privacy guard above never inspects them. Hermes core therefore
applies an equivalent pre-egress guard on the auxiliary path
(`agent/aux_egress_guard.py`). Two independent, fail-closed checks decide whether
a failed auxiliary request may reach a **cloud** provider:

1. **Local-endpoint pin — secure default-deny.** When an aux task is pinned to a
   LOCAL endpoint (loopback / RFC-1918 / RFC-6598 CGNAT such as Tailscale /
   `*.local`) and that endpoint **times out or is unreachable**, the request is
   **refused/skipped** rather than spilled to the main cloud provider. Prior to
   this guard a local Ollama outage silently fell back to the cloud, defeating
   the whole point of pinning a local endpoint. Opt back in with
   `auxiliary.allow_cloud_fallback: true` (native config; default **false**).
   Non-outage failures on a local endpoint (a rejected model, an operator's
   explicit `auxiliary.<task>.fallback_chain` hop) keep the documented fallback.

2. **Private payload — never to cloud.** A payload the deterministic scanner
   flags as private (`#private` / `#local` tags, secrets/credentials, and the
   operator `MODEL_ROUTER_PRIVATE_TERMS` / `MODEL_ROUTER_PRIVATE_PATHS` signals)
   is **never** sent to a cloud provider — on the primary call or on fallback —
   regardless of `allow_cloud_fallback`. This guard is always on for the
   auxiliary path and does not depend on `MODEL_ROUTER_PRIVACY`.

Both checks fail closed: an unreadable config or a scanner error denies cloud
egress. `AUX_PROVIDER=auto` with **no** local `AUX_BASE_URL` keeps its documented
behavior — public traffic still follows the main provider + fallback policy.

> **`MODEL_ROUTER_PRIVACY=false` is unsafe.** It disables the main-loop privacy
> guard entirely, so private data in ordinary (non-auxiliary) turns can reach the
> cloud. The auxiliary pre-egress guard above still protects auxiliary calls, but
> it is **not** a substitute for main-loop privacy routing. Leave
> `MODEL_ROUTER_PRIVACY=true` in production.

> **Follow-up (not yet implemented):** interactive per-call confirmation for a
> pinned-local outage. It is intentionally omitted here because it cannot fail
> closed in gateway/headless contexts and must not transmit raw private payload
> without a separate explicit opt-in. The default-deny policy above is the safe
> baseline until a fail-closed confirmation channel exists.

## Configuration

`config.yaml` is rendered from `hermes/config.yaml.tmpl` by `make hermes-install`.
The plugin is installed to `$HERMES_HOME/plugins/model-router/` and enabled via
`plugins.enabled`. Plugin settings (`plugins.entries.model-router.settings`)
and their `.env` fallbacks:

| Setting | `.env` fallback | Purpose |
|---------|-----------------|---------|
| `complexity_routing` | `MODEL_ROUTER_COMPLEXITY` | enable the task-scoped model rewrite (default **true**) |
| `privacy_routing` | `MODEL_ROUTER_PRIVACY` | the fail-closed guard, **enabled by default**; `MODEL_ROUTER_PRIVACY=false` is a deliberate, risky opt-out that disables it entirely |
| `effort_routing` | `MODEL_ROUTER_EFFORT` | per-task reasoning-effort rewrite for managed GPT requests (default **true**; never above the config ceiling) |
| `astra_policy` | `MODEL_ROUTER_ASTRA` | `task` (default: leading standalone `#max`/`#astra` line authorizes), `always` (legacy allow), `never` |
| `task_ttl_seconds` | `MODEL_ROUTER_TASK_TTL` | sliding TTL for per-session task state / authorization (default 1800) |
| `ollama_base_url` | `OLLAMA_BASE_URL` | host-reachable local endpoint |
| `ollama_model` | `OLLAMA_CHAT_MODEL` | a pulled **chat** model |
| `private_terms` | `MODEL_ROUTER_PRIVATE_TERMS` | extra local-only terms |
| `private_paths` | `MODEL_ROUTER_PRIVATE_PATHS` | extra local-only path fragments |

All knobs are backwards-compatible: a pre-upgrade config without the new keys
gets the balanced defaults (effort rewrite on, Astra opt-in per task), and
explicit `false`/`never` values render and load verbatim.

## Readiness — when is private routing *live*?

Private routing is **fully active only** when both hold:

1. **Ollama is reachable from the host** where Hermes runs. In `docker-compose`
   Ollama is loopback-only *inside* Docker (`11434/tcp`, not published). Publish
   it to the host loopback (or run a host Ollama) and set `OLLAMA_BASE_URL`.
2. **A chat model is pulled** (the compose Ollama only pulls the *embedding*
   model for Honcho). e.g. `ollama pull llama3.1:8b`, then set `OLLAMA_CHAT_MODEL`.

Until both hold, the guard still protects you: private requests are **refused**
(never sent to the cloud), proven by `make test-router`. `make route-smoke`
confirms tier resolution against the live proxies.

## Honcho and private sessions — known limitation

Hermes 0.21.2 has **no per-session "private/ephemeral" flag**: the only Honcho
write gates are global (`save_messages`) or launch-time (`skip_memory`). So a
plugin cannot, per-turn, stop Honcho from persisting a transcript or deriving
cloud representations of it. The privacy guard prevents the *orchestrator's*
cloud LLM calls from leaking private data, but Honcho isolation for a single
private turn is **not** yet active.

Proven fail-closed options until a core per-session hook exists:

- Run private work under a Hermes **profile with memory disabled**
  (`memory.provider` unset / `memory_enabled: false`), or
- Point every Honcho reasoning model (`HONCHO_MODEL`) at a local endpoint so no
  transcript-derived reasoning leaves the host, or
- Globally set `save_messages: false` for private operating periods.

This is tracked as a follow-up: add a memory-provider `should_persist(session)`
gate the plugin can drive. **Do not treat private Honcho isolation as active
until that lands.**

## Verify

```bash
make route-smoke     # live: probe /v1/models, degrade-check tiers, run the canary
make test-router     # offline: unit + zero-egress privacy canary
make models          # what each proxy exposes
make router-logs     # last 20 model_router lines from ~/.hermes/logs/agent.log (LINES=<n>)
make router-logs-follow  # same, then follow live (survives log rotation)
```
