# model-router plugin

Two Hermes middleware behaviors in one bundled plugin:

- **Task-scoped routing** (`llm_request`) — deterministically classifies the
  CURRENT user task (never history, system prompts, or tool output) and
  rewrites model **and** reasoning effort *within the request's family*:
  - orchestrator (CLIProxyAPI): status/lookup→`gpt-5.6-luna` (low effort),
    ordinary work→`gpt-5.6-luna` (medium), ambiguous/planning→`gpt-5.6-terra`,
    genuinely hard→`gpt-5.6-sol`; `gpt-6-astra` ONLY when the current task
    starts with a standalone `#max`/`#astra` control line (per-task, TTL-bound,
    never bleeding to the next task or session). The automatic ceiling is Sol.
  - delegated coding (claude-max-proxy): `opus` by default, `fable` only for
    genuinely difficult engineering; effort fields are never touched here.
  A **leading standalone `#opus`/`#fable`** line forces that coding model for the
  request, overriding complexity (effort untouched, no state stored). Because the
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

- **Privacy routing** (`llm_execution`) — **fail-closed**. If the outbound
  request (system + messages + tool results) carries private data, it is never
  sent to a cloud provider: it is answered by a local Ollama model or refused.
  Signals: `#private`/`#local` tags, US SSN, Luhn-valid PAN, ABA routing number,
  IBAN, private keys / provider secrets / bearer tokens, credential pairs, and
  operator-configured terms/paths. Once a session is flagged private it stays
  local. Classifier errors are treated as private. The handler never raises, so
  Hermes' fail-open middleware path can never leak private data to the cloud.

- **Specialist routing pilot** (`pre_llm_call` hook, off by default) —
  `specialists.py` deterministically picks 0-4 book-backed profiles for the
  current genuine user task and appends a `<specialist-routing>` note to the
  orchestrator's turn (privacy first, high-risk profiles advisory with the
  `skills` toolset only, readiness from the installed profile's `/learn`
  skill). `/route-specialists <task>` and `make specialist-route` are offline
  dry-runs. See `docs/specialist-routing.md`.

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
        astra_policy: task             # task | always | never
        task_ttl_seconds: 1800         # per-session task-state TTL
        ollama_base_url: "http://127.0.0.1:11434/v1"   # host-reachable Ollama
        ollama_model: "llama3.1:8b"                     # a pulled CHAT model
        private_terms: []                               # extra local-only terms
        private_paths: []                               # extra local-only path fragments
        available_gpt_models: []                        # optional probe cache
        available_coding_models: []
        specialist_routing: false        # pilot; docs/specialist-routing.md
        specialist_max_profiles: 4
        specialist_require_ready: false
        specialist_classifier: none      # none | local
        specialist_profiles: []
```

Environment fallbacks (used when a setting is unset): `OLLAMA_BASE_URL`,
`OLLAMA_CHAT_MODEL`, `MODEL_ROUTER_COMPLEXITY`, `MODEL_ROUTER_PRIVACY`,
`MODEL_ROUTER_EFFORT`, `MODEL_ROUTER_ASTRA`, `MODEL_ROUTER_TASK_TTL`,
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
