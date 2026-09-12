# Honcho

Honcho is the long-term memory layer. Hermes keeps its own `MEMORY.md` and
`USER.md` for durable facts; Honcho adds a model of *you* that improves across
sessions, and a "dialectic" endpoint the agent queries in natural language
("what does the operator prefer for commit messages?"). Upstream docs:
<https://honcho.dev/docs>.

The kit self-hosts it. Nothing leaves the machine and no vendor API key is
needed.

## What runs

| Container | Role |
|-----------|------|
| `honcho-api` | REST API on :8000 that Hermes talks to |
| `honcho-deriver` | background worker that turns messages into conclusions about the user |
| `honcho-db` | Postgres 15 with pgvector |
| `honcho-redis` | cache |
| `ollama` | local embedding model (`nomic-embed-text`) for Honcho's vector search |

## How reasoning is routed

Honcho's components (deriver, summary, dialectic levels, dream) each accept an
OpenAI-compatible endpoint. The compose file points all of them at CLIProxyAPI,
so Honcho thinks on the same ChatGPT subscription as Hermes:

```
DERIVER_MODEL_CONFIG__TRANSPORT=openai
DERIVER_MODEL_CONFIG__MODEL=${HONCHO_MODEL}
DERIVER_MODEL_CONFIG__OVERRIDES__BASE_URL=http://cliproxyapi:8317/v1
LLM_OPENAI_API_KEY=${CLIPROXY_API_KEY}
```

`HONCHO_MODEL` in `.env` must be a model your subscription exposes; check with
`make models`.

## Embeddings

A ChatGPT subscription has no embeddings endpoint, so Ollama serves one
locally. `HONCHO_EMBED_MODEL` and `HONCHO_EMBED_DIMENSIONS` must agree
(`nomic-embed-text` = 768). Changing the model after data exists requires
`make clean` because the vector column width is fixed at creation.

## Identity: peers and workspaces

`~/.hermes/honcho.json`:

```json
{
  "baseUrl": "http://127.0.0.1:8000",
  "hosts": {
    "hermes": { "enabled": true, "aiPeer": "hermes", "peerName": "me", "workspace": "agent-ops" }
  }
}
```

- `peerName` is you. Every profile and the Windows worker use the same name,
  so what one agent learns about you is available to all of them.
- `aiPeer` is the agent. Profiles get their own (`hermes.windows-operator`).
- `workspace` scopes everything. Keep one workspace for one operator.

Values come from `HONCHO_PEER_NAME` and `HONCHO_WORKSPACE` in `.env`.

## Checking it works

```bash
make honcho-health                 # {"status":"ok"}
hermes memory status               # provider honcho, connected
make logs S=honcho-deriver         # should show messages being processed after a chat
```

In a chat, tell Hermes something about yourself, start a new session, and ask
it back. The deriver needs a minute to process.

## Tuning

- **Deriver errors mentioning `response_format` or `json_schema`.** Some
  proxied models reject strict schemas. Set
  `HONCHO_STRUCTURED_OUTPUT_MODE=json_object` in `.env` and `make up`.
- **Slow first start.** Ollama pulls the model on first boot; Honcho waits for
  the healthcheck. `make logs S=ollama`.
- **Memory of a mistake.** Honcho is queryable and editable through its API
  (<http://127.0.0.1:8000/docs>). Deleting a workspace resets everything for
  that operator.
