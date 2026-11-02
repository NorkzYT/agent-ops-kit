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
#        scripts/hermes-install.sh --render-only OUT   (render config.yaml to OUT and stop: no
#                                                       hermes, no writes under HERMES_HOME)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

FORCE=0
RENDER_ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1 ;;
    --render-only) RENDER_ONLY="${2:-}"; [[ -n "$RENDER_ONLY" ]] || die "usage: $0 --render-only OUT"; shift ;;
    *) die "unknown argument: $1 (usage: $0 [--force] | --render-only OUT)" ;;
  esac
  shift
done

# ENV_FILE defaults to ./.env; overridable so tests can point at a fixture
# instead of the operator's real .env (which must never be rewritten by a test).
ENV_FILE="${ENV_FILE:-.env}"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found. Run: make init"

getenv() { env_file_get "$1" "$ENV_FILE" 2>/dev/null || printf '%s' "${2:-}"; }

export PATH="$HOME/.local/bin:$PATH"
HERMES_HOME="$(getenv HERMES_HOME "$HOME/.hermes")"
export HERMES_HOME

# ------------------------------------------------------------ 1. Hermes ---
if [[ -n "$RENDER_ONLY" ]]; then
  :  # render-only: hermes is not needed
elif ! have hermes; then
  if [[ "${HERMES_SKIP_INSTALL:-0}" == "1" ]]; then
    die "hermes not found and HERMES_SKIP_INSTALL=1"
  fi
  log "installing Hermes (official installer)"
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
  have hermes || die "hermes still not on PATH; open a new shell and re-run: make hermes-install"
fi
if [[ -z "$RENDER_ONLY" ]]; then
  log "hermes: $(hermes --version 2>/dev/null | head -n1 || echo present)"
  mkdir -p "$HERMES_HOME" "$HERMES_HOME/memories" "$HERMES_HOME/skills"
fi

# --------------------------------------------------------- 2. config.yaml ---
HERMES_MODEL="$(getenv HERMES_MODEL gpt-6.1-terra)"; export HERMES_MODEL
HERMES_CODING_MODEL="$(getenv HERMES_CODING_MODEL opus)"; export HERMES_CODING_MODEL
CLIPROXY_BASE_URL="http://127.0.0.1:$(getenv CLIPROXY_PORT 8317)/v1"; export CLIPROXY_BASE_URL
CLAUDE_MAX_PROXY_BASE_URL="http://127.0.0.1:$(getenv CLAUDE_MAX_PROXY_PORT 3456)/v1"; export CLAUDE_MAX_PROXY_BASE_URL
REPOS_DIR="$(getenv REPOS_DIR /opt/repos)"; export REPOS_DIR
cfg="$HERMES_HOME/config.yaml"

# Local Ollama (OpenAI-compatible) — used by the model-router plugin for private
# and auxiliary work, and as the tail of the fallback chains. These render into
# config.yaml even when Ollama is not yet host-reachable; until then private work
# fails closed (refuses) and the fallback tail is simply inert. See
# docs/model-routing.md for the readiness steps (publish the port, pull a chat model).
OLLAMA_BASE_URL="$(getenv OLLAMA_BASE_URL "http://127.0.0.1:$(getenv OLLAMA_PORT 11434)/v1")"; export OLLAMA_BASE_URL
# Explicit local model roles (docs/model-routing.md, Local model roles), all on
# OLLAMA_BASE_URL:
#   routine  — ordinary private turns, title/compression, specialist classifier
#   fallback — tried once when a role model fails (and the last native fallback hop)
#   agent    — difficult private turns, private coding, the native fallback agent
# OLLAMA_CHAT_MODEL is the legacy single-model knob. When it is set, it is the
# default for routine and agent and there is no default fallback, so an
# upgraded single-model install keeps answering on the model it has pulled.
LEGACY_CHAT_MODEL="$(getenv OLLAMA_CHAT_MODEL "")"
OLLAMA_ROUTINE_MODEL="$(getenv OLLAMA_ROUTINE_MODEL "${LEGACY_CHAT_MODEL:-qwen3.5:27b}")"; export OLLAMA_ROUTINE_MODEL
OLLAMA_AGENT_MODEL="$(getenv OLLAMA_AGENT_MODEL "${LEGACY_CHAT_MODEL:-qwen3.8:27b}")"; export OLLAMA_AGENT_MODEL
if [[ -n "$LEGACY_CHAT_MODEL" ]]; then
  OLLAMA_FALLBACK_MODEL="$(getenv OLLAMA_FALLBACK_MODEL "")"
