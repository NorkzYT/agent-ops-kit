# model-router plugin

Two Hermes middleware behaviors in one bundled plugin, plus a load-time
extension of Hermes' auxiliary egress guard (`aux_guard.py`: CLIProxyAPI and
claude-max-proxy count as cloud even on loopback, and a `#privacy`/`#private`
payload is never sent to them by compression or title generation; see
`docs/model-routing.md`, Auxiliary pre-egress guard):

- **Task-scoped routing** (`llm_request`) — deterministically classifies the
  CURRENT user task (never history, system prompts, or tool output) and
  rewrites model **and** reasoning effort *within the request's family*:
  - orchestrator (CLIProxyAPI): status/lookup→`gpt-5.6-luna` (low effort),
    ordinary work→`gpt-5.6-luna` (medium), ambiguous/planning→`gpt-5.6-terra`,
    genuinely hard→`gpt-5.6-sol`; `gpt-6-astra` ONLY when the current task
    starts with a standalone `#max`/`#astra` control line (per-task, TTL-bound,
    never bleeding to the next task or session). The automatic ceiling is Sol.
  - delegated coding (claude-max-proxy): `opus` by default, `fable` only for
    genuinely difficult engineering; Claude effort per task — `medium`
    routine/ordinary, `high` complex, `xhigh` only for unusually hard
    debugging/architecture, `max` only with a leading `#max`/`#astra` — capped
    by `coding_effort_ceiling`, not the orchestrator ceiling.
  A **leading standalone `#opus`/`#fable`** line forces that coding model for the
  request, overriding complexity (effort still follows the task body, never
  `max`; no state stored). Because the
  middleware can rewrite the request body but not switch the bound
  provider/`base_url`, the force is honored only on a coding-family seam; on the
  orchestrator/GPT seam it is refused with a typed response at the execution seam
  rather than faked with a GPT alias. Privacy is enforced first, so a private
  forced request never reaches the cloud coding proxy.
  Quoted/fenced/tool-borne `#max`/`#deep`/`#hard`/`#opus`/`#fable` tags are inert; `#deep`/
  `#hard` never grant Astra. The classifier first reduces the Hermes wire
  format to the sender-authored segment (`task_intent.genuine_user_text`):
  gateway trigger/reply metadata, channel backfill (`[New message]` marker),
  the `[sender]` prefix, appended `<memory-context>` and
  `--- Attached Context ---` blocks are provenance-stripped, so recalled
  memories, other users' channel messages, quoted replies, attachments and
  pasted transcripts neither inflate the tier nor grant authorization — while
  a genuine leading `#max` still authorizes under the gateway wrapper. The
  same-task hash is stable across tool iterations, the absolute state
  lifetime survives per-iteration re-puts, and an expired (retired) task hash
  cannot re-authorize Astra from the same historical message. Effort is only
  rewritten on fields the request already carries (both Chat Completions and
  Responses shapes), never above the configured ceiling. It only rewrites
  models it manages (known aliases) and degrades to an available tier when a
  live `/v1/models` probe says the first choice is gone; a missing lower tier
  never silently upgrades to Astra — an unauthorized Astra request that
  cannot be downgraded is refused with a typed response at the execution
  seam. Every managed decision (even unchanged) is logged with opaque hashes,
  tier/reason labels and transport outcomes — never prompt content; a lazy
  streaming response logs `stream-opened` (completion unobserved) rather than
  a fabricated `completed`, and is returned unconsumed.

- **Privacy routing** (`llm_execution`) — **per-request `#privacy` marker,
  fail-closed**. A request whose current user turn carries a standalone
  `#privacy` typed by the sender is never sent to a cloud provider: it is
  answered by a local Ollama model or refused. That marker is the only signal:
  content is not classified (ID/card/bank numbers, secrets, credentials and
  `private_terms`/`private_paths` route nothing), `#private` is equivalent to
  `#privacy`, `#local` is inert,
  and there is no session stickiness — the next unmarked turn is public. The
  marker never comes from system prompts, recalled memory, channel backfill,
  reply quotes, tool results, quoted/fenced text or an attached file (unless
  the upload is the whole prompt). Earlier marked exchanges are withheld from
  later public requests (placeholder text; tool-call ids kept). Check errors
  are treated as private. The handler never raises, so Hermes' fail-open
  middleware path can never send a marked request to the cloud.
  The local answer comes from an explicit role on the same endpoint: the
  **routine** model for ordinary turns, the **agent** model for high/max-tier
  turns, delegated coding and `#opus`/`#fable`; the **fallback** model is tried
  once on a model-level failure (404, timeout, bad response), never after a
  connection failure; then the request is refused. A non-local
  `ollama_base_url` is refused unsent.

