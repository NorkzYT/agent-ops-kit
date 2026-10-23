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
| Local / private / aux | Ollama (OpenAI-compatible) | `:11434/v1` (or a Tailscale host) | routine `qwen3.5:27b`, fallback `qwen3.6:27b`, agent `qwen3.8:27b` ([Local model roles](#local-model-roles)) |

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

Only the **first non-empty line** of the sender-authored task is a control line.
Precedence when that line is exactly one tag: coding force (`#opus`/`#fable`)
→ max authorization (`#max`/`#astra`) → routine force (`#quick`/…).

- A **leading standalone** `#opus` or `#fable` line **forces the Claude coding
  model** for that request, overriding complexity (see *Forced coding* below).
- A **leading standalone** `#max` or `#astra` line (the first non-empty line of
  the task, containing only the tag) authorizes `gpt-6-astra` for that task.
- Inline `#max`, `#deep`, `#hard` are hard-work *hints* (Sol at most) — they no
  longer grant Astra, and they are ignored entirely inside quotes/code fences,
  tool output, or older turns. Inline/quoted `#opus`/`#fable` are inert (not even
  a hint).
- `#quick` / `#routine` / `#cheap` / `#simple` / `#low` force the routine tier.

**Forced coding (`#opus` / `#fable`).** A leading standalone `#opus`/`#fable`
line forces that exact coding model regardless of task shape, for **that request
only** (no task state or authorization is stored, so it never bleeds into the
next task). The control names the model only: reasoning effort still follows
the task body ([Reasoning effort](#reasoning-effort)) and never reaches `max`
(that needs `#max`/`#astra`, which cannot share the control line). The
control is resolved through the provenance-safe extractor, so a tag in memory,
backfill, a reply quote, an attachment, a quote/fence, tool output or an older
turn grants nothing; only a genuine leading tag in the current turn counts.

Because the `llm_request` middleware can rewrite the request **body** but cannot
switch the bound provider/`base_url` (verified against the installed core —
`agent/turn_api_request.py`, `agent/turn_api_call.py`: the client is
`agent.client`, fixed before middleware), the force is honored only on a
**coding-family seam** (claude-max-proxy): the model is rewritten to
`opus`/`fable`, degrading to nothing (kept as configured, normal error/fallback)
if a live `/v1/models` probe proves it absent. On the **orchestrator/GPT seam**
the call cannot be redirected to the coding provider, so a forced request is
**refused with a typed response** at the execution seam rather than sent to the
wrong provider or faked with a GPT alias — delegate the task (delegation routes
to the coding proxy) or drop the control. **Privacy stays the higher priority:**
the fail-closed privacy guard runs first, so a private forced request goes local
or is refused and never reaches the cloud coding proxy.

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

**Coding requests** (claude-max-proxy) get a task-based Claude effort with
`coding_effort_routing` on (default), from the same current-task
classification and independent of the model choice (`opus`/`fable`):

| Current coding task | Effort | Proxy thinking budget |
|---------------------|--------|-----------------------|
| routine / ordinary code (status, small fixes, single-keyword or planning work) | `medium` | 10k |
| complex code (Sol-tier: two hard categories, or one plus strong signals) | `high` | 32k |
| unusually hard debugging/architecture: hard tier with strong signals, naming `debug`/`arch`/`concurrency` **and** a second hard category or an explicit `#deep`/`#hard` | `xhigh` | 48k |
| leading standalone `#max`/`#astra` line (authorized, same TTL rules as Astra) | `max` | 64k |

The coding effort is capped by `coding_effort_ceiling` (default `max`), not
by `agent.reasoning_effort` — that orchestrator ceiling (`high`) would rule
out `xhigh`. Every effort field the request carries is rewritten (Hermes'
custom provider sends top-level `reasoning_effort`, which the proxy reads);
a coding request with no reasoning field gets a top-level `reasoning_effort`
so the proxy's `DEFAULT_THINKING_BUDGET` is not silently used instead;
explicitly disabled reasoning (`none`, `enabled: false`) is never re-enabled.
Only managed coding aliases (`opus`/`fable`) are touched: GPT/orchestrator
effort, the private path (local answers always send `reasoning_effort:
"none"`), and Ollama fallback hops (a non-managed model id) are unchanged.
`MODEL_ROUTER_CODING_EFFORT=false` restores the previous behavior (configured
effort passes through untouched). **Auxiliary** title/compression calls keep their per-entry
`reasoning_effort` from native config (compression never runs at `high`; see
[Auxiliary work](#auxiliary-work-title-generation-compression)).

Model selection (which model) and reasoning effort (how hard it thinks) are
decided independently by the same task classification.

## Observability

Every managed routing decision is logged — including unchanged ones — as a
single `model_router: route …` line carrying only opaque correlation ids
(hashes of the session id and current-task text, plus the core
`api_request_id`), the family, tier, authorization bit, `model old->new`,
`effort old->new` (coding requests too, e.g. `family=coding … effort=high->xhigh`;
`unset->medium` when the request carried no field, `off` when reasoning was
disabled), stable reason labels and an outcome
(`rewritten` / `unchanged` / `no-candidate` / `refuse-scheduled` /
`forced-coding` / `forced-coding-unroutable`). A forced-coding request carries
the `force:opus` / `force:fable` reason label and is identified on both seams.
Prompt content, secrets and private values never appear in logs.

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
`make router-logs`. The coding proxy confirms what it applied in its own
`request.start` line (`reasoningSource`, `reasoningEffort`,
`reasoningBudget`): `make claude-proxy-logs`.

## Local model roles

Three explicit roles on the one local endpoint (`OLLAMA_BASE_URL`), rendered
into the plugin settings and the native chains. No role relies on
`provider: auto`.

| Role | Default | `.env` | Plugin setting | Used by |
|------|---------|--------|----------------|---------|
| routine | `qwen3.5:27b` | `OLLAMA_ROUTINE_MODEL` | `ollama_routine_model` | ordinary private turns, title generation (`AUX_MODEL` default; compression too with `AUX_COMPRESSION_ROUTE=aux`), specialist classifier |
| fallback | `qwen3.6:27b` | `OLLAMA_FALLBACK_MODEL` | `ollama_fallback_model` | one retry after a role-model failure; last hop of both native chains; aux `fallback_chain` |
| agent | `qwen3.8:27b` | `OLLAMA_AGENT_MODEL` | `ollama_agent_model` | difficult private turns (high/max tier), private delegated coding, `#opus`/`#fable` when private, the native fallback agent |

`OLLAMA_CHAT_MODEL` is the legacy single-model knob. When it is set, it is
the default for routine and agent, there is no default fallback, and the
installer warns. That way an upgraded single-model install keeps using the
model it has pulled. Set the three `OLLAMA_*_MODEL` roles to get the hierarchy.

Cloud stays primary for normal work: the orchestrator on CLIProxyAPI
(luna/terra/sol), coding on claude-max-proxy (opus/fable). The local roles
answer only `#privacy` turns, auxiliary calls pinned to Ollama, the specialist
classifier, and the native fallback tail after both cloud providers failed.

**Private traffic (plugin seam).** Hermes' native `fallback_providers` cannot
pick a model by task difficulty, and private traffic must never enter it, so
the selection lives in the plugin's `llm_execution` privacy path:

1. Role from the current task only: `agent` for a coding-family request
   (claude-max-proxy seam), a forced coding control, or a `high`/`max` tier;
   `routine` otherwise.
2. The role model answers. On a model-level failure (HTTP error such as a
   404 for a missing model, a timeout, an unreadable response) the fallback
   model is tried **once**. A connection failure stops at once: the fallback
   lives on the same endpoint.
3. Still nothing: a typed refusal. Never `next_call`, never a cloud provider.
4. An `ollama_base_url` that is not local (loopback, RFC-1918, CGNAT/Tailscale,
   link-local, `localhost`, a single-label host or `*.local`/`*.lan`/
   `*.internal`/`*.home.arpa`/`*.ts.net`) is refused unsent.

Log line: `model_router: private->local ok reason=<labels> role=routine model=qwen3.5:27b attempt=1/2`.
With only the legacy `ollama_model` set every role is that one model and
there is no fallback (the pre-roles behavior).

**Limits.**

- The private path is answer-only: the local model gets the messages, not the
  tools, so a private turn cannot run tools. Tool-using local agent work is the
  native fallback tail (non-private only).
- Private turns are sent with top-level `reasoning_effort: "none"` (Ollama
  0.34 `/v1` honours it; a top-level `think: false` is ignored there) and a
  bounded `max_tokens`: 2048, reduced to fit what the history leaves of
  `ollama_context_tokens` (default 65536, the served num_ctx), never below
  512. Without them qwen3.5:27b spends the turn on `message.reasoning` and an
  empty `content` is refused. Reasoning text is never used as the answer.
- The private chain shares one budget: `ollama_timeout` (60 s; model load and
  queueing) + estimated prefill at 150 tok/s + `max_tokens` at 10 tok/s,
  capped at 600 s (`budget=` in the `private->local ok` log line). A ~23k-token
  history took 58 s on the host, so a flat 60 s timed out long sessions.
- Ollama on the current host keeps one 27b model resident (`ollama ps`).
  Moving between roles reloads the model, about 20-25 s. The classifier has a
  10 s budget, so its first call after an agent turn usually times out and adds
  nothing.
- The native chains walk agent → fallback in a fixed order. The routine
  model is not in them, because a fallback hop has to run full agent turns.

## Fallback chains (native Hermes config)

For **normal, non-private** traffic only. Hermes walks the chain on a 429,
on a 503/timeout/connection failure (after its retries), and immediately on a
404 model-not-found. No entry duplicates the primary:

- Orchestrator: CLIProxyAPI → claude-max-proxy (`opus`) → Ollama agent
  (`qwen3.8:27b`) → Ollama fallback (`qwen3.6:27b`) (`fallback_providers`)
- Delegation: claude-max-proxy → Ollama agent → Ollama fallback
  (`delegation.fallback_providers`)

Hermes 0.21.2 keeps two entries on one `base_url` when their models differ.
It drops only an exact (provider, model, base_url) duplicate, and skips an
entry whose model matches the backend that just failed. So both local hops are
real, and `OLLAMA_FALLBACK_MODEL` equal to the agent model collapses to one
hop. `make local-route-status` prints the chains as Hermes resolves them.

Private traffic never reaches these chains: the privacy guard short-circuits
before any provider call.

## Privacy routing (`llm_execution` middleware) — fail-closed

Privacy routing is **enabled by default** (`MODEL_ROUTER_PRIVACY` defaults to
`true` and renders into `config.yaml`). Setting `MODEL_ROUTER_PRIVACY=false` is a
deliberate, **risky opt-out** that disables the guard entirely; when enabled it
stays fail-closed as described here.

**Current `.env.example`: opt-out.** The shipped `.env.example` sets
`MODEL_ROUTER_PRIVACY=false`, and an explicit `false` renders verbatim. With it:

- a `#privacy`/`#private` turn follows the normal cloud route, and earlier
  marked turns are not withheld from later requests;
- the installer warns (`MODEL_ROUTER_PRIVACY=false: private data is NOT kept
  local`), and `make local-route-status` / `local-route-readback` flag it;
- still enforced, because they do not read this setting: the
  [auxiliary guard](#auxiliary-pre-egress-guard--fail-closed) refuses a
  compression payload carrying `#privacy`/`#private`, and the specialist router
  never sends a marked turn to its classifier or to a cloud-backed profile.

Set `MODEL_ROUTER_PRIVACY=true` and [apply it](install.md#change-a-setting)
for the per-request local-or-refuse behavior below.

Classification and local-routing errors remain fail-closed. Exceptions from a
permitted downstream provider call propagate unchanged to normal provider error
handling; they are not evidence of private content or an unavailable local model.
A `router-guard` refusal identifies an internal privacy-path failure, not a
provider timeout. Repository changes require installation and a gateway restart
to update an already-running plugin instance.

### The `#privacy` marker — the only signal

A request is private **only** when the current user turn carries a standalone
`#privacy` token that the sender typed (anywhere in the text, any case:
`#privacy`, `#PRIVACY`, `(#privacy)`; not `#privacy-policy` or `foo#privacy`).
Typing `#privacy` on its own first line still lets a `#max`/`#astra`/`#opus`/
`#fable`/`#quick` control line follow it.

A marked request is **never sent to a cloud provider**: the local Ollama model
answers it, or, when local inference is unavailable, it is **refused**. That
covers every model call of the turn and a Hermes fallback restart of the same
turn. A check error is treated as private.

Nothing else makes a request private:

- **Not sticky.** Privacy is decided per request from the current turn. The
  next turn without `#privacy` is public and goes to the normal (cloud) route,
  in the same session.
- **No content classification.** ID numbers, card/bank numbers, IBANs,
  secrets, credentials, private keys, `private_terms` and `private_paths` do
  not route anything. Type `#privacy` when a turn must stay local.
- **`#private` is equivalent to `#privacy`** (same provenance rules, same
  fail-closed local-or-refuse path); `#local` stays inert.

Only the sender's own text counts. These never activate the marker: system or
developer prompts, assistant turns, tool results (including Responses function
outputs and tool-result parts), earlier user turns, appended memory
(`<memory-context>`, `--- Attached Context ---`), channel backfill before
`[New message]`, reply quotes and other prepended gateway blocks, Hermes
runtime envelopes (delegation results, Kanban wakes, process notices), and
`>`-quoted or fenced text. In an inline attachment relay only the typed prose
counts; an upload sent with no typed text is the prompt, so its `#privacy`
counts, and when the relay boundary cannot be proven the whole sender segment
is checked (both fail toward local).

**Earlier marked turns stay off the cloud.** On a public request the guard
withholds every earlier marked exchange (the marked user turn and every item up
to the next user turn: the local answer, tool calls and results) before it is
sent: text becomes `[withheld: this earlier turn was marked private and
answered locally]`, tool-call arguments become `{}`, reasoning items and extra
fields are dropped, and roles plus tool-call ids/names are kept so provider
pairing rules hold. The current turn and its own tool loop are untouched. A
withholding error keeps the request local. Log line: `public request; withheld
N private-history item(s)`.

Limits (see [Honcho and private sessions](#honcho-and-private-sessions--known-limitation)):

- Only the main-loop LLM call is guarded. Memory providers (Honcho) persist
  and reason over the transcript on their own route, so derived memories of a
  marked turn can come back in recalled context.
- Title generation and compression are guarded by Hermes core's auxiliary
  guard, which this plugin extends: CLIProxyAPI counts as cloud and a
  `#privacy`/`#private` anywhere in the payload is refused (see
  [Auxiliary pre-egress guard](#auxiliary-pre-egress-guard--fail-closed)). A
  session that held a marked turn is therefore not compressed on CLIProxyAPI.
- A `#privacy` sent as a mid-turn steer inside a tool result does not move an
  already-public turn local.

### Why this is fail-closed

Hermes middleware is *fail-open*: if a middleware **raises**, Hermes logs a
warning and continues to the base cloud path. The guard therefore **never
raises** — every path returns a local result or a refusal object, and the cloud
callback (`next_call`) is invoked only for traffic proven public. This was
verified end-to-end through Hermes' own `run_llm_execution_middleware` dispatch:
a `#privacy` request produced **zero** cloud calls and a refusal; a benign
request reached the cloud, and the next unmarked turn reached it without the
marked turn's content. See `make test-router` (the `TestPrivacyCanary`
cases assert zero bytes to `:8317` and `:3456`).

A refusal is a **final answer** (`finish_reason: stop`, typed by
`_model_router_refusal` / `_model_router_refusal_reason`), never
`content_filter`: Hermes answers an HTTP-200 `content_filter` with
`handle_content_policy_refusal`, which activates the configured fallback chain
(`gpt-5.6-luna -> opus`), so a refused private turn would restart on a cloud
provider. An empty local answer is likewise a local failure (next local model,
then refuse), because Hermes' empty-response ladder also ends in fallback.
`TestHermesCoreSeam` runs the refusal through the installed Hermes middleware
chain and response checks.

> No real secrets are used in any test. All sensitive-looking fixtures are
> synthetic values chosen to satisfy the deterministic checksums.

## Auxiliary work (title generation, compression)

Cheap, high-volume helper calls (`auxiliary.title_generation` /
`auxiliary.compression`). Current defaults:

- **Title generation** uses the main route: CLIProxyAPI with the orchestrator
  model (`HERMES_MODEL`, `gpt-5.6-luna`). `.env.example` sets no `AUX_*` title
  variables, so the installer renders `provider: auto` with
  `reasoning_effort: low`. `make local-route-readback` accepts this route only
  while the main model points at the configured CLIProxyAPI.
- **Compression** uses CLIProxyAPI `gpt-5.6-terra`, then `gpt-5.6-luna`
  ([below](#compression-on-cliproxyapi-default)).

The installer still reads `AUX_BASE_URL` / `AUX_MODEL` / `AUX_FALLBACK_MODEL` /
`AUX_PROVIDER` as an optional override that moves title generation to a local
endpoint. Behavior on **Hermes 0.21.2**
(`agent/auxiliary_client.py:_resolve_task_provider_model`):

| `.env` | Rendered | Hermes resolves |
|--------|----------|-----------------|
| no `AUX_BASE_URL` (default) | `provider: auto`, empty model/base_url, `fallback_chain: []` | the **main provider + model**, following the top-level fallback policy. Install never breaks when Ollama is absent. |
| `AUX_BASE_URL` set | `provider: custom`, `model: AUX_MODEL` (default: routine role), `fallback_chain: [{provider: custom, model: AUX_FALLBACK_MODEL (default: fallback role), base_url: AUX_BASE_URL}]` | that local endpoint, then the fallback model on it |

`provider: auto` next to a base_url **without an api_key** resolves back to
`auto`, so Hermes drops the base_url and calls the main (cloud) provider with
the local model name. Ollama needs no key, so that layout silently leaves the
aux calls on the cloud. The installer therefore renders `custom` whenever
`AUX_BASE_URL` is set. `make local-route-readback` fails on the old layout. To keep
title generation off the cloud (optional; not the shipped default):

```
AUX_BASE_URL=http://127.0.0.1:11434/v1
# AUX_MODEL=qwen3.5:27b          default: OLLAMA_ROUTINE_MODEL
# AUX_FALLBACK_MODEL=qwen3.6:27b default: OLLAMA_FALLBACK_MODEL
```

### Compression on CLIProxyAPI (default)

`AUX_COMPRESSION_ROUTE=cliproxy` (the default) renders compression on
CLIProxyAPI, whatever `AUX_BASE_URL` says:

```yaml
compression:
  provider: custom
  model: gpt-5.6-terra                    # AUX_COMPRESSION_MODEL
  base_url: http://127.0.0.1:8317/v1      # CLIPROXY_PORT
  api_key: ${CLIPROXY_API_KEY}
  reasoning_effort: low
  extra_body: {}
  timeout: 120
  fallback_chain: [{provider: custom, model: "gpt-5.6-luna", base_url: "http://127.0.0.1:8317/v1", key_env: CLIPROXY_API_KEY}]
```

Terra is the routing policy's model for work that needs judgment (a summary
must keep decisions, errors and file paths); luna, the everyday model, is the
fallback. Hermes moves to luna when terra hits a usage/rate limit (429,
verified) or returns an invalid response, and the compression stall retry
pins it too. A 404 is not a fallback reason, and Hermes scopes a payment error
to the whole proxy endpoint, so neither reaches luna. Wait budgets are the
cloud ones (120 s / 600 s). `AUX_COMPRESSION_FALLBACK_MODEL=none` drops the
fallback.

| `.env` | Default | Effect |
|--------|---------|--------|
| `AUX_COMPRESSION_ROUTE` | `cliproxy` | `aux` follows the title-generation route (`AUX_BASE_URL` local Ollama, else the main provider) |
| `AUX_COMPRESSION_MODEL` | `gpt-5.6-terra` | primary model on CLIProxyAPI |
| `AUX_COMPRESSION_FALLBACK_MODEL` | `gpt-5.6-luna` | same-proxy fallback; `none` = no fallback |

Private payloads never take this route: the model-router plugin's guard
extension (below) refuses a payload carrying `#privacy`/`#private` before
anything is sent. Verified by `test_cliproxy_compression_wire_and_private_refusal`
and `test_cliproxy_compression_falls_back_to_luna` (Hermes' own `call_llm`
against a stub proxy).

**Compression on a local route** (`AUX_COMPRESSION_ROUTE=aux` with
`AUX_BASE_URL` set) renders thinking off and prefill-sized
budgets. Measured on the Tailscale Ollama (qwen3.5:27b, 64k context): prefill
~200 tok/s, decode ~12 tok/s, so the ~52.6k-token summary prompt Hermes builds
(its input is capped at 160k chars; there is no aux-side chunking) streams
nothing for 4+ minutes and the default 120 s no-progress window always fires.

| Key | Local (`aux` + `AUX_BASE_URL`) | Cloud (CLIProxyAPI or inherited) |
|-----|--------------------------------|----------------------------------|
| `auxiliary.compression.reasoning_effort` | `""` | `low` |
| `auxiliary.compression.extra_body` | `{reasoning_effort: none}` | `{}` |
| `auxiliary.compression.timeout` | `600` | `120` |
| `compression.context_timeout_seconds` | `420` | `120` |
| `compression.context_total_ceiling_seconds` | `900` | `600` |

Ollama's `/v1` honors only a top-level `reasoning_effort`; the `reasoning`
object Hermes emits for the `reasoning_effort` key is ignored for thinking and
overrides the top-level field, so the local route disables thinking through
`extra_body` alone (verified on the wire by `test_local_compression_wire_disables_thinking`).

Re-run the installer (below) to re-render the `auxiliary.*` entries. With an
explicit provider Hermes still falls back to the main agent model after a
non-outage error (for example both local models rejected). The guard below
blocks that for an outage and for private payloads.

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

2. **Private payload — never to cloud.** A payload the core scanner flags as
   private (`#private` / `#local` tags, secrets/credentials, and the operator
   `MODEL_ROUTER_PRIVATE_TERMS` / `MODEL_ROUTER_PRIVATE_PATHS` signals) is
   **never** sent to a cloud provider — on the primary call or on fallback —
   regardless of `allow_cloud_fallback`. This guard is always on for the
   auxiliary path and does not depend on `MODEL_ROUTER_PRIVACY`. It lives in
   Hermes core and still matches content; `#privacy` comes from the plugin
   extension below.

Both checks fail closed: an unreadable config or a scanner error denies cloud
egress. `AUX_PROVIDER=auto` with **no** local `AUX_BASE_URL` keeps its documented
behavior — public traffic still follows the main provider + fallback policy.

**Plugin extension (`aux_guard.py`).** Core's guard has two gaps for this
stack: it treats every loopback URL as local, and a local endpoint skips the
privacy scan — but CLIProxyAPI (`:8317`) and claude-max-proxy (`:3456`) listen
on loopback and answer from cloud models; and its scanner does not know
`#privacy`. When the model-router plugin loads, it wraps both checks in the
loaded core module:

- a CLIProxyAPI / claude-max-proxy URL (by port, proxy host name, or the
  configured `CLIPROXY_BASE_URL` / `CLAUDE_MAX_PROXY_BASE_URL`) is never local,
  so its payloads are scanned;
- a standalone `#privacy` / `#private` token **anywhere** in the payload is
  private (`privacy-marker`). Compression input is a serialized transcript, so
  the sender provenance the request guard relies on is gone; any occurrence
  counts, including a quoted or discussed marker.

Errors fail closed (a locality error reads "not local", a scan error reads
private). The refusal is core's `AuxCloudEgressRefused`; it covers the primary
call, the main-model retry and the stall retry, which all go through
`call_llm`. Log line at load: `model_router: aux privacy guard installed`.
If core's guard module is missing the plugin logs `aux privacy guard
unavailable` at ERROR; pin compression locally (`AUX_COMPRESSION_ROUTE=aux`)
until it is back. `test_core_guard_alone_leaks_loopback_cloud_proxy` keeps the
reason on record: without the extension a `#privacy` payload reaches the proxy.

**Limitations.**

- A refused compression is a summary failure to Hermes: no request is sent,
  the summary cooldown is armed, and Hermes compacts with its deterministic
  fallback summary instead of an LLM one. There is no local retry for that
  call; set `AUX_COMPRESSION_ROUTE=aux` with a local `AUX_BASE_URL` if private
  sessions must get LLM summaries.
- The deterministic fallback summary quotes excerpts of the compacted turns,
  including a marked turn's text (verified with a real `ContextCompressor`).
  The request guard withholds only earlier *marked user turns*, not a
  compaction summary, so a later public turn in that session can carry those
  excerpts to the cloud. Summaries from the `aux` route (Ollama) have the same
  gap in paraphrased form. Until the request guard also withholds marked
  summaries, start a fresh session (`/new`) after private work in a long one.
- The extension depends on the plugin being loaded in the process that
  compresses (`make local-route-readback` fails when `model-router` is not in
  `plugins.enabled`).

> **`MODEL_ROUTER_PRIVACY=false` is unsafe.** It disables the main-loop privacy
> guard entirely, so a `#privacy` turn (and its later history) can reach the
> cloud. The auxiliary pre-egress guard above still protects auxiliary calls, but
> it is **not** a substitute for main-loop privacy routing. Leave
> `MODEL_ROUTER_PRIVACY=true` in production.

> **Follow-up (not yet implemented):** interactive per-call confirmation for a
> pinned-local outage. It is intentionally omitted here because it cannot fail
> closed in gateway/headless contexts and must not transmit raw private payload
> without a separate explicit opt-in. The default-deny policy above is the safe
> baseline until a fail-closed confirmation channel exists.

## Specialist routing (pilot)

The same plugin can suggest book-backed specialist profiles per task through a
`pre_llm_call` hook. The plugin default is off (`specialist_routing`); the shipped
`.env.example` turns it on with `MODEL_ROUTER_SPECIALIST_MAX=6` (the hard cap)
and `MODEL_ROUTER_SPECIALIST_CLASSIFIER=cliproxy`. It uses the same
current-task extraction and `#privacy`/`#private` marker check, and only runs in
the `default` profile. Typed keywords and verified attachment text (PDF/DOCX
extracted locally) add deterministic signals. For a public task with weak or no
keyword matches, or with verified attachments, the hybrid classifier (CLIProxyAPI
`gpt-5.6-terra` at `medium` effort, or `local`, the routine role below) may add
at most two non-high-risk profiles. The cap applies once, to the combined list.
Marked turns never reach either classifier. See
[specialist-routing.md](specialist-routing.md).

## Configuration

`config.yaml` is rendered from `hermes/config.yaml.tmpl` by `make hermes-install`.
The plugin is installed to `$HERMES_HOME/plugins/model-router/` and enabled via
`plugins.enabled`. Plugin settings (`plugins.entries.model-router.settings`)
and their `.env` fallbacks:

| Setting | `.env` fallback | Purpose |
|---------|-----------------|---------|
| `complexity_routing` | `MODEL_ROUTER_COMPLEXITY` | enable the task-scoped model rewrite (default **true**) |
| `privacy_routing` | `MODEL_ROUTER_PRIVACY` | the fail-closed `#privacy` guard, **enabled by default**; `MODEL_ROUTER_PRIVACY=false` is a deliberate, risky opt-out that disables it (and `#privacy`) entirely |
| `effort_routing` | `MODEL_ROUTER_EFFORT` | per-task reasoning-effort rewrite for managed GPT requests (default **true**; never above the config ceiling) |
| `coding_effort_routing` | `MODEL_ROUTER_CODING_EFFORT` | per-task Claude effort for managed coding requests (default **true**; see [Reasoning effort](#reasoning-effort)) |
| `coding_effort_ceiling` | `MODEL_ROUTER_CODING_EFFORT_CEILING` | cap for the coding effort: `low`/`medium`/`high`/`xhigh`/`max` (default `max`; anything else loads as `max`) |
| `astra_policy` | `MODEL_ROUTER_ASTRA` | `task` (default: leading standalone `#max`/`#astra` line authorizes), `always` (legacy allow), `never` |
| `task_ttl_seconds` | `MODEL_ROUTER_TASK_TTL` | sliding TTL for per-session task state / authorization (default 1800) |
| `ollama_base_url` | `OLLAMA_BASE_URL` | host-reachable **local** endpoint (a non-local URL is refused) |
| `ollama_model` | `OLLAMA_CHAT_MODEL` | legacy single model: the default for every unset role (rendered as the routine model) |
| `ollama_routine_model` | `OLLAMA_ROUTINE_MODEL` | routine role (`qwen3.5:27b`) |
| `ollama_fallback_model` | `OLLAMA_FALLBACK_MODEL` | fallback role (`qwen3.6:27b`); empty = no local fallback |
| `ollama_agent_model` | `OLLAMA_AGENT_MODEL` | agent role (`qwen3.8:27b`) |
| `specialist_*` | `MODEL_ROUTER_SPECIALIST*` | specialist router, selection mode (`MODEL_ROUTER_SPECIALIST_MODE`, default `ai`: the classifier selects from the installed catalog; `hybrid`/`keywords` opt-in) and classifier ([specialist-routing.md](specialist-routing.md)); presence-aware on `--force` |
| `private_terms` | `MODEL_ROUTER_PRIVATE_TERMS` | **ignored by request routing** (only `#privacy`); a warning is logged at load when set. Still read by knowledge-classify's offline gate (presence-aware on `--force`, never logged) |
| `private_paths` | `MODEL_ROUTER_PRIVATE_PATHS` | same as `private_terms` |

All knobs are backwards-compatible: a pre-upgrade config without the new keys
gets the balanced defaults (effort rewrite on, Astra opt-in per task), and
explicit `false`/`never` values render and load verbatim.

## Activate the local hierarchy

Run these from your own terminal in the repo checkout, not from a Hermes or
Discord turn. Nothing below restarts the gateway except step 6.

1. Offline checks, then what the endpoint has:

   ```bash
   cd /opt/repos/claude-code-autopilot
   make test-local-route && make test-router
   make local-models            # qwen3.5:27b, qwen3.6:27b, qwen3.8:27b present with "tools"
   ```

2. Set the roles in `.env`. `MODEL_ROUTER_PRIVACY=true` is required for
   private turns to stay local. With `false` the guard is off, and private text
   in ordinary turns can reach the cloud.

   ```
   OLLAMA_BASE_URL=http://100.65.5.47:11434/v1
   OLLAMA_ROUTINE_MODEL=qwen3.5:27b
   OLLAMA_FALLBACK_MODEL=qwen3.6:27b
   OLLAMA_AGENT_MODEL=qwen3.8:27b
   AUX_BASE_URL=http://100.65.5.47:11434/v1
   AUX_MODEL=qwen3.5:27b
   # compression stays on CLIProxyAPI gpt-5.6-terra unless AUX_COMPRESSION_ROUTE=aux
   MODEL_ROUTER_PRIVACY=true
   MODEL_ROUTER_SPECIALISTS=true
   MODEL_ROUTER_SPECIALIST_MAX=4
   MODEL_ROUTER_SPECIALIST_CLASSIFIER=local
   ```

3. Probe and preview. Nothing is written:

   ```bash
   make specialist-classify-probe MODEL=qwen3.5:27b FALLBACK_MODEL=qwen3.6:27b TIMEOUT=25 \
     TASK="I keep going back and forth on what to do next with my bakery and the people around me"
   bash scripts/hermes-install.sh --render-only /tmp/hermes-next.yaml
   diff ~/.hermes/config.yaml /tmp/hermes-next.yaml
   ```

4. Back up the current config and plugin for rollback:

   ```bash
   stamp=$(date +%Y%m%d%H%M%S)
   cp -a ~/.hermes/config.yaml ~/.hermes/config.yaml.pre-local-roles.$stamp
   cp -a ~/.hermes/plugins/model-router ~/.hermes/backups/model-router.pre-local-roles.$stamp
   ```

5. Render and install, without restarting. `--force` also refreshes SOUL.md,
   honcho.json, the skills and the plugin, keeping `.bak` copies of the files
   it replaces:

   ```bash
   make hermes-config-apply     # = HERMES_SKIP_GATEWAY=1 bash scripts/hermes-install.sh --force
   make local-route-readback    # "live config matches the render; local-only invariants hold"
   ```

6. Restart, then read back:

   ```bash
   make hermes-restart
   make router-logs LINES=5     # model_router registered (... specialists=on) local_models=routine:qwen3.5:27b,fallback:qwen3.6:27b,agent:qwen3.8:27b classifier=local
   make local-route-status
   make specialist-status
   ```

7. One real turn each: a `#privacy` question
   (`make router-logs` shows `private->local ok ... role=routine model=qwen3.5:27b`),
   and an open-ended public question with no clear domain
   (`make specialist-logs` shows `gate=no-match classifier_model=...`).

**Rollback.**

- Classifier only: set `MODEL_ROUTER_SPECIALIST_CLASSIFIER=none` in `.env`,
  re-run step 5, then `make hermes-restart`.
- Specialists off: set `MODEL_ROUTER_SPECIALISTS=false` in `.env`. A `true` in
  `.env` wins on the next `--force`. Then `make specialist-disable` and
  `make hermes-restart`.
- Everything: restore the step-4 copies, revert the `.env` lines, restart.

  ```bash
  cp -a ~/.hermes/config.yaml.pre-local-roles.<stamp> ~/.hermes/config.yaml
  rm -rf ~/.hermes/plugins/model-router
  cp -a ~/.hermes/backups/model-router.pre-local-roles.<stamp> ~/.hermes/plugins/model-router
  make hermes-restart
  ```

The previous plugin ignores the new settings keys, and the new plugin reads an
old config through `ollama_model`, so either half can be rolled back alone.

## Readiness — when is private routing *live*?

Private routing is **fully active only** when both hold:

1. **Ollama is reachable from the host** where Hermes runs. In `docker-compose`
   Ollama is loopback-only *inside* Docker (`11434/tcp`, not published). Publish
   it to the host loopback (or run a host Ollama) and set `OLLAMA_BASE_URL`.
2. **The role models are pulled** (the compose Ollama only pulls the
   *embedding* model for Honcho): `ollama pull qwen3.5:27b` (and the fallback
   and agent models), then set the `OLLAMA_*_MODEL` roles. `make local-models`
   lists what the endpoint has.

Until both hold, the guard still protects you: private requests are **refused**
(never sent to the cloud), proven by `make test-router`. `make route-smoke`
confirms tier resolution against the live proxies.

## Honcho and private sessions — known limitation

Hermes 0.21.2 has **no per-turn or per-session "private" memory gate**: the
only Honcho write gates are global (`save_messages`) or launch-time
(`skip_memory`). `MemoryManager.sync_all` (called after every completed turn,
`run_agent.py`) and `prefetch_all`/`queue_prefetch_all` take the turn text with
no policy hook a plugin can drive. So a `#privacy` turn keeps the main-loop LLM
call local, but Honcho still persists that turn and may derive cloud
representations of it, and recalled memories can carry it into later public
turns. Private Honcho isolation is **not** active.

Fail-closed options until a core gate exists:

- Run private work under a Hermes **profile with memory disabled**
  (`memory.provider` unset / `memory_enabled: false`), or
- Point every Honcho reasoning model (`HONCHO_MODEL`) at a local endpoint so no
  transcript-derived reasoning leaves the host, or
- Globally set `save_messages: false` for private operating periods.

### Private Honcho route for `#private` (validator only; refuses today)

`hermes/plugins/model-router/private_honcho.py` holds the private memory route
so it can be reviewed and tested before Hermes can use it. It is **not wired
into Hermes** and changes nothing in the public Honcho (`HONCHO_*`,
`honcho.json`, the compose `honcho-*` services).

- **Config** (`PRIVATE_HONCHO_*` in `.env.example`): a separate Honcho API,
  its workspace, and its reasoning model/endpoint and embedding endpoint.
- **Validator** (`validate`, fail-closed): reasoning and embeddings must be a
  local Ollama (`:11434` or host `ollama`); CLIProxyAPI (`cliproxyapi`,
  `:8317`), claude-max-proxy (`:3456`), any configured proxy URL and every
  public host are refused. The private Honcho API must be local and a
  different instance from the public one: a second workspace on the public
  server is not isolation, because that server's reasoning goes through
  CLIProxyAPI.
- **Decision** (`decide` / `dispatch`): an unmarked turn keeps the public
  route exactly as configured. A marked turn gets the private route only with
  a valid config **and** a Hermes seam; otherwise it is a typed refusal
  (`PrivateHonchoRefused`) and neither Honcho is called. There is no path from
  a marked turn to the public route.
- **Seam:** `HERMES_MEMORY_ROUTE_SEAM = False`. Hermes 0.21.2
  `MemoryManager.sync_all` / `prefetch_all` / `queue_prefetch_all` /
  `on_pre_compress` take no host or route, and the Honcho provider binds one
  `baseUrl` per profile at startup, so every marked turn resolves to
  `refuse` (`kind="seam"`). Hermes core still syncs that turn to the public
  Honcho on its own (the limitation above is unchanged) until item 1 below
  lands and is wired here.

`make test-private-honcho` runs the hermetic tests.

### Pending design: private specialists and private memory (not implemented)

Neither exists yet. Blockers and the proposed shape, for review before any
build:

1. **Memory gate (Hermes core).** Add a plugin-drivable per-turn policy, for
   example a `memory_policy(session_id, user_message) -> "default" | "skip" |
   "local"` hook, consulted by `sync_all`, `prefetch_all`,
   `queue_prefetch_all` and `on_pre_compress`. model-router answers `local`
   for a turn where `privacy_marker.turn_is_marked` is true.
2. **Local Honcho.** A separate self-hosted Honcho workspace whose deriver and
   dialectic models are on Ollama, used only for `local` turns, with no
   cross-reads from the cloud-backed workspace. Until (1) exists, `skip` is
   the only safe action.
3. **Private specialists.** Today a private turn is answer-only (the local
   model gets no tools), so it cannot create a Kanban task or delegate. A
   private specialist needs: a tool-capable local path; profile variants whose
   main, auxiliary and memory routes are all local (no specialist profile is
   local today, so a private turn routes none); Kanban cards and delegation
   briefs that never reach a cloud model or the shared board in clear text;
   and the gate in (1) for the specialist's own memory.
4. **Aux guard.** The core auxiliary guard must recognize `#privacy`
   (it matches `#private`/`#local` and content today).

## Verify

```bash
make route-smoke     # live: probe /v1/models, degrade-check tiers, run the canary
make test-router     # offline: unit + zero-egress privacy canary
make models          # what each proxy exposes
make router-logs     # last 20 model_router lines from ~/.hermes/logs/agent.log (LINES=<n>)
make router-logs-follow  # same, then follow live (survives log rotation)
make local-route-status  # local roles vs endpoint, native chains + aux as Hermes resolves them
make local-models        # endpoint catalog: capabilities, size, loaded model, role
make local-route-readback  # live config vs the current render + local-only invariants (exit 1 on drift)
make test-local-route    # offline: roles, local fallback, classifier gate, installer render
```
