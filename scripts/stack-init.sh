#!/usr/bin/env bash
# stack-init.sh — one-time (idempotent) preparation of the Docker half.
#   1. create .env from .env.example if missing
#   2. fill empty CLIPROXY_API_KEY / CLIPROXY_MANAGEMENT_KEY / HONCHO_DB_PASSWORD with random values
#   3. render data/cliproxyapi/config.yaml from docker/cliproxyapi/config.example.yaml
#   4. create the bind-mount directories the containers write to
# The Claude proxy (host, not Docker) is set up by scripts/claude-max-proxy/install.sh.
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
ensure_secret HONCHO_DB_PASSWORD .env
# Bearer secret for the host claude-max-proxy (auth on /v1/* and admin/ops).
ensure_secret CLAUDE_MAX_PROXY_API_KEY .env

mkdir -p data/cliproxyapi/auths data/cliproxyapi/logs
# render_template (lib.sh) escapes | \ & in the substituted values, so a key
# that happens to contain sed metacharacters cannot break out of the s/// or
# trigger GNU sed's `e` command. Do not hand-roll sed here.
CLIPROXY_API_KEY="$(env_file_get CLIPROXY_API_KEY .env)" \
CLIPROXY_MANAGEMENT_KEY="$(env_file_get CLIPROXY_MANAGEMENT_KEY .env)" \
  render_template docker/cliproxyapi/config.example.yaml data/cliproxyapi/config.yaml
chmod 600 data/cliproxyapi/config.yaml
log "rendered data/cliproxyapi/config.yaml"

# Legacy: the proxy container used to run as PUID/PGID with these as its home.
if [[ -d data/claude-max-proxy/home ]]; then
  warn "data/claude-max-proxy/home is from the old Docker proxy; safe to delete after make claude-proxy-install"
fi

if ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
  warn "gh is not logged in on this host; the coding worker cannot push or open PRs until you run: gh auth login"
fi

if ! command -v docker >/dev/null 2>&1; then
  warn "docker not found. Install Docker Engine + Compose plugin: https://docs.docker.com/engine/install/"
fi

log "done. Next: make up"