else
  OLLAMA_FALLBACK_MODEL="$(getenv OLLAMA_FALLBACK_MODEL qwen3.6:27b)"
fi
export OLLAMA_FALLBACK_MODEL
if [[ -n "$LEGACY_CHAT_MODEL" ]] && ! env_file_get OLLAMA_ROUTINE_MODEL "$ENV_FILE" >/dev/null; then
  warn "OLLAMA_CHAT_MODEL=$LEGACY_CHAT_MODEL names the routine/agent models; set OLLAMA_ROUTINE_MODEL," \
       "OLLAMA_FALLBACK_MODEL and OLLAMA_AGENT_MODEL for the local hierarchy (docs/model-routing.md)"
fi
# Auxiliary (title/compression) routing. AUX_BASE_URL empty: provider auto =
# inherit the main provider + model (install never breaks when Ollama is absent).
# AUX_BASE_URL set: rendered as provider custom, because Hermes 0.21.2 resolves
# `provider: auto` + base_url WITHOUT an api_key back to the main (cloud)
# provider. The model defaults to the routine role and the fallback role is
# rendered as auxiliary.<task>.fallback_chain on the same endpoint.
AUX_BASE_URL="$(getenv AUX_BASE_URL "")"; export AUX_BASE_URL
AUX_PROVIDER="$(getenv AUX_PROVIDER auto)"
if [[ -n "$AUX_BASE_URL" && "$AUX_PROVIDER" == "auto" ]]; then AUX_PROVIDER=custom; fi
export AUX_PROVIDER
if [[ -n "$AUX_BASE_URL" ]]; then
  AUX_MODEL="$(getenv AUX_MODEL "$OLLAMA_ROUTINE_MODEL")"
  AUX_FALLBACK_MODEL="$(getenv AUX_FALLBACK_MODEL "$OLLAMA_FALLBACK_MODEL")"
else
  AUX_MODEL="$(getenv AUX_MODEL "")"
  AUX_FALLBACK_MODEL=""
fi
export AUX_MODEL
# YAML flow list for auxiliary.<task>.fallback_chain. The double quotes are
# literal YAML: a model id like qwen3.6:27b holds a colon.
aux_fallback_chain() {
  [[ -n "$AUX_FALLBACK_MODEL" && "$AUX_FALLBACK_MODEL" != "$AUX_MODEL" ]] || { echo "[]"; return; }
  printf '[{provider: custom, model: "%s", base_url: "%s"}]' "$AUX_FALLBACK_MODEL" "$AUX_BASE_URL"
}
AUX_FALLBACK_CHAIN="$(aux_fallback_chain)"; export AUX_FALLBACK_CHAIN
# Compression route. AUX_COMPRESSION_ROUTE=cliproxy (default): CLIProxyAPI,
# AUX_COMPRESSION_MODEL (gpt-6.1-terra) then AUX_COMPRESSION_FALLBACK_MODEL
# (gpt-5.6-luna, `none` = no fallback) on the same proxy. The key stays in .env
# (${CLIPROXY_API_KEY} / key_env). The model-router plugin's aux guard refuses
# #privacy/#private payloads on this route. AUX_COMPRESSION_ROUTE=aux: the
# title-generation route above (AUX_BASE_URL or the main provider).
AUX_COMPRESSION_ROUTE="$(getenv AUX_COMPRESSION_ROUTE cliproxy)"
case "$AUX_COMPRESSION_ROUTE" in
  cliproxy|aux) ;;
  *) warn "AUX_COMPRESSION_ROUTE=$AUX_COMPRESSION_ROUTE is not cliproxy or aux; using cliproxy"
     AUX_COMPRESSION_ROUTE=cliproxy ;;
esac
if [[ "$AUX_COMPRESSION_ROUTE" == "cliproxy" ]]; then
  AUX_COMPRESSION_PROVIDER=custom; AUX_COMPRESSION_BASE_URL="$CLIPROXY_BASE_URL"
  AUX_COMPRESSION_API_KEY='${CLIPROXY_API_KEY}'
  AUX_COMPRESSION_MODEL="$(getenv AUX_COMPRESSION_MODEL gpt-6.1-terra)"
  aux_comp_fb="$(getenv AUX_COMPRESSION_FALLBACK_MODEL gpt-5.6-luna)"
  if [[ "$aux_comp_fb" != "none" && "$aux_comp_fb" != "$AUX_COMPRESSION_MODEL" ]]; then
    AUX_COMPRESSION_FALLBACK_CHAIN="$(printf '[{provider: custom, model: "%s", base_url: "%s", key_env: CLIPROXY_API_KEY}]' \
      "$aux_comp_fb" "$CLIPROXY_BASE_URL")"
  else
    AUX_COMPRESSION_FALLBACK_CHAIN="[]"
  fi
