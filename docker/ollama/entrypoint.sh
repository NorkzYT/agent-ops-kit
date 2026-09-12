#!/bin/sh
# Ollama entrypoint for agent-ops-kit.
# Starts the server, pulls the embedding model Honcho uses, then marks the
# container ready (the compose healthcheck waits for /tmp/ollama-ready).
set -eu

MODEL="${OLLAMA_EMBED_MODEL:-nomic-embed-text}"
rm -f /tmp/ollama-ready

ollama serve &
SERVER_PID=$!

# Wait for the API to answer.
i=0
until ollama list >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "[ollama] server did not start in time" >&2
    exit 1
  fi
  sleep 1
done

if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL" || \
   ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL:latest"; then
  echo "[ollama] embedding model already present: $MODEL"
else
  echo "[ollama] pulling embedding model: $MODEL"
  n=0
  until ollama pull "$MODEL"; do
    n=$((n + 1))
    if [ "$n" -ge 5 ]; then
      echo "[ollama] pull failed 5 times; giving up (container stays unhealthy)" >&2
      wait "$SERVER_PID"
      exit 1
    fi
    echo "[ollama] pull failed, retrying in 10s ($n/5)" >&2
    sleep 10
  done
fi

touch /tmp/ollama-ready
echo "[ollama] ready — serving embeddings for $MODEL"
wait "$SERVER_PID"
