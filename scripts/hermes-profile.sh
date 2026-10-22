#!/usr/bin/env bash
# hermes-profile.sh <name> — create a Hermes profile from hermes/profiles/<name>.
#
#   $HERMES_HOME/profiles/<name>/            created with `hermes profile create --clone`
#   $HERMES_HOME/profiles/<name>/SOUL.md     from hermes/profiles/<name>/SOUL.md
#   $HERMES_HOME/profiles/<name>/.env        proxy key (+ optional Discord token)
#   $HERMES_HOME/profiles/<name>/knowledge/  drop .md/.txt books here, then /learn it
#
# Env: PROFILE_DISCORD_BOT_TOKEN  give this profile its own Discord bot (optional)
#      PROFILE_DISCORD_ALLOWED_USERS  defaults to the kit's DISCORD_ALLOWED_USERS
#      PROFILE_PEER_URL, PROFILE_PEER_KEY  worker lane relay (mac-worker, windows-worker):
#                      register the worker machine's own Hermes gateway (its api_server
#                      URL and API_SERVER_KEY) as this profile's peer; the key goes to the
#                      profile .env, never to argv. Optional PROFILE_PEER_NAME (default:
#                      <name> without "-worker", e.g. mac)
#      PROFILE_SSH_HOST, PROFILE_SSH_USER  run this profile's terminal on another
#                      machine over SSH (optional administration; worker lanes use the peer);
#                      optional PROFILE_SSH_PORT, PROFILE_SSH_KEY, PROFILE_SSH_CWD (default ~)
#      KNOWLEDGE_ROOT  where the profile knowledge corpora live (default data/knowledge)
#      PROFILES_DIR    profile seeds (default hermes/profiles; tests and knowledge-apply)
#      HERMES_HOME     Hermes home; when unset, read from ENV_FILE, else ~/.hermes
#      HERMES_BIN      hermes executable (default: hermes on PATH; tests use a stub)
#      ENV_FILE        dotenv to read keys from (default .env; overridable for tests)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

env_file="${ENV_FILE:-.env}"
name="${1:-}"
[[ -n "$name" ]] || die "usage: scripts/hermes-profile.sh <name>   (see hermes/profiles/)"
src="${PROFILES_DIR:-hermes/profiles}/$name"
[[ -f "$src/SOUL.md" ]] || die "no $src/SOUL.md. Copy hermes/profiles/_template to hermes/profiles/$name first."
[[ "$name" != "_template" ]] || die "copy _template to a real name first"
[[ -f "$env_file" ]] || die "$env_file not found. Run: make init"

export PATH="$HOME/.local/bin:$PATH"
HERMES_BIN="${HERMES_BIN:-hermes}"
have "$HERMES_BIN" || die "hermes not installed. Run: make hermes-install"
hermes() { "$HERMES_BIN" "$@"; }
if [[ -z "${HERMES_HOME:-}" ]]; then
  HERMES_HOME="$(env_file_get HERMES_HOME "$env_file" 2>/dev/null || echo "$HOME/.hermes")"
fi
export HERMES_HOME
pdir="$HERMES_HOME/profiles/$name"

if [[ -d "$pdir" ]]; then
  log "profile '$name' already exists at $pdir; refreshing SOUL.md and .env"
else
  log "creating profile '$name' (clone of the default config, skills and model settings)"
  hermes profile create "$name" --clone
fi
mkdir -p "$pdir/knowledge"

cp -f "$src/SOUL.md" "$pdir/SOUL.md"
log "installed $pdir/SOUL.md"

penv="$pdir/.env"
touch "$penv"; chmod 600 "$penv"
env_file_set CLIPROXY_API_KEY "$(env_file_get CLIPROXY_API_KEY "$env_file")" "$penv"
env_file_set CLAUDE_MAX_PROXY_API_KEY local "$penv"
bu="$(env_file_get BROWSER_USE_API_KEY "$env_file" 2>/dev/null || true)"
[[ -n "$bu" ]] && env_file_set BROWSER_USE_API_KEY "$bu" "$penv"
if [[ -n "${PROFILE_DISCORD_BOT_TOKEN:-}" ]]; then
  env_file_set DISCORD_BOT_TOKEN "$PROFILE_DISCORD_BOT_TOKEN" "$penv"
  env_file_set DISCORD_ALLOWED_USERS "${PROFILE_DISCORD_ALLOWED_USERS:-$(env_file_get DISCORD_ALLOWED_USERS "$env_file" 2>/dev/null || true)}" "$penv"
  env_file_set DISCORD_REQUIRE_MENTION true "$penv"
  log "profile has its own Discord bot; start it with:  $name gateway install"