else
  AUX_COMPRESSION_PROVIDER="$AUX_PROVIDER"; AUX_COMPRESSION_MODEL="$AUX_MODEL"
  AUX_COMPRESSION_BASE_URL="$AUX_BASE_URL"; AUX_COMPRESSION_API_KEY=""
  AUX_COMPRESSION_FALLBACK_CHAIN="$AUX_FALLBACK_CHAIN"
fi
export AUX_COMPRESSION_ROUTE AUX_COMPRESSION_PROVIDER AUX_COMPRESSION_MODEL AUX_COMPRESSION_BASE_URL \
  AUX_COMPRESSION_API_KEY AUX_COMPRESSION_FALLBACK_CHAIN
# Compression: never high effort. A local route turns thinking off with a
# top-level reasoning_effort none in extra_body (Ollama ignores the `reasoning`
# object the reasoning_effort key renders, and that object would override it)
# and gets wait budgets sized for local prefill (config.yaml.tmpl, compression).
if [[ "$AUX_COMPRESSION_ROUTE" == "aux" && -n "$AUX_BASE_URL" ]]; then
  AUX_COMPRESSION_EFFORT='""'; AUX_COMPRESSION_EXTRA_BODY='{reasoning_effort: none}'
  AUX_COMPRESSION_TIMEOUT=600; COMPRESSION_IDLE_SECONDS=420; COMPRESSION_CEILING_SECONDS=900
else
  AUX_COMPRESSION_EFFORT=low; AUX_COMPRESSION_EXTRA_BODY='{}'
  AUX_COMPRESSION_TIMEOUT=120; COMPRESSION_IDLE_SECONDS=120; COMPRESSION_CEILING_SECONDS=600
fi
export AUX_COMPRESSION_EFFORT AUX_COMPRESSION_EXTRA_BODY AUX_COMPRESSION_TIMEOUT \
  COMPRESSION_IDLE_SECONDS COMPRESSION_CEILING_SECONDS
# model-router plugin knobs. Both default to true when unset in .env:
#   - complexity_routing: rewrite each request to the cheapest model that fits.
#   - privacy_routing:     the fail-closed guard that keeps a #privacy-marked
#     request off every cloud provider (local Ollama or refuse). Setting
#     MODEL_ROUTER_PRIVACY=false is a deliberate opt-out that disables the guard
#     (and #privacy) entirely. When on, it stays fail-closed: a marked request
#     never auto-escalates to the cloud on a local outage.
# Explicit false is preserved verbatim (not forced back to true by --force).
MODEL_ROUTER_COMPLEXITY="$(getenv MODEL_ROUTER_COMPLEXITY true)"; export MODEL_ROUTER_COMPLEXITY
MODEL_ROUTER_PRIVACY="$(getenv MODEL_ROUTER_PRIVACY true)"; export MODEL_ROUTER_PRIVACY
# Balanced-routing knobs (same preserve-explicit-value rule):
#   - MODEL_ROUTER_EFFORT: rewrite reasoning-effort fields already present on
#     managed orchestrator requests per task (never above the config ceiling).
#   - MODEL_ROUTER_CODING_EFFORT: per-task Claude effort on managed coding
#     requests (medium/high/xhigh; max only with #max/#astra).
#   - MODEL_ROUTER_CODING_EFFORT_CEILING: low|medium|high|xhigh|max (default max).
#   - MODEL_ROUTER_ASTRA: task (default; leading standalone #max/#astra line in
#     the current user task authorizes gpt-6-astra) | always (legacy) | never.
#   - MODEL_ROUTER_TASK_TTL: seconds a session's current-task state (and its
#     Astra authorization) stays live without a substantive follow-up.
MODEL_ROUTER_EFFORT="$(getenv MODEL_ROUTER_EFFORT true)"; export MODEL_ROUTER_EFFORT
MODEL_ROUTER_CODING_EFFORT="$(getenv MODEL_ROUTER_CODING_EFFORT true)"; export MODEL_ROUTER_CODING_EFFORT
MODEL_ROUTER_CODING_EFFORT_CEILING="$(getenv MODEL_ROUTER_CODING_EFFORT_CEILING max)"; export MODEL_ROUTER_CODING_EFFORT_CEILING
MODEL_ROUTER_ASTRA="$(getenv MODEL_ROUTER_ASTRA task)"; export MODEL_ROUTER_ASTRA
MODEL_ROUTER_TASK_TTL="$(getenv MODEL_ROUTER_TASK_TTL 1800)"; export MODEL_ROUTER_TASK_TTL

