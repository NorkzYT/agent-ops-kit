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

## Complexity routing (`llm_request` middleware)

Each request is scored deterministically (length, code fences, architectural
keywords, turn count; `#max`/`#quick` force a tier) and the model is rewritten
**within its own family**. Only models the plugin manages (the aliases below)
are ever rewritten — a deliberate exact id is left untouched.

| Tier | Orchestrator (GPT) | Coding |
|------|--------------------|--------|
| routine | `gpt-5.6-luna` | `opus` |
| medium | `gpt-5.6-terra` | `opus` |
| high | `gpt-5.6-sol` | `fable` |
| max | `gpt-6-astra` | `fable` |

Tier order follows OpenAI's line: **Luna** is fastest/lowest-cost (the routine
floor), **Terra** balances intelligence and cost, **Sol** is the flagship for
complex professional work, and **Astra** (GPT-6) handles the hardest
end-to-end work.

**Degrade:** given a live `/v1/models` set, a chosen tier that is not available
steps *down* to the nearest available tier (never up). `make route-smoke`
probes both proxies and proves every tier resolves to a live model.

Routing decisions log the tier, integer score, and detector labels only —
never prompt content.

## Reasoning effort

`config.yaml` sets `agent.reasoning_effort: high`. This is Hermes' single
chokepoint (`resolve_reasoning_config`): it governs the **orchestrator** and
every **delegated subagent**, clamped per model at the transport boundary. `high`
is a valid level for every model the kit uses — GPT-5.6 luna/terra/sol and GPT-6
astra on the Codex Responses wire, and `opus`/`fable` through claude-max-proxy,
which reads `reasoning_effort` and maps it to a Claude CLI effort level (so this
does not break Claude proxy semantics). **Auxiliary** title/compression calls
carry `reasoning_effort: high` on each `auxiliary.*` entry as well.

Complexity routing (which model) and reasoning effort (how hard it thinks) are
orthogonal: the plugin only rewrites the model id; effort stays in native config.

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
| `complexity_routing` | `MODEL_ROUTER_COMPLEXITY` | enable the model rewrite (default **true**) |
| `privacy_routing` | `MODEL_ROUTER_PRIVACY` | the fail-closed guard, **enabled by default**; `MODEL_ROUTER_PRIVACY=false` is a deliberate, risky opt-out that disables it entirely |
| `ollama_base_url` | `OLLAMA_BASE_URL` | host-reachable local endpoint |
| `ollama_model` | `OLLAMA_CHAT_MODEL` | a pulled **chat** model |
| `private_terms` | `MODEL_ROUTER_PRIVATE_TERMS` | extra local-only terms |
| `private_paths` | `MODEL_ROUTER_PRIVATE_PATHS` | extra local-only path fragments |

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
```
