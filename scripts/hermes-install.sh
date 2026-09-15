#!/usr/bin/env bash
# hermes-install.sh — install Hermes on this host and wire it to the stack.
#
#   reads   .env                          (kit settings + secrets)
#   writes  $HERMES_HOME/config.yaml      (from hermes/config.yaml.tmpl)
#           $HERMES_HOME/.env             (Discord, proxy key, Browser Use)
#           $HERMES_HOME/honcho.json      (self-hosted Honcho)
#           $HERMES_HOME/SOUL.md          (orchestrator persona, first time)
#           $HERMES_HOME/memories/USER.md (first time)
#           $HERMES_HOME/skills/agent-ops-kit/*
#   runs    the official Hermes installer when `hermes` is missing,
#           installs the Honcho plugin dependency,
#           installs + starts the gateway service, then `hermes doctor`.
#
# Usage: scripts/hermes-install.sh [--force]   (--force rewrites config.yaml and SOUL.md, keeping .bak copies)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

# ENV_FILE defaults to ./.env; overridable so tests can point at a fixture
# instead of the operator's real .env (which must never be rewritten by a test).
ENV_FILE="${ENV_FILE:-.env}"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found. Run: make init"

getenv() { env_file_get "$1" "$ENV_FILE" 2>/dev/null || printf '%s' "${2:-}"; }

export PATH="$HOME/.local/bin:$PATH"
HERMES_HOME="$(getenv HERMES_HOME "$HOME/.hermes")"
export HERMES_HOME

# ------------------------------------------------------------ 1. Hermes ---
if ! have hermes; then
  if [[ "${HERMES_SKIP_INSTALL:-0}" == "1" ]]; then
    die "hermes not found and HERMES_SKIP_INSTALL=1"
  fi
  log "installing Hermes (official installer)"
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
  have hermes || die "hermes still not on PATH; open a new shell and re-run: make hermes-install"
fi
log "hermes: $(hermes --version 2>/dev/null | head -n1 || echo present)"
mkdir -p "$HERMES_HOME" "$HERMES_HOME/memories" "$HERMES_HOME/skills"

# --------------------------------------------------------- 2. config.yaml ---
HERMES_MODEL="$(getenv HERMES_MODEL gpt-5.6-luna)"; export HERMES_MODEL
HERMES_CODING_MODEL="$(getenv HERMES_CODING_MODEL opus)"; export HERMES_CODING_MODEL
CLIPROXY_BASE_URL="http://127.0.0.1:$(getenv CLIPROXY_PORT 8317)/v1"; export CLIPROXY_BASE_URL
CLAUDE_MAX_PROXY_BASE_URL="http://127.0.0.1:$(getenv CLAUDE_MAX_PROXY_PORT 3456)/v1"; export CLAUDE_MAX_PROXY_BASE_URL
REPOS_DIR="$(getenv REPOS_DIR /opt/repos)"; export REPOS_DIR
# Local Ollama (OpenAI-compatible) — used by the model-router plugin for private
# and auxiliary work, and as the tail of the fallback chains. These render into
# config.yaml even when Ollama is not yet host-reachable; until then private work
# fails closed (refuses) and the fallback tail is simply inert. See
# docs/model-routing.md for the readiness steps (publish the port, pull a chat model).
OLLAMA_BASE_URL="$(getenv OLLAMA_BASE_URL "http://127.0.0.1:$(getenv OLLAMA_PORT 11434)/v1")"; export OLLAMA_BASE_URL
OLLAMA_CHAT_MODEL="$(getenv OLLAMA_CHAT_MODEL llama3.1:8b)"; export OLLAMA_CHAT_MODEL
# Auxiliary (title/compression) routing. Default AUX_PROVIDER=auto with an empty
# AUX_MODEL/AUX_BASE_URL means "use the main provider + model" (with the top-level
# fallback policy), so install never breaks when Ollama is absent. Per Hermes
# 0.21.2, a non-empty base_url routes to that endpoint and the provider field is
# ignored — so to keep these cheap calls OFF the cloud, set AUX_BASE_URL to your
# Ollama endpoint and a pulled AUX_MODEL (AUX_PROVIDER is then irrelevant).
AUX_PROVIDER="$(getenv AUX_PROVIDER auto)"; export AUX_PROVIDER
AUX_MODEL="$(getenv AUX_MODEL "")"; export AUX_MODEL
AUX_BASE_URL="$(getenv AUX_BASE_URL "")"; export AUX_BASE_URL
# model-router plugin knobs. Both default to true when unset in .env:
#   - complexity_routing: rewrite each request to the cheapest model that fits.
#   - privacy_routing:     the fail-closed guard that keeps private data off every
#     cloud provider (local Ollama or refuse). Setting MODEL_ROUTER_PRIVACY=false
#     is a deliberate, risky opt-out that disables that guard entirely; leave it
#     on unless you fully understand the tradeoff. When on, it stays fail-closed:
#     a private request never auto-escalates to the cloud on a local outage.
# Explicit false is preserved verbatim (not forced back to true by --force).
MODEL_ROUTER_COMPLEXITY="$(getenv MODEL_ROUTER_COMPLEXITY true)"; export MODEL_ROUTER_COMPLEXITY
MODEL_ROUTER_PRIVACY="$(getenv MODEL_ROUTER_PRIVACY true)"; export MODEL_ROUTER_PRIVACY