# Specialist router (docs/specialist-routing.md). Presence-aware: a value set in
# .env wins (explicit false stays false); a key absent from .env keeps the value
# already in $HERMES_HOME/config.yaml (what `make specialist-install` wrote), so
# --force never resets a live setting; only a fresh install gets the default.
#   MODEL_ROUTER_SPECIALISTS                   true|false        (default false)
#   MODEL_ROUTER_SPECIALIST_MAX                0-15              (default 4)
#   MODEL_ROUTER_SPECIALIST_REQUIRE_READY      true|false        (default false)
#   MODEL_ROUTER_SPECIALIST_CLASSIFIER         none|local|cliproxy (default none)
#   MODEL_ROUTER_SPECIALIST_MODE               ai|hybrid|keywords (default ai: the classifier selects;
#                                              deterministic code enforces privacy/allowlist/readiness/caps)
#   MODEL_ROUTER_SPECIALIST_CLASSIFIER_TIMEOUT seconds, total    (default 10, max 25)
#   MODEL_ROUTER_SPECIALIST_PROFILES           a,b allowlist     (default all)
SETTINGS_KEY="plugins.entries.model-router.settings"
live_setting() { python3 "$ROOT/scripts/config-get.py" "$cfg" "$SETTINGS_KEY.$1" 2>/dev/null; }
norm_bool() {
  case "${1,,}" in
    1|true|yes|on) echo true ;;
    0|false|no|off) echo false ;;
    *) return 1 ;;
  esac
}
# yaml_list "a, b" -> ["a", "b"]: comma-separated, trimmed, double-quoted.
yaml_list() {
  local item out="" items=()
  IFS=',' read -ra items <<< "$1"
  for item in "${items[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"; item="${item%"${item##*[![:space:]]}"}"
    [[ -n "$item" ]] || continue
    item="${item//\\/\\\\}"; item="${item//\"/\\\"}"
    out+="${out:+, }\"$item\""
  done
  printf '[%s]' "$out"
}
# router_knob VAR KEY DEFAULT KIND -> sets/exports VAR and VAR_SOURCE.
router_knob() {
  local var="$1" key="$2" def="$3" kind="$4" raw="" src="" val=""
  if raw="$(env_file_get "$var" "$ENV_FILE")"; then src=".env"
  elif [[ -f "$cfg" ]] && raw="$(live_setting "$key")"; then src="live config"
  else raw="$def"; src="default"; fi
  case "$kind" in
    bool) val="$(norm_bool "$raw")" || { warn "$var=$raw ($src) is not a boolean; using $def"; val="$def"; src="default"; } ;;
    int) [[ "$raw" =~ ^[0-9]+$ ]] && (( 10#$raw <= 15 )) && val="$((10#$raw))" || { warn "$var=$raw ($src) is not 0-15; using $def"; val="$def"; src="default"; } ;;
    seconds) [[ "$raw" =~ ^[0-9]+([.][0-9]+)?$ ]] && val="$raw" || { warn "$var=$raw ($src) is not a number; using $def"; val="$def"; src="default"; } ;;
    classifier) raw="${raw,,}"; [[ -z "$raw" || "$raw" == "null" ]] && raw=none
                [[ "$raw" == none || "$raw" == local || "$raw" == cliproxy ]] && val="$raw" || { warn "$var=$raw ($src) is not none|local|cliproxy; using $def"; val="$def"; src="default"; } ;;
    mode) raw="${raw,,}"; [[ -z "$raw" || "$raw" == "null" ]] && raw="$def"
          [[ "$raw" == ai || "$raw" == hybrid || "$raw" == keywords ]] && val="$raw" || { warn "$var=$raw ($src) is not ai|hybrid|keywords; using $def"; val="$def"; src="default"; } ;;
    list) [[ "${raw// /}" == all ]] && raw=""; val="$(yaml_list "$raw")" ;;
  esac
  printf -v "$var" '%s' "$val"; export "${var?}"
  printf -v "${var}_SOURCE" '%s' "$src"
}
router_knob MODEL_ROUTER_SPECIALISTS specialist_routing false bool
router_knob MODEL_ROUTER_SPECIALIST_MAX specialist_max_profiles 4 int
router_knob MODEL_ROUTER_SPECIALIST_REQUIRE_READY specialist_require_ready false bool
router_knob MODEL_ROUTER_SPECIALIST_CLASSIFIER specialist_classifier none classifier
router_knob MODEL_ROUTER_SPECIALIST_MODE specialist_selection_mode ai mode
router_knob MODEL_ROUTER_SPECIALIST_CLASSIFIER_TIMEOUT specialist_classifier_timeout 10 seconds
router_knob MODEL_ROUTER_SPECIALIST_PROFILES specialist_profiles "" list
# Legacy privacy terms/paths follow the same rule, so --force never wipes them.
# Request routing ignores them (#privacy only). Values are never logged.
router_knob MODEL_ROUTER_PRIVATE_TERMS private_terms "" list
router_knob MODEL_ROUTER_PRIVATE_PATHS private_paths "" list
log "specialist_routing=$MODEL_ROUTER_SPECIALISTS ($MODEL_ROUTER_SPECIALISTS_SOURCE)" \
    "max=$MODEL_ROUTER_SPECIALIST_MAX ($MODEL_ROUTER_SPECIALIST_MAX_SOURCE)" \
    "classifier=$MODEL_ROUTER_SPECIALIST_CLASSIFIER ($MODEL_ROUTER_SPECIALIST_CLASSIFIER_SOURCE)" \
    "mode=$MODEL_ROUTER_SPECIALIST_MODE ($MODEL_ROUTER_SPECIALIST_MODE_SOURCE)"
