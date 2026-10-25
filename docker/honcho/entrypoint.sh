#!/bin/sh
# Honcho API entrypoint for agent-ops-kit.
#
# Upstream's docker/entrypoint.sh runs the Alembic migrations and starts the
# API. The migrations always create the pgvector columns as vector(1536)
# (OpenAI text-embedding-3-small). This stack embeds with a local Ollama model
# of a different width (nomic-embed-text = 768), so Honcho's own
# scripts/configure_embeddings.py must ALTER the columns to
# EMBEDDING_VECTOR_DIMENSIONS before the API starts, or the startup validator
# refuses to boot:
#   "public.documents.embedding dim (1536) does not match
#    EMBEDDING_VECTOR_DIMENSIONS (768)"
#
# The script is a no-op when the columns already match and refuses to touch
# populated tables, so it is safe to run on every start. To change the
# embedding model after data exists: `make clean` (fresh database).
set -eu

echo "Running database migrations..."
/app/.venv/bin/python scripts/provision_db.py

echo "Configuring pgvector columns for EMBEDDING_VECTOR_DIMENSIONS=${EMBEDDING_VECTOR_DIMENSIONS:-1536}..."
/app/.venv/bin/python scripts/configure_embeddings.py --yes

echo "Starting API server..."
exec /app/.venv/bin/fastapi run --host 0.0.0.0 --workers "${API_WORKERS:-1}" src/main.py
