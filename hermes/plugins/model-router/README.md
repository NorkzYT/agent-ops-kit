# model-router plugin

Two Hermes middleware behaviors in one bundled plugin:

- **Complexity routing** (`llm_request`) — deterministically scores each request
  and rewrites the model *within its own family*:
  - orchestrator (CLIProxyAPI): `routine→gpt-5.6-luna`, `medium→gpt-5.6-terra`,
    `high→gpt-5.6-sol`, `max→gpt-6-astra`.
  - delegated coding (claude-max-proxy): `opus` by default, `fable` for
    high/max complexity.
  It only rewrites models it manages (known aliases) and degrades to an
  available tier when a live `/v1/models` probe says the first choice is gone.
  It logs labels and scores only — never prompt content.

- **Privacy routing** (`llm_execution`) — **fail-closed**. If the outbound
  request (system + messages + tool results) carries private data, it is never
  sent to a cloud provider: it is answered by a local Ollama model or refused.
  Signals: `#private`/`#local` tags, US SSN, Luhn-valid PAN, ABA routing number,
  IBAN, private keys / provider secrets / bearer tokens, credential pairs, and
  operator-configured terms/paths. Once a session is flagged private it stays
  local. Classifier errors are treated as private. The handler never raises, so
  Hermes' fail-open middleware path can never leak private data to the cloud.

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
        ollama_base_url: "http://127.0.0.1:11434/v1"   # host-reachable Ollama
        ollama_model: "llama3.1:8b"                     # a pulled CHAT model
        private_terms: []                               # extra local-only terms
        private_paths: []                               # extra local-only path fragments
        available_gpt_models: []                        # optional probe cache
        available_coding_models: []
```

Environment fallbacks (used when a setting is unset): `OLLAMA_BASE_URL`,
`OLLAMA_CHAT_MODEL`, `MODEL_ROUTER_COMPLEXITY`, `MODEL_ROUTER_PRIVACY`,
`MODEL_ROUTER_PRIVATE_TERMS`, `MODEL_ROUTER_PRIVATE_PATHS`.

## Readiness

Private routing is **active only** when a host-reachable Ollama chat endpoint is
configured *and* the zero-egress canary passes. Until then the plugin still runs
fail-closed: private requests are **refused** (never sent to cloud). See
`docs/model-routing.md`.