[[ "$MODEL_ROUTER_SPECIALISTS" == "true" && "$MODEL_ROUTER_SPECIALIST_MODE" == "ai" && "$MODEL_ROUTER_SPECIALIST_CLASSIFIER" == "none" ]] && \
  warn "specialist_selection_mode=ai with specialist_classifier=none selects no specialist (fail-closed); set MODEL_ROUTER_SPECIALIST_CLASSIFIER=cliproxy or local"
[[ "$MODEL_ROUTER_PRIVACY" == "false" ]] && \
  warn "MODEL_ROUTER_PRIVACY=false: private data is NOT kept local in ordinary turns (docs/model-routing.md)"

# Dangerous-command approvals the operator answered "Always" to live in the top-level
# command_allowlist (pattern keys or command globs, docs: Hermes user-guide/security).
# They are a standing decision: keep them verbatim across --force instead of wiping
# them with the template. Entries holding a comma cannot round-trip through
# config-get.py's a,b list form and are reported, never dropped silently.
if [[ -f "$cfg" ]] && raw_allow="$(python3 "$ROOT/scripts/config-get.py" "$cfg" command_allowlist 2>/dev/null)"; then
  COMMAND_ALLOWLIST="$(yaml_list "$raw_allow")"
  [[ -n "$raw_allow" ]] && log "command_allowlist: keeping $(tr -cd ',' <<< "$raw_allow" | wc -c | tr -d ' ') + 1 live entr(ies)"
else
  COMMAND_ALLOWLIST="[]"
fi
export COMMAND_ALLOWLIST

if [[ -n "$RENDER_ONLY" ]]; then
  render_template hermes/config.yaml.tmpl "$RENDER_ONLY"
  chmod 600 "$RENDER_ONLY"
  log "rendered $RENDER_ONLY (render-only: nothing else written)"
  exit 0
fi

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
# Restarting the gateway from inside a Hermes turn kills that turn; the same
# markers scripts/specialist-activate.py checks make this step a no-op there.
in_hermes=""
for marker in _HERMES_GATEWAY HERMES_GATEWAY HERMES_GATEWAY_MODE HERMES_GATEWAY_SESSION HERMES_CRON_SESSION; do
  case "${!marker:-}" in ""|0|false|no|off) ;; *) in_hermes="$marker" ;; esac
done
if [[ -n "$in_hermes" && "${HERMES_SKIP_GATEWAY:-0}" != "1" ]]; then
  warn "$in_hermes is set (running inside Hermes): not restarting the gateway. Run in your own terminal: make hermes-restart"
elif [[ "${HERMES_SKIP_GATEWAY:-0}" != "1" ]]; then
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