- **Specialist routing pilot** (`pre_llm_call` hook, off by default) —
  `specialists.py` deterministically picks 0-4 book-backed profiles for the
  current genuine user task and appends a `<specialist-routing>` note to the
  orchestrator's turn (privacy first, high-risk profiles advisory with the
  `skills` toolset only, readiness from the installed profile's `/learn`
  skill). With `specialist_classifier: cliproxy` a public task with no or
  only weak keyword matches, or with verified attachments, asks CLIProxyAPI
  (`gpt-5.6-terra`, `medium` reasoning) for at most two non-high-risk
  additions, from the typed task plus the verified attached documents (PDF
  and DOCX text extracted locally, bounded; wire text, `#privacy` and routing
  notes withheld). `local` asks the routine local model instead. A
  `#privacy` turn never reaches either. `/route-specialists <task>` and `make
  specialist-route` are offline dry-runs; `make specialist-classify-probe`
  exercises the live classifier. See `docs/specialist-routing.md`.

## Configuration

`~/.hermes/config.yaml`:

```yaml
plugins:
  enabled: [model-router]
  entries:
    model-router:
      settings:
        complexity_routing: true
        privacy_routing: true
        effort_routing: true           # per-task effort rewrite (gpt family)
        coding_effort_routing: true    # per-task Claude effort (coding family)
        coding_effort_ceiling: max     # low | medium | high | xhigh | max
        astra_policy: task             # task | always | never
        task_ttl_seconds: 1800         # per-session task-state TTL
        ollama_base_url: "http://127.0.0.1:11434/v1"   # host-reachable, local-only Ollama
        ollama_model: "qwen3.5:27b"                     # legacy: default for unset roles
        ollama_routine_model: "qwen3.5:27b"             # ordinary private turns + classifier
        ollama_fallback_model: "qwen3.6:27b"            # tried once after a role-model failure
        ollama_agent_model: "qwen3.8:27b"               # difficult private turns + private coding
        private_terms: []                               # ignored by routing (#privacy only)
        private_paths: []                               # ignored by routing (#privacy only)
        available_gpt_models: []                        # optional probe cache
        available_coding_models: []
        specialist_routing: false        # pilot; docs/specialist-routing.md
        specialist_max_profiles: 4
        specialist_require_ready: false
        specialist_classifier: none      # none | local | cliproxy (gpt-5.6-terra, medium effort)
        specialist_classifier_timeout: 10  # seconds, both models, max 25
        specialist_profiles: []
```

Environment fallbacks (used when a setting is unset): `OLLAMA_BASE_URL`,
`OLLAMA_CHAT_MODEL`, `OLLAMA_ROUTINE_MODEL`, `OLLAMA_FALLBACK_MODEL`,
`OLLAMA_AGENT_MODEL`, `MODEL_ROUTER_SPECIALIST_CLASSIFIER_TIMEOUT`, `MODEL_ROUTER_COMPLEXITY`, `MODEL_ROUTER_PRIVACY`,
`MODEL_ROUTER_EFFORT`, `MODEL_ROUTER_CODING_EFFORT`,
`MODEL_ROUTER_CODING_EFFORT_CEILING`, `MODEL_ROUTER_ASTRA`, `MODEL_ROUTER_TASK_TTL`,
`MODEL_ROUTER_PRIVATE_TERMS`, `MODEL_ROUTER_PRIVATE_PATHS`.

`complexity_routing` and `privacy_routing` both default to **true**. The kit's
`hermes-install.sh` renders them from `MODEL_ROUTER_COMPLEXITY` /
`MODEL_ROUTER_PRIVACY` in `.env`; an explicit `false` is preserved (privacy_routing
`false` is a deliberate, risky opt-out that disables the fail-closed guard).

## Readiness

Private routing is **active only** when a host-reachable Ollama chat endpoint is
configured *and* the zero-egress canary passes. Until then the plugin still runs
fail-closed: private requests are **refused** (never sent to cloud). See
`docs/model-routing.md`.