cfg="$HERMES_HOME/config.yaml"
if [[ -f "$cfg" && "$FORCE" != "1" ]]; then
  render_template hermes/config.yaml.tmpl "$cfg.agent-ops-kit"
  warn "config.yaml exists; wrote the kit version to $cfg.agent-ops-kit"
  warn "compare with: diff $cfg $cfg.agent-ops-kit   (or re-run with --force)"
else
  [[ -f "$cfg" ]] && cp -f "$cfg" "$cfg.bak.$(date +%Y%m%d%H%M%S)"
  render_template hermes/config.yaml.tmpl "$cfg"
  chmod 600 "$cfg"
  log "wrote $cfg"
fi

# ----------------------------------------------------------------- 3. .env ---
henv="$HERMES_HOME/.env"
touch "$henv"; chmod 600 "$henv"
for key in DISCORD_BOT_TOKEN DISCORD_ALLOWED_USERS DISCORD_HOME_CHANNEL DISCORD_FREE_RESPONSE_CHANNELS \
           CLIPROXY_API_KEY BROWSER_USE_API_KEY; do
  env_file_set "$key" "$(getenv "$key")" "$henv"
done
env_file_set DISCORD_REQUIRE_MENTION true "$henv"
# Delegation sends this as the bearer to claude-max-proxy; it must match the
# key rendered into proxy.env. Falls back to "local" only for a legacy .env with
# no key (the proxy then runs unauthenticated on loopback and accepts any string).
env_file_set CLAUDE_MAX_PROXY_API_KEY "$(getenv CLAUDE_MAX_PROXY_API_KEY local)" "$henv"
log "wrote $henv"
[[ -n "$(getenv DISCORD_BOT_TOKEN)" ]] || warn "DISCORD_BOT_TOKEN is empty; the Discord gateway will not start until you set it in .env and re-run"
[[ -n "$(getenv DISCORD_ALLOWED_USERS)" ]] || warn "DISCORD_ALLOWED_USERS is empty; nobody can talk to the bot yet"

# ----------------------------------------------------------- 4. honcho.json ---
HONCHO_BASE_URL="http://127.0.0.1:$(getenv HONCHO_PORT 8000)"; export HONCHO_BASE_URL
HONCHO_PEER_NAME="$(getenv HONCHO_PEER_NAME me)"; export HONCHO_PEER_NAME
HONCHO_WORKSPACE="$(getenv HONCHO_WORKSPACE agent-ops)"; export HONCHO_WORKSPACE
hj="$HERMES_HOME/honcho.json"
[[ -f "$hj" ]] && cp -f "$hj" "$hj.bak.$(date +%Y%m%d%H%M%S)"
render_template hermes/honcho.json.tmpl "$hj"
chmod 600 "$hj"
log "wrote $hj (self-hosted Honcho at $HONCHO_BASE_URL)"

