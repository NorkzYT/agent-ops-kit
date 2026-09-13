#!/usr/bin/env bash
# doctor.sh — check the whole stack in one go. Exit 1 when something is broken.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

ERR=0; WARN=0
ok()   { printf '  [OK]   %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; ERR=$((ERR+1)); }
warn_() { printf '  [WARN] %s\n' "$*"; WARN=$((WARN+1)); }
hdr()  { printf '\n== %s ==\n' "$*"; }
getenv() { env_file_get "$1" .env 2>/dev/null || printf '%s' "${2:-}"; }

hdr "Files"
[[ -f .env ]] && ok ".env present" || { bad ".env missing (make init)"; }
[[ -f data/cliproxyapi/config.yaml ]] && ok "CLIProxyAPI config rendered" || bad "data/cliproxyapi/config.yaml missing (make init)"
proxy_dir="$(getenv CLAUDE_MAX_PROXY_DIR ./vendor/claude-max-api-proxy)"
[[ -f "$proxy_dir/Dockerfile" ]] && ok "claude-max-api-proxy sources at $proxy_dir" || bad "proxy sources missing at $proxy_dir (make init)"
for k in CLIPROXY_API_KEY HONCHO_DB_PASSWORD DISCORD_BOT_TOKEN DISCORD_ALLOWED_USERS; do
  [[ -n "$(getenv "$k")" ]] && ok "$k set" || warn_ "$k empty in .env"
done
[[ -n "$(getenv CLAUDE_CODE_OAUTH_TOKEN)" ]] && ok "CLAUDE_CODE_OAUTH_TOKEN set" || warn_ "CLAUDE_CODE_OAUTH_TOKEN empty (make auth-claude-proxy)"
[[ -d data/claude-max-proxy/home ]] && ok "claude-max-proxy home dir" || warn_ "data/claude-max-proxy/home missing (make init)"

hdr "Docker"
if have docker; then
  ok "docker $(docker --version 2>/dev/null | sed 's/Docker version //')"
  if docker compose version >/dev/null 2>&1; then
    ok "compose plugin"
    if docker compose config -q 2>/tmp/doctor-compose.err; then ok "docker-compose.yml valid"; else bad "docker-compose.yml invalid: $(head -n1 /tmp/doctor-compose.err)"; fi
    for s in honcho-db honcho-redis ollama cliproxyapi honcho-api honcho-deriver claude-max-proxy; do
      st="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$s" 2>/dev/null || echo missing)"
      case "$st" in
        "running healthy"|"running ") ok "$s: $st";;
        running*)
          if [[ "$s" == claude-max-proxy && -z "$(getenv CLAUDE_CODE_OAUTH_TOKEN)" ]]; then
            warn_ "$s: $st (idle, waiting for credentials: make auth-claude-proxy)"
          else warn_ "$s: $st"; fi;;
        missing) bad "$s: not created (make up)";;
        *) bad "$s: $st";;
      esac
    done
  else bad "docker compose plugin missing"; fi
else
  bad "docker not installed"
fi

hdr "Endpoints"
cp_port="$(getenv CLIPROXY_PORT 8317)"; cm_port="$(getenv CLAUDE_MAX_PROXY_PORT 3456)"; h_port="$(getenv HONCHO_PORT 8000)"
if curl -fsS -m 5 http://127.0.0.1:"$h_port"/health >/dev/null 2>&1; then ok "Honcho API http://127.0.0.1:$h_port/health"; else bad "Honcho API not answering on :$h_port"; fi
if out="$(curl -fsS -m 10 -H "Authorization: Bearer $(getenv CLIPROXY_API_KEY)" http://127.0.0.1:"$cp_port"/v1/models 2>/dev/null)"; then
  n="$(grep -o '"id"' <<<"$out" | wc -l | tr -d ' ')"
  if [[ "$n" -gt 0 ]]; then ok "CLIProxyAPI :$cp_port exposes $n models"; else warn_ "CLIProxyAPI up but no models (make auth-codex)"; fi
else bad "CLIProxyAPI not answering on :$cp_port"; fi
if out="$(curl -fsS -m 10 http://127.0.0.1:"$cm_port"/v1/models 2>/dev/null)"; then
  n="$(grep -o '"id"' <<<"$out" | wc -l | tr -d ' ')"
  if [[ "$n" -gt 0 ]]; then ok "claude-max-proxy :$cm_port exposes $n models"; else warn_ "claude-max-proxy up but no models (make auth-claude-proxy)"; fi
else bad "claude-max-proxy not answering on :$cm_port"; fi
if have docker && docker exec ollama ollama list 2>/dev/null | grep -q "$(getenv HONCHO_EMBED_MODEL nomic-embed-text)"; then
  ok "Ollama has $(getenv HONCHO_EMBED_MODEL nomic-embed-text)"
else warn_ "Ollama embedding model not listed yet (first start pulls it; check: make logs S=ollama)"; fi

hdr "Hermes"
export PATH="$HOME/.local/bin:$PATH"
HERMES_HOME="$(getenv HERMES_HOME "$HOME/.hermes")"
if have hermes; then
  ok "hermes installed ($(hermes --version 2>/dev/null | head -n1 || echo version unknown))"
  [[ -f "$HERMES_HOME/config.yaml" ]] && ok "config.yaml" || bad "config.yaml missing (make hermes-install)"
  [[ -f "$HERMES_HOME/honcho.json" ]] && ok "honcho.json" || bad "honcho.json missing (make hermes-install)"
  grep -q "127.0.0.1:$cp_port" "$HERMES_HOME/config.yaml" 2>/dev/null && ok "model routed to CLIProxyAPI" || warn_ "config.yaml does not point at CLIProxyAPI"
  grep -q "127.0.0.1:$cm_port" "$HERMES_HOME/config.yaml" 2>/dev/null && ok "delegation routed to claude-max-proxy" || warn_ "delegation not pointed at claude-max-proxy"
  if have systemctl && systemctl --user is-active hermes-gateway >/dev/null 2>&1; then ok "gateway service active"
  elif pgrep -f 'hermes.*gateway' >/dev/null 2>&1; then ok "gateway process running"
  else warn_ "gateway not running (hermes gateway start)"; fi
  hermes doctor >/tmp/hermes-doctor.out 2>&1 && ok "hermes doctor clean" || warn_ "hermes doctor reported issues: see /tmp/hermes-doctor.out"
else
  bad "hermes not installed (make hermes-install)"
fi

printf '\n%d failure(s), %d warning(s)\n' "$ERR" "$WARN"
[[ "$ERR" -eq 0 ]]
