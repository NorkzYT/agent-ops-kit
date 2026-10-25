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
locally. `HONCHO_EMBED_MODEL` and `HONCHO_EMBED_DIMENSIONS` must agree.

| Model | Dimensions | Context | Size | Use when |
|-------|-----------:|--------:|-----:|----------|
| `nomic-embed-text` (default) | 768 | 8192 | 274 MB | English memory, CPU-only host |
| `bge-m3` | 1024 | 8192 | 1.2 GB | you chat with Hermes in several languages |
| `mxbai-embed-large` | 1024 | 512 | 670 MB | not recommended: truncates Honcho's longer documents |

Honcho sends inputs up to `EMBEDDING_MAX_INPUT_TOKENS=8192`, so the model's
context has to be 8k. Stay on the default unless you need multilingual recall.

Honcho's migrations always create the pgvector columns as `vector(1536)`.
The kit's `honcho-api` entrypoint (`docker/honcho/entrypoint.sh`) runs
upstream's `scripts/configure_embeddings.py --yes` after the migrations, which
resizes the columns to `HONCHO_EMBED_DIMENSIONS` while the tables are empty and
is a no-op afterwards. Changing the model after data exists therefore requires
`make clean` (the script refuses to alter populated tables).

## Database password

`honcho-db` is Postgres with password auth; `make init` generates
`HONCHO_DB_PASSWORD` in `.env` and both `honcho-api` and the deriver use it.
The port is not published to the host. Postgres only reads the password when
the data volume is first created, so a stack that was initialised before this
setting existed keeps its old auth until you either run `make clean && make up`
(empty database) or follow the manual steps in `docs/troubleshooting.md`.

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

- `peerName` is you. Honcho keeps a "peer" per participant and builds its
  model of the user under this name, so every profile and the Windows worker
  use the same value and share what they learn about you. `me` or your handle,
  it does not matter, but set it before the first install: a new name is a new
  peer with empty memory.
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