# Honcho plugin dependency (what `hermes memory setup honcho` would install).
venv_py="$HERMES_HOME/hermes-agent/venv/bin/python"
if [[ -x "$venv_py" ]]; then
  if ! "$venv_py" -c 'import honcho' >/dev/null 2>&1; then
    log "installing honcho-ai into the Hermes venv"
    if have uv; then uv pip install --python "$venv_py" -q honcho-ai || "$venv_py" -m pip install -q honcho-ai
    else "$venv_py" -m pip install -q honcho-ai; fi
  fi
else
  warn "Hermes venv not found at $venv_py; run 'hermes memory setup honcho' once to install its dependency"
fi

# ----------------------------------------------------------- 5. persona ---
soul="$HERMES_HOME/SOUL.md"
if [[ ! -f "$soul" || "$FORCE" == "1" ]] || grep -q '^# SOUL.md - Who You Are' "$soul" 2>/dev/null; then
  [[ -f "$soul" ]] && cp -f "$soul" "$soul.bak.$(date +%Y%m%d%H%M%S)"
  cp -f hermes/SOUL.md "$soul"
  log "wrote $soul"
else
  log "keeping existing $soul (custom); kit version: $ROOT/hermes/SOUL.md"
fi
if [[ ! -f "$HERMES_HOME/memories/USER.md" ]]; then
  cp -f hermes/USER.md "$HERMES_HOME/memories/USER.md"
  log "seeded $HERMES_HOME/memories/USER.md"
fi

# ------------------------------------------------------------ 6. skills ---
for d in hermes/skills/*/; do
  name="$(basename "$d")"
  mkdir -p "$HERMES_HOME/skills/agent-ops-kit/$name"
  cp -rf "$d". "$HERMES_HOME/skills/agent-ops-kit/$name/"
done
log "installed skills: $(ls hermes/skills | tr '\n' ' ')"

# ------------------------------------------------------- 6b. plugins ---
# Bundled Hermes plugins ship in hermes/plugins/<name>/ and install into
# $HERMES_HOME/plugins/<name>/ where Hermes discovers them. Enablement is in
# config.yaml (plugins.enabled), rendered above.
if [[ -d hermes/plugins ]]; then
  mkdir -p "$HERMES_HOME/plugins"
  for d in hermes/plugins/*/; do
    name="$(basename "$d")"
    dest="$HERMES_HOME/plugins/$name"
    mkdir -p "$dest"
    cp -rf "$d". "$dest/"
    # Drop any test bytecode that may have been produced in the source tree.
    rm -rf "$dest/__pycache__" 2>/dev/null || true
  done
  log "installed plugins: $(ls hermes/plugins | tr '\n' ' ')"
fi

# ------------------------------------------------------------ 7. gateway ---
if [[ "${HERMES_SKIP_GATEWAY:-0}" != "1" ]]; then
  if [[ -n "$(getenv DISCORD_BOT_TOKEN)" ]]; then
    if have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
      log "installing the gateway as a user service"
      hermes gateway install || warn "hermes gateway install failed; start manually with: hermes gateway"
      hermes gateway restart >/dev/null 2>&1 || hermes gateway start >/dev/null 2>&1 || true
      if have loginctl; then
        loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes' || \
          warn "run once so the bot survives logout:  sudo loginctl enable-linger $USER"
      fi
    else
      warn "no systemd user session; start the bot with: hermes gateway   (or a tmux session)"
    fi
  fi
fi

# ------------------------------------------------------------- 8. doctor ---
log "running hermes doctor"
hermes doctor || warn "hermes doctor reported issues (see above)"
hermes memory status 2>/dev/null || true

cat <<MSG

Hermes is configured.
  Orchestrator model : $HERMES_MODEL via $CLIPROXY_BASE_URL
  Coding subagents   : $HERMES_CODING_MODEL via $CLAUDE_MAX_PROXY_BASE_URL
  Memory             : Honcho at $HONCHO_BASE_URL (workspace $HONCHO_WORKSPACE)
  Home               : $HERMES_HOME

Try it:   hermes chat            (terminal)
          @mention the bot in Discord
Profiles: make hermes-profile NAME=marketing     (see hermes/profiles/README.md)
MSG
