#!/usr/bin/env bash
# browseros-neo.sh — run the BrowserOS neo pilot (docker/browseros-neo) safely.
#
#   browseros-neo.sh check      render the Compose config and refuse anything unsafe
#   browseros-neo.sh up         check, start Neo, then open the agent path (unless paused)
#   browseros-neo.sh down       stop and remove the containers (the profile folder stays)
#   browseros-neo.sh status     containers, published bindings, agent path open/paused
#   browseros-neo.sh pause      close the agent's MCP path so a human can take over
#   browseros-neo.sh resume     reopen the agent's MCP path
#   browseros-neo.sh access     print how to reach the viewer from another machine
#   browseros-neo.sh hermes-snippet   print the Hermes mcp_servers entry
#
# Settings (environment): BROWSEROS_NEO_DATA (profile folder, default
# ~/.local/share/agent-ops-kit/browseros-neo/data), BROWSEROS_NEO_VNC_PORT
# (16080), BROWSEROS_NEO_MCP_PORT (19010), BROWSEROS_NEO_PROJECT (browseros-neo).
# docs/browseros-neo.md explains what pause does and does not stop.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"

COMPOSE_DIR="$ROOT/docker/browseros-neo"
CHECKER="$ROOT/scripts/lib/browseros_neo_check.py"
IMAGE="ghcr.io/browseros-ai/neo:0.52.1.2-d32f6ab@sha256:9f6302905f4db0d7d58a13e00b74cfc5c4448c5a7517a36700581a48e102d72e"
MIN_DOCKER_MAJOR=28   # 28.0 stopped other LAN hosts reaching 127.0.0.1-published ports
DOCKER="${BROWSEROS_NEO_DOCKER:-docker}"

PROJECT="${BROWSEROS_NEO_PROJECT:-browseros-neo}"
DATA="${BROWSEROS_NEO_DATA:-$HOME/.local/share/agent-ops-kit/browseros-neo/data}"
VNC_PORT="${BROWSEROS_NEO_VNC_PORT:-16080}"
MCP_PORT="${BROWSEROS_NEO_MCP_PORT:-19010}"

