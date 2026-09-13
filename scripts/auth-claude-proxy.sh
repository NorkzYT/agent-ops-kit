#!/usr/bin/env bash
# auth-claude-proxy.sh — log the Claude Max subscription into claude-max-proxy.
#
# Runs `claude setup-token` on the host (against the proxy's private
# CLAUDE_CONFIG_DIR, so your own `claude` login is untouched), stores the
# long-lived token in .env as CLAUDE_CODE_OAUTH_TOKEN, then re-runs the proxy
# installer, which re-renders proxy.env, restarts the service and waits for
# /health.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

[[ -f .env ]] || die ".env missing: run make init"
bash scripts/claude-max-proxy/install.sh --deps-only
export PATH="$HOME/.local/bin:$PATH"

mkdir -p data/claude-max-proxy/claude
echo
echo "A login URL follows. Open it, sign in with the Claude Max account, paste the"
echo "code back here. At the end the CLI prints a token starting with sk-ant-oat01-."
echo
CLAUDE_CONFIG_DIR="$ROOT/data/claude-max-proxy/claude" claude setup-token || true

echo
read -r -p "Paste the sk-ant-oat01-... token (Enter to skip): " token
token="${token//[[:space:]]/}"
if [[ -z "$token" ]]; then
  warn "no token stored. Re-run make auth-claude-proxy, or put it in .env as CLAUDE_CODE_OAUTH_TOKEN and run: make claude-proxy-install"
  exit 0
fi
[[ "$token" == sk-ant-oat01-* ]] || warn "token does not start with sk-ant-oat01-; storing it anyway"

env_file_set CLAUDE_CODE_OAUTH_TOKEN "$token" .env
log "stored CLAUDE_CODE_OAUTH_TOKEN in .env"

exec bash scripts/claude-max-proxy/install.sh
