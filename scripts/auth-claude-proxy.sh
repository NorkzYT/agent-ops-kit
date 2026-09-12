#!/usr/bin/env bash
# auth-claude-proxy.sh — log the Claude Max subscription into claude-max-proxy.
#
# Runs `claude setup-token` in a one-off container (works even while the
# service container is idle or missing), stores the long-lived token in .env
# as CLAUDE_CODE_OAUTH_TOKEN, recreates the service, and waits for /health.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

COMPOSE="${COMPOSE:-docker compose}"
[[ -f .env ]] || die ".env missing: run make init"
have docker || die "docker not found"

mkdir -p data/claude-max-proxy/home/.config data/claude-max-proxy/data

log "building the proxy image if needed"
$COMPOSE build claude-max-proxy >/dev/null

echo
echo "A login URL follows. Open it, sign in with the Claude Max account, paste the"
echo "code back here. At the end the CLI prints a token starting with sk-ant-oat01-."
echo
# --entrypoint bypasses the kit's wait-for-credentials wrapper; --no-deps keeps
# the rest of the stack out of it; --rm leaves no container behind.
$COMPOSE run --rm --no-deps --entrypoint claude claude-max-proxy setup-token || true

echo
read -r -p "Paste the sk-ant-oat01-... token (Enter to skip): " token
token="${token//[[:space:]]/}"
if [[ -z "$token" ]]; then
  warn "no token stored. Re-run make auth-claude-proxy, or put it in .env as CLAUDE_CODE_OAUTH_TOKEN and run: make up"
  exit 0
fi
[[ "$token" == sk-ant-oat01-* ]] || warn "token does not start with sk-ant-oat01-; storing it anyway"

env_file_set CLAUDE_CODE_OAUTH_TOKEN "$token" .env
log "stored CLAUDE_CODE_OAUTH_TOKEN in .env"

log "recreating claude-max-proxy with the token"
$COMPOSE up -d claude-max-proxy

port="$(env_file_get CLAUDE_MAX_PROXY_PORT .env 2>/dev/null || echo 3456)"
printf '[agent-ops-kit] waiting for http://127.0.0.1:%s/health ' "$port"
for _ in $(seq 1 40); do
  if curl -fsS -m 3 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    echo; log "claude-max-proxy is up. Models: make models"
    exit 0
  fi
  printf '.'; sleep 3
done
echo
warn "proxy not healthy yet (first start probes every model, ~1 min). Check: make logs S=claude-max-proxy"