[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || die "BROWSEROS_NEO_PROJECT must match [a-z0-9][a-z0-9_-]*"
for p in "$VNC_PORT" "$MCP_PORT"; do
  [[ "$p" =~ ^[1-9][0-9]{3,4}$ ]] && (( p >= 1024 && p <= 65535 )) || die "ports must be 1024-65535 (got $p)"
done
[[ "$VNC_PORT" != "$MCP_PORT" ]] || die "BROWSEROS_NEO_VNC_PORT and BROWSEROS_NEO_MCP_PORT must differ"
[[ "$DATA" = /* ]] || die "BROWSEROS_NEO_DATA must be an absolute path"
case "$DATA" in "$ROOT"|"$ROOT"/*) die "BROWSEROS_NEO_DATA must be outside the repository" ;; esac
STATE_DIR="$(dirname "$DATA")/state-$PROJECT"
PAUSED_MARK="$STATE_DIR/agent-paused"

# The Compose invocation never reads a stray .env, COMPOSE_FILE or profile;
# what `check` validates is exactly what `up` runs.
compose() {
  env -u COMPOSE_FILE -u COMPOSE_PROFILES -u COMPOSE_PROJECT_NAME -u COMPOSE_ENV_FILES \
    BROWSEROS_NEO_DATA="$DATA" BROWSEROS_NEO_VNC_PORT="$VNC_PORT" BROWSEROS_NEO_MCP_PORT="$MCP_PORT" \
    BROWSEROS_NEO_UID="$(id -u)" BROWSEROS_NEO_GID="$(id -g)" \
    "$DOCKER" compose --env-file /dev/null -p "$PROJECT" --project-directory "$COMPOSE_DIR" \
    -f "$COMPOSE_DIR/compose.yaml" "$@"
}

wall_timeout() {
  python3 -I -c 'import sys,tomllib; print(int(tomllib.load(open(sys.argv[1],"rb"))["run"]["wall_timeout_seconds"]))' \
    "$COMPOSE_DIR/neo.toml"
}

port_open() {  # port_open PORT -> 0 when 127.0.0.1:PORT accepts a TCP connection
  python3 -I -c 'import socket,sys
s=socket.socket(); s.settimeout(2)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1])))==0 else 1)' "$1"
}

check_engine() {
  [[ -z "${DOCKER_HOST:-}" || "${DOCKER_HOST}" == unix://* ]] \
    || die "DOCKER_HOST=$DOCKER_HOST is not a local socket; 127.0.0.1 would mean another machine"
  local ctx; ctx="$("$DOCKER" context show 2>/dev/null || echo default)"
  local ep; ep="$("$DOCKER" context inspect "$ctx" --format '{{.Endpoints.docker.Host}}' 2>/dev/null || echo unix://)"
  [[ "$ep" == unix://* ]] || die "docker context '$ctx' points at $ep, not a local socket"
  local ver; ver="$("$DOCKER" version --format '{{.Server.Version}}' 2>/dev/null)" || die "Docker engine unreachable"
  [[ "${ver%%.*}" =~ ^[0-9]+$ ]] && (( ${ver%%.*} >= MIN_DOCKER_MAJOR )) \
    || die "Docker engine $ver is older than $MIN_DOCKER_MAJOR.0; loopback-published ports may be reachable from the LAN"
}

check_data() {
  [[ -d "$DATA" && ! -L "$DATA" ]] || die "profile folder $DATA is missing or a symlink (run 'up' to create it)"
  local owner mode; owner="$(stat -c %u "$DATA")"; mode="$(stat -c %a "$DATA")"
  [[ "$owner" == "$(id -u)" ]] || die "profile folder $DATA is owned by uid $owner, not $(id -u)"
  (( (8#$mode & 8#077) == 0 )) || die "profile folder $DATA has mode $mode; group/other access must be 0 (chmod 700)"
  # One container per profile: another container on this folder corrupts it.
  local others
  others="$("$DOCKER" ps -a --filter "volume=$DATA" --format '{{.Names}} {{.Label "com.docker.compose.project"}}' \
    | awk -v p="$PROJECT" '$2 != p { print $1 }')"
  [[ -z "$others" ]] || die "profile folder $DATA is already mounted by: $others"
}

cmd_check() {
  check_engine
  check_data
  local cfg; cfg="$(compose config --format json)" || die "compose config failed"
  python3 -I "$CHECKER" --data "$DATA" --image "$IMAGE" --vnc-port "$VNC_PORT" --mcp-port "$MCP_PORT" <<<"$cfg" \
    || die "rendered Compose config refused (see above)"
  log "config ok: project=$PROJECT viewer=127.0.0.1:$VNC_PORT mcp=127.0.0.1:$MCP_PORT data=$DATA"
}

verify_bindings() {  # every published port of this project is on 127.0.0.1, live
  local ids bad
  ids="$(compose ps -q)"
  [[ -n "$ids" ]] || return 0
  # shellcheck disable=SC2086
  bad="$("$DOCKER" inspect $ids --format '{{.Name}}{{range $p, $b := .NetworkSettings.Ports}}{{range $b}} {{$p}}={{.HostIp}}{{end}}{{end}}' \
    | tr ' ' '\n' | grep '=' | grep -v '=127\.0\.0\.1$' || true)"
  [[ -z "$bad" ]] || { compose stop mcp-gate >/dev/null 2>&1 || true; die "non-loopback binding found, agent path closed: $bad"; }
}

cmd_up() {
  ( umask 077; mkdir -p "$DATA" "$STATE_DIR" )
  cmd_check
  "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1 || compose pull neo
  compose up -d --wait --wait-timeout "${BROWSEROS_NEO_WAIT:-240}" neo
  if [[ -e "$PAUSED_MARK" ]]; then
    compose stop mcp-gate >/dev/null 2>&1 || true
    log "agent path stays PAUSED ($PAUSED_MARK); run 'resume' to reopen it"
  else
    compose up -d --no-deps mcp-gate
  fi
  verify_bindings
  cmd_status
}

cmd_down() {
  compose down --remove-orphans
  log "stopped; profile kept in $DATA"
}

gate_running() { [[ -n "$(compose ps -q --status running mcp-gate 2>/dev/null)" ]]; }

cmd_status() {
  compose ps
  verify_bindings
  if [[ -e "$PAUSED_MARK" ]]; then log "agent path: PAUSED (human has the browser)"
  elif gate_running; then log "agent path: OPEN at http://127.0.0.1:$MCP_PORT/mcp"
  else log "agent path: CLOSED (gate not running)"; fi
}

cmd_pause() {
  ( umask 077; mkdir -p "$STATE_DIR" ); : > "$PAUSED_MARK"
  compose stop -t 2 mcp-gate >/dev/null
  if port_open "$MCP_PORT"; then die "127.0.0.1:$MCP_PORT still accepts connections; do NOT type secrets"; fi
  log "agent path closed: 127.0.0.1:$MCP_PORT refuses connections"
  if [[ "${1:-}" != "--no-wait" ]]; then
    local w; w=$(( $(wall_timeout) + 5 ))
    log "waiting ${w}s for any browser script already in flight to hit its run budget..."
    sleep "$w"
  fi
  log "PAUSED. The browser is yours in the viewer; run 'resume' when done."
}

cmd_resume() {
  [[ -n "$(compose ps -q --status running neo)" ]] || die "neo is not running; use 'up'"
  # The paused marker goes only once the gate is verified, so a failed resume
  # leaves `up` keeping the agent path closed.
  compose up -d --no-deps mcp-gate >/dev/null
  verify_bindings
  local _
  for _ in $(seq 1 20); do
    if port_open "$MCP_PORT"; then
      rm -f "$PAUSED_MARK"
      log "agent path open: http://127.0.0.1:$MCP_PORT/mcp"
      return 0
    fi
    sleep 0.5
  done
  compose stop -t 2 mcp-gate >/dev/null 2>&1 || true
  die "gate started but 127.0.0.1:$MCP_PORT does not accept connections; agent path left closed"
}

cmd_access() {
  cat <<EOF
The viewer has no password; it is published on this host's 127.0.0.1 only.
From your own machine, tunnel it over SSH (SSH is the authentication):

  ssh -N -L ${VNC_PORT}:127.0.0.1:${VNC_PORT} <you>@<this-host>

then open http://127.0.0.1:${VNC_PORT}/ locally. Before signing in to anything:

  scripts/browseros-neo.sh pause     # cut the agent off, wait out in-flight runs
  ...sign in, finish MFA, close the tunnel...
  scripts/browseros-neo.sh resume    # give the browser back to the agent
EOF
}

cmd_hermes_snippet() {
  sed "s/__BROWSEROS_NEO_MCP_PORT__/$MCP_PORT/" "$ROOT/hermes/mcp/browseros-neo.yaml"
}

case "${1:-}" in
  check) cmd_check ;;
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  pause) shift; cmd_pause "${1:-}" ;;
  resume) cmd_resume ;;
  access) cmd_access ;;
  hermes-snippet) cmd_hermes_snippet ;;
  *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