fi

# Worker lane relay: the profile stays on this host (local terminal) and hands
# each card to the worker's own Hermes over its authenticated peer API
# (docs/discord-kanban-workflow.md).
if [[ -n "${PROFILE_PEER_URL:-}" ]]; then
  [[ -z "${PROFILE_SSH_HOST:-}" ]] || die "set PROFILE_PEER_URL or PROFILE_SSH_HOST, not both; worker lanes use the peer"
  [[ -n "${PROFILE_PEER_KEY:-}" ]] || die "PROFILE_PEER_URL needs PROFILE_PEER_KEY (the worker gateway's API_SERVER_KEY)"
  peer="${PROFILE_PEER_NAME:-${name%-worker}}"
  hermes -p "$name" peer add "$peer" --url "$PROFILE_PEER_URL" --note "kanban-workflow lane $name" >/dev/null
  peer_key_var="HERMES_PEER_$(tr '[:lower:]-' '[:upper:]_' <<<"$peer")_KEY"
  env_file_set "$peer_key_var" "$PROFILE_PEER_KEY" "$penv"
  hermes -p "$name" config set terminal.backend local >/dev/null
  log "peer '$peer' registered for $name (key in $penv as $peer_key_var); check: hermes -p $name peer list"
fi

# Optional: run this profile's terminal and file tools on another machine over SSH.
if [[ -n "${PROFILE_SSH_HOST:-}" ]]; then
  [[ -n "${PROFILE_SSH_USER:-}" ]] || die "PROFILE_SSH_HOST needs PROFILE_SSH_USER"
  hermes -p "$name" config set terminal.backend ssh >/dev/null
  hermes -p "$name" config set terminal.ssh_host "$PROFILE_SSH_HOST" >/dev/null
  hermes -p "$name" config set terminal.ssh_user "$PROFILE_SSH_USER" >/dev/null
  hermes -p "$name" config set terminal.cwd "${PROFILE_SSH_CWD:-~}" >/dev/null
  if [[ -n "${PROFILE_SSH_PORT:-}" ]]; then hermes -p "$name" config set terminal.ssh_port "$PROFILE_SSH_PORT" >/dev/null; fi
  if [[ -n "${PROFILE_SSH_KEY:-}" ]]; then hermes -p "$name" config set terminal.ssh_key "$PROFILE_SSH_KEY" >/dev/null; fi
  log "terminal runs on $PROFILE_SSH_USER@$PROFILE_SSH_HOST over SSH; check: hermes -p $name config get terminal.backend"
fi

# Profile-specific extras (optional files next to SOUL.md)
[[ -d "$src/skills" ]] && { mkdir -p "$pdir/skills"; cp -rf "$src/skills/." "$pdir/skills/"; log "copied profile skills"; }
# Repo-tracked seed knowledge is copied first; the shared knowledge root (below)
# is authoritative and refreshes any file it also provides.
[[ -d "$src/knowledge" ]] && { cp -rf "$src/knowledge/." "$pdir/knowledge/"; log "copied seed knowledge"; }

# Sync this profile's book/notes corpus from the shared knowledge root (a local
# dir or an SMB mount) into the profile's knowledge/ folder. Safe: copy-only,
# additive by default, never touches the source. Set KNOWLEDGE_SYNC_MODE=mirror
# to prune stale knowledge files from the destination.
KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$ROOT/data/knowledge}" \
  bash "$ROOT/scripts/knowledge-sync.sh" "$name" "$pdir/knowledge"

cat <<MSG

Profile '$name' is ready.
  Chat:        $name chat        (or: hermes -p $name chat)
  Knowledge:   books sync from ${KNOWLEDGE_ROOT:-$ROOT/data/knowledge}/$name into
               $pdir/knowledge/ on each run (refs/*.ref.md point at the shared
               canonical copy). Then: make knowledge-learn NAME=$name
  Kanban:      hermes kanban create "<task>" --assignee $name
  Own Discord: PROFILE_DISCORD_BOT_TOKEN=<token> make hermes-profile NAME=$name && $name gateway install
MSG
