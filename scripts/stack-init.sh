#!/usr/bin/env bash
# stack-init.sh — one-time (idempotent) preparation of the Docker half.
#   1. create .env from .env.example if missing
#   2. fill empty CLIPROXY_API_KEY / CLIPROXY_MANAGEMENT_KEY with random values
#   3. render data/cliproxyapi/config.yaml from docker/cliproxyapi/config.example.yaml
#   4. clone/fast-forward the claude-max-api-proxy sources used to build its image
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

log "agent-ops-kit init"

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  log "created .env from .env.example"
fi

ensure_secret CLIPROXY_API_KEY .env
ensure_secret CLIPROXY_MANAGEMENT_KEY .env

if [[ -z "$(env_file_get PUID .env || true)" ]]; then env_file_set PUID "$(id -u)" .env; fi
if [[ -z "$(env_file_get PGID .env || true)" ]]; then env_file_set PGID "$(id -g)" .env; fi

mkdir -p data/cliproxyapi/auths data/cliproxyapi/logs
api_key="$(env_file_get CLIPROXY_API_KEY .env)"
mgmt_key="$(env_file_get CLIPROXY_MANAGEMENT_KEY .env)"
sed -e "s|__CLIPROXY_API_KEY__|${api_key}|" -e "s|__CLIPROXY_MANAGEMENT_KEY__|${mgmt_key}|" \
  docker/cliproxyapi/config.example.yaml > data/cliproxyapi/config.yaml
chmod 600 data/cliproxyapi/config.yaml
log "rendered data/cliproxyapi/config.yaml"

proxy_dir="$(env_file_get CLAUDE_MAX_PROXY_DIR .env || true)"
proxy_dir="${proxy_dir:-./vendor/claude-max-api-proxy}"
CLAUDE_MAX_PROXY_REPO="$(env_file_get CLAUDE_MAX_PROXY_REPO .env || true)" \
CLAUDE_MAX_PROXY_REF="$(env_file_get CLAUDE_MAX_PROXY_REF .env || true)" \
  bash docker/claude-max-proxy/sync-checkout.sh "$proxy_dir"

if ! command -v docker >/dev/null 2>&1; then
  warn "docker not found. Install Docker Engine + Compose plugin: https://docs.docker.com/engine/install/"
fi

log "done. Next: make up"
