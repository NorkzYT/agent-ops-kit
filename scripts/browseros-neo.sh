#!/usr/bin/env bash
# browseros-neo.sh — run the BrowserOS neo pilot (docker/browseros-neo) safely.
#
#   browseros-neo.sh check      render the Compose config and refuse anything unsafe
#   browseros-neo.sh up         check, start Neo, then open the agent path (unless paused)
#   browseros-neo.sh down --approve-down
#                               stop and remove the containers (the profile folder stays);
#                               refused without the flag
#   browseros-neo.sh status     reconcile the pause record with the live gate, port and bindings
#   browseros-neo.sh pause [--no-wait]
#                               close the agent's MCP path, then drain for the pinned run budget
#   browseros-neo.sh resume     re-check config/engine/profile against `up`, then reopen the path
#   browseros-neo.sh handoff [--clear-origin https://ORIGIN]...
#                               after a drained pause: restart Neo's browser, remove every
#                               service worker/Cache Storage, clear each sign-in origin,
#                               close all tabs, verify; the human then signs in via the viewer
#   browseros-neo.sh access     print how to reach the viewer from another machine
#   browseros-neo.sh hermes-snippet    print the reference Hermes mcp_servers entry
#   browseros-neo.sh hermes-commands   print the native `hermes config set` commands for it
#
# Settings (environment): BROWSEROS_NEO_DATA (profile folder, default
# ~/.local/share/agent-ops-kit/browseros-neo/data), BROWSEROS_NEO_VNC_PORT
# (16080), BROWSEROS_NEO_MCP_PORT (19010), BROWSEROS_NEO_PROJECT (browseros-neo).
# up/down/pause/resume hold one lifecycle lock per profile; a second one fails.
# Only `down --approve-down` removes containers and only `pause` stops one (the
# mcp-gate). No other command stops, removes or recreates a container: when a
# check fails they refuse and print the approved command to run.
# `handoff` restarts the browser process inside the running Neo, not a container.
# docs/browseros-neo.md explains what pause does and does not stop.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"

COMPOSE_DIR="$ROOT/docker/browseros-neo"
CHECKER="$ROOT/scripts/lib/browseros_neo_check.py"
IMAGE="ghcr.io/browseros-ai/neo:0.52.1.2-d32f6ab@sha256:9f6302905f4db0d7d58a13e00b74cfc5c4448c5a7517a36700581a48e102d72e"
MIN_DOCKER_MAJOR=28   # 28.0 stopped other LAN hosts reaching 127.0.0.1-published ports
MAX_WALL_TIMEOUT=600  # refuse a neo.toml run budget outside 1..600 s
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
# The profile path must be its own canonical spelling: no symlinked parent,
# '..', '//' or trailing '/' that could alias another folder (or the repo).
canon="$(realpath -m -- "$DATA")" || die "cannot canonicalise BROWSEROS_NEO_DATA=$DATA"
[[ "$canon" == "$DATA" ]] || die "BROWSEROS_NEO_DATA=$DATA is not canonical (resolves to $canon); use the resolved path"
in_tree() { [[ "$1" == "$2" || "$1" == "$2"/* ]]; }
in_tree "$DATA" "$ROOT" && die "BROWSEROS_NEO_DATA must be outside the repository"
main_tree="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
if [[ -n "$main_tree" ]]; then
  main_tree="$(realpath -m -- "$main_tree/..")"
  in_tree "$DATA" "$main_tree" && die "BROWSEROS_NEO_DATA must be outside the repository checkout $main_tree"
fi
STATE_DIR="$(dirname "$DATA")/state-$PROJECT"
STATE_FILE="$STATE_DIR/agent-paused"   # present = agent path must stay closed
MANIFEST="$STATE_DIR/manifest"         # what `up` checked and started
LOCK="$STATE_DIR/lock"

# The Compose invocation never reads a stray .env, COMPOSE_FILE or profile;
# what `check` validates is exactly what `up` runs.
compose() {
  env -u COMPOSE_FILE -u COMPOSE_PROFILES -u COMPOSE_PROJECT_NAME -u COMPOSE_ENV_FILES \
    BROWSEROS_NEO_DATA="$DATA" BROWSEROS_NEO_VNC_PORT="$VNC_PORT" BROWSEROS_NEO_MCP_PORT="$MCP_PORT" \
    BROWSEROS_NEO_UID="$(id -u)" BROWSEROS_NEO_GID="$(id -g)" \
    "$DOCKER" compose --env-file /dev/null -p "$PROJECT" --project-directory "$COMPOSE_DIR" \
    -f "$COMPOSE_DIR/compose.yaml" "$@"
}

sha() { sha256sum -- "$1" | cut -d' ' -f1; }

take_lock() {  # one lifecycle command per profile; never waits
  ( umask 077; mkdir -p "$STATE_DIR" )
  exec 9>>"$LOCK"
  flock -n 9 || die "lifecycle lock $LOCK is held by another up/down/pause/resume; nothing changed"
}

wall_timeout() {  # the run budget in the host's neo.toml, as a bounded positive integer
  local w
  w="$(python3 -I -c 'import sys,tomllib; v=tomllib.load(open(sys.argv[1],"rb"))["run"]["wall_timeout_seconds"]
assert type(v) is int; print(v)' "$COMPOSE_DIR/neo.toml" 2>/dev/null)" \
    || die "cannot read [run] wall_timeout_seconds from $COMPOSE_DIR/neo.toml"
  [[ "$w" =~ ^[0-9]+$ ]] && (( w >= 1 && w <= MAX_WALL_TIMEOUT )) \
    || die "neo.toml wall_timeout_seconds=$w is outside 1..$MAX_WALL_TIMEOUT"
  echo "$w"
}

port_open() {  # port_open PORT -> 0 when 127.0.0.1:PORT accepts a TCP connection
  python3 -I -c 'import socket,sys
s=socket.socket(); s.settimeout(2)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1])))==0 else 1)' "$1"
}

check_engine() {
  [[ -z "${DOCKER_HOST:-}" || "${DOCKER_HOST}" == unix://* ]] \
    || die "DOCKER_HOST=$DOCKER_HOST is not a local socket; 127.0.0.1 would mean another machine"
  local ctx ep ver
  ctx="$("$DOCKER" context show 2>/dev/null)" && [[ -n "$ctx" ]] || die "cannot read the current docker context"
  ep="$("$DOCKER" context inspect "$ctx" --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" \
    || die "cannot inspect docker context '$ctx'"
  [[ "$ep" == unix://* ]] || die "docker context '$ctx' points at '$ep', not a local socket"
  ver="$("$DOCKER" version --format '{{.Server.Version}}' 2>/dev/null)" || die "Docker engine unreachable"
  [[ "${ver%%.*}" =~ ^[0-9]+$ ]] && (( ${ver%%.*} >= MIN_DOCKER_MAJOR )) \
    || die "Docker engine $ver is older than $MIN_DOCKER_MAJOR.0; loopback-published ports may be reachable from the LAN"
  ENGINE_ID="$("$DOCKER" info --format '{{.ID}}' 2>/dev/null)" && [[ -n "$ENGINE_ID" ]] \
    || die "cannot read the Docker engine ID"
}

check_data() {
  [[ -d "$DATA" && ! -L "$DATA" ]] || die "profile folder $DATA is missing or a symlink (run 'up' to create it)"
  local owner mode; owner="$(stat -c %u "$DATA")"; mode="$(stat -c %a "$DATA")"
  [[ "$owner" == "$(id -u)" ]] || die "profile folder $DATA is owned by uid $owner, not $(id -u)"
  (( (8#$mode & 8#077) == 0 )) || die "profile folder $DATA has mode $mode; group/other access must be 0 (chmod 700)"
  # One container per profile: another container mounting this folder, a
  # parent of it or a folder inside it can read or corrupt the profile.
  local ids rows others
  ids="$("$DOCKER" ps -aq)" || die "cannot list containers to check who mounts $DATA"
  [[ -n "$ids" ]] || return 0
  # shellcheck disable=SC2086
  rows="$("$DOCKER" inspect $ids --format '{{.Name}}|{{index .Config.Labels "com.docker.compose.project"}}|{{range .Mounts}}{{.Source}}|{{end}}')" \
    || die "cannot inspect containers to check who mounts $DATA"
  others="$(awk -F'|' -v p="$PROJECT" -v d="$DATA" '$2 != p {
      for (i = 3; i < NF; i++) { s = $i
        if (s == d || index(d, s "/") == 1 || index(s, d "/") == 1 || s == "/") { print $1 " (" s ")"; break } } }' <<<"$rows")"
  [[ -z "$others" ]] || die "profile folder $DATA is already mounted by: $others"
}

cmd_check() {  # sets CONFIG_SHA for the rendered, accepted config
  check_engine
  check_data
  local cfg; cfg="$(compose config --format json)" || die "compose config failed"
  python3 -I "$CHECKER" --data "$DATA" --image "$IMAGE" --compose-dir "$COMPOSE_DIR" --project "$PROJECT" \
    --vnc-port "$VNC_PORT" --mcp-port "$MCP_PORT" <<<"$cfg" \
    || die "rendered Compose config refused (see above)"
  CONFIG_SHA="$(printf '%s' "$cfg" | sha256sum | cut -d' ' -f1)"
  log "config ok: project=$PROJECT viewer=127.0.0.1:$VNC_PORT mcp=127.0.0.1:$MCP_PORT data=$DATA"
}

manifest_body() {
  cat <<EOF
project=$PROJECT
data=$DATA
vnc_port=$VNC_PORT
mcp_port=$MCP_PORT
image=$IMAGE
engine_id=$ENGINE_ID
config_sha=$CONFIG_SHA
neo_toml_sha=$(sha "$COMPOSE_DIR/neo.toml")
gate_sha=$(sha "$COMPOSE_DIR/mcp-gate.py")
ws6080_sha=$(sha "$COMPOSE_DIR/websockify-6080")
policy_sha=$(sha "$COMPOSE_DIR/chromium-policy.json")
wall_timeout=$(wall_timeout)
EOF
}

manifest_get() {  # manifest_get KEY -> value; fails when the manifest or key is missing
  [[ -f "$MANIFEST" ]] || return 1
  local v; v="$(sed -n "s/^$1=//p" "$MANIFEST")"; [[ -n "$v" ]] || return 1; echo "$v"
}

# The exact operator command for removing the pilot; printed, never run.
APPROVED_DOWN="scripts/browseros-neo.sh down --approve-down"
[[ "$PROJECT" == browseros-neo ]] || APPROVED_DOWN="BROWSEROS_NEO_PROJECT=$PROJECT BROWSEROS_NEO_DATA=$DATA $APPROVED_DOWN"

# --- live state -------------------------------------------------------------
# Every reader fails (non-zero) when Docker cannot answer; callers treat that
# as "unknown", never as "closed" or "loopback".

running_ids() { compose ps -q --status running "$@"; }

gate_running() {  # 0 running, 1 not running, 2 unknown
  local ids; ids="$(running_ids mcp-gate)" || return 2
  [[ -n "$ids" ]]
}

foreign_gates() {  # pilot gates of other projects (a pause run with the wrong environment)
  "$DOCKER" ps --filter label=com.docker.compose.service=mcp-gate \
    --format '{{.Names}} {{.Label "com.docker.compose.project"}} {{.Image}}' \
    | awk -v p="$PROJECT" -v i="$IMAGE" '$2 != p && $3 == i { print $1 " (project " $2 ")" }'
}

binding_problems() {  # prints every unexpected live binding; returns 2 when Docker cannot answer
  local ids rows
  ids="$(compose ps -q)" || return 2
  [[ -n "$ids" ]] || return 0
  # shellcheck disable=SC2086
  rows="$("$DOCKER" inspect $ids --format '{{index .Config.Labels "com.docker.compose.service"}}{{range $p, $b := .NetworkSettings.Ports}}{{range $b}} {{$p}}={{.HostIp}}:{{.HostPort}}{{end}}{{end}}')" \
    || return 2
  local svc b rest
  while read -r svc rest; do
    case "$svc" in neo|mcp-gate|viewer) ;; *) echo "unexpected container ${svc:-<unlabelled>}"; continue ;; esac
    for b in $rest; do
      case "$svc $b" in
        "neo 6081/tcp=127.0.0.1:$VNC_PORT"|"mcp-gate 9010/tcp=127.0.0.1:$MCP_PORT") ;;
        *) echo "$svc $b" ;;
      esac
    done
  done <<<"$rows"
}

refuse_running() {  # fail closed WITHOUT stopping anything: say so, and print the approved command
  die "$1. Nothing was stopped: the pilot may still be running and reachable as reported. Do not use the viewer or type secrets. To remove it (operator approval): $APPROVED_DOWN"
}

enforce_bindings() {  # up/resume: any unexpected binding or unanswerable inspect refuses (no auto-stop)
  local bad rc=0
  bad="$(binding_problems)" || rc=$?
  (( rc == 0 )) || refuse_running "cannot inspect the pilot's live bindings (fail closed)"
  [[ -z "$bad" ]] || refuse_running "EXPOSED: unexpected live binding: $(tr '\n' ' ' <<<"$bad")"
}

assert_gate_closed() {  # the agent path is verifiably closed right now, or die
  local rc=0 foreign
  gate_running || rc=$?
  (( rc == 1 )) || die "${1:-}mcp-gate is $([[ $rc == 0 ]] && echo running || echo 'in an unknown state'); agent path NOT closed"
  ! port_open "$MCP_PORT" || die "${1:-}127.0.0.1:$MCP_PORT still accepts connections; agent path NOT closed"
  foreign="$(foreign_gates)" || die "${1:-}cannot list other pilot gates"
  [[ -z "$foreign" ]] || die "${1:-}another pilot gate is running: $foreign (wrong BROWSEROS_NEO_* environment?)"
}

stop_gate() {  # only `pause`, an explicit operator command, calls this
  compose stop -t 2 mcp-gate >/dev/null || die "stopping mcp-gate failed; agent path may still be OPEN"
}

manifest_matches_or_die() {  # $1 = what is refused; every recorded key must equal the current check
  local fresh="To start clean (operator approval): $APPROVED_DOWN, then 'up'"
  [[ -f "$MANIFEST" ]] || die "$1 refused: no manifest from 'up' for this profile. Containers were not touched. $fresh"
  local k want got now
  now="$(manifest_body)"
  for k in project data vnc_port mcp_port image engine_id config_sha neo_toml_sha gate_sha ws6080_sha policy_sha wall_timeout; do
    want="$(manifest_get "$k")" || die "$1 refused: manifest lacks $k. Containers were not touched. $fresh"
    got="$(sed -n "s/^$k=//p" <<<"$now")"
    [[ "$got" == "$want" ]] || die "$1 refused: $k changed since 'up' ($want -> $got); existing containers are never recreated automatically. $fresh"
  done
}

verify_running_neo() {  # the running Neo is the pinned image on this exact profile folder
  local id row
  id="$(running_ids neo)" || die "cannot query neo"
  [[ -n "$id" && "$id" != *$'\n'* ]] || die "neo is not running (exactly one container expected); use 'up'"
  row="$("$DOCKER" inspect "$id" --format '{{.Config.Image}}|{{range .Mounts}}{{.Destination}}={{.Source}};{{end}}')" \
    || die "cannot inspect the running neo container"
  [[ "${row%%|*}" == "$IMAGE" ]] || die "running neo uses image ${row%%|*}, not the pinned image. Nothing was stopped. To replace it (operator approval): $APPROVED_DOWN, then 'up'"
  [[ ";${row#*|}" == *";/data=$DATA;"* ]] || die "running neo does not mount $DATA at /data; wrong BROWSEROS_NEO_DATA?"
}

# --- commands ---------------------------------------------------------------

cmd_up() {
  take_lock
  ( umask 077; mkdir -p "$DATA" )
  cmd_check
  # Never recreate. A file bind mount pins the inode a container started with
  # and Compose does not notice a changed neo.toml or mcp-gate.py, so existing
  # containers (running or stopped) are reused only when everything `up`
  # recorded still matches; otherwise `up` refuses and names the approved
  # command. --no-recreate also stops Compose replacing a container on its
  # own when the config hash changed.
  local ids
  ids="$(compose ps -aq)" || die "cannot list the pilot's containers"
  if [[ -n "$ids" ]]; then
    manifest_matches_or_die "up"
  else
    manifest_body > "$MANIFEST.tmp" && mv -f "$MANIFEST.tmp" "$MANIFEST"
  fi
  # Paused: the gate must already be stopped; `up` never stops it for you.
  [[ ! -e "$STATE_FILE" ]] || assert_gate_closed "pause recorded in $STATE_FILE but "
  "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1 || compose pull neo
  compose up -d --no-recreate --wait --wait-timeout "${BROWSEROS_NEO_WAIT:-240}" neo viewer
  enforce_bindings
  if [[ -e "$STATE_FILE" ]]; then
    assert_gate_closed
    log "agent path stays closed (pause recorded in $STATE_FILE); run 'resume' to reopen it"
  else
    open_gate
  fi
  report_status || true
}

gate_failed() {  # the gate did not come up cleanly; report the real state, stop nothing
  local rc=0
  gate_running || rc=$?
  if (( rc == 1 )) && ! port_open "$MCP_PORT"; then
    die "$1; mcp-gate is not running and 127.0.0.1:$MCP_PORT refuses connections (agent path closed)"
  fi
  die "$1; mcp-gate is $([[ $rc == 0 ]] && echo running || echo 'in an unknown state') and the agent path may be OPEN. Nothing was stopped. Close it with: scripts/browseros-neo.sh pause (stops mcp-gate only)"
}

open_gate() {  # start the gate and prove the path is open; on failure refuse without stopping anything
  # Callers proved mcp-gate.py's hash equals the manifest; an existing gate
  # container is started as is, never recreated.
  compose up -d --no-deps --no-recreate mcp-gate >/dev/null || gate_failed "starting mcp-gate failed"
  enforce_bindings
  local _
  for _ in $(seq 1 20); do
    if port_open "$MCP_PORT" && gate_running; then
      rm -f "$STATE_FILE"
      log "agent path OPEN: http://127.0.0.1:$MCP_PORT/mcp"
      return 0
    fi
    sleep 0.5
  done
  gate_failed "gate did not accept connections on 127.0.0.1:$MCP_PORT within 10 s"
}

cmd_down() {  # the only command that removes containers; needs the explicit flag
  [[ "${1:-}" == --approve-down ]] || die "down stops and removes the pilot's containers (neo, viewer, mcp-gate) and network for project $PROJECT; the profile folder stays. Nothing was done. Run it only with operator approval: $APPROVED_DOWN"
  take_lock
  compose down --remove-orphans
  log "stopped; profile kept in $DATA"
}

cmd_pause() {
  local nowait=0
  case "${1:-}" in "") ;; --no-wait) nowait=1 ;; *) die "pause: unknown option $1" ;; esac
  take_lock
  # Record the intent first so a concurrent or later `up` keeps the gate shut
  # even if this command dies part-way.
  local w drift=""
  w="$(manifest_get wall_timeout)" || drift="no manifest from 'up' for this profile"
  if [[ -z "$drift" && "$(sha "$COMPOSE_DIR/neo.toml")" != "$(manifest_get neo_toml_sha)" ]]; then
    drift="neo.toml changed since 'up'; the running Neo may use a different run budget"
  fi
  printf 'state=DRAINING\nsince=%s\ndeadline=%s\n' "$(date +%s)" \
    "$([[ -z "$drift" ]] && echo $(( $(date +%s) + w + 5 )) || echo unknown)" > "$STATE_FILE"
  stop_gate
  assert_gate_closed
  log "agent MCP path CLOSED: 127.0.0.1:$MCP_PORT refuses connections, mcp-gate stopped"
  [[ -z "$drift" ]] || die "DRAINING with unknown budget: $drift. Do not type secrets. To pin it again (operator approval): $APPROVED_DOWN, then 'up'"
  if (( nowait )); then
    log "DRAINING, not drained: a browser script already in flight may act for up to $((w + 5)) s more."
    log "Do NOT type secrets yet. Run 'pause' again (without --no-wait) to finish the drain."
    return 0
  fi
  log "DRAINING: waiting $((w + 5)) s (pinned run budget ${w} s + 5 s) for in-flight browser scripts..."
  sleep "$((w + 5))"
  # Re-verify everything after the wait; state observed before it is stale.
  [[ "$(sha "$COMPOSE_DIR/neo.toml")" == "$(manifest_get neo_toml_sha)" ]] \
    || die "neo.toml changed during the drain; state stays DRAINING. To pin it again (operator approval): $APPROVED_DOWN, then 'up'"
  grep -qx 'state=DRAINING' "$STATE_FILE" 2>/dev/null || die "pause record changed during the drain; state unknown"
  assert_gate_closed "agent path reopened during the drain: "
  printf 'state=CLOSED\nclosed_at=%s\n' "$(date +%s)" > "$STATE_FILE"
  log "CLOSED: the agent path has stayed closed for the pinned run budget."
  log "This is not proof the browser is quiet: page timers, injected scripts, service workers, open tabs"
  log "or settings left by earlier tool calls can outlive the run budget, and looking at the page cannot"
  log "show them. Before signing in, run 'handoff' (docs/browseros-neo.md, 'Login status')."
}

cmd_handoff() {  # prepare the running browser for a human sign-in; agent path must be drained
  local a args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --clear-origin)
        a="${2:-}"
        [[ "$a" =~ ^https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$ ]] \
          || die "handoff: --clear-origin needs an https origin like https://accounts.example.com (got '${a}')"
        args+=(--clear-origin "$a"); shift 2 ;;
      *) die "handoff: unknown option $1" ;;
    esac
  done
  take_lock
  grep -qx 'state=CLOSED' "$STATE_FILE" 2>/dev/null \
    || die "handoff refused: the agent path is not drained (run 'pause' without --no-wait first). Nothing was touched."
  assert_gate_closed "handoff refused: "
  cmd_check
  manifest_matches_or_die "handoff"
  verify_running_neo
  enforce_bindings
  local id report rc=0
  id="$(running_ids neo)" || die "cannot query neo"
  log "handoff: restarting Neo's browser and scrubbing agent-planted state (up to ~4 min)..."
  report="$("$DOCKER" exec -i -u neo "$id" /usr/bin/python3 -I - "${args[@]}" < "$COMPOSE_DIR/neo-handoff.py")" || rc=$?
  printf '%s\n' "$report"
  # The scrub ran with the gate closed; prove it is still closed and still the same record.
  assert_gate_closed "handoff: agent path reopened during the scrub: "
  grep -qx 'state=CLOSED' "$STATE_FILE" 2>/dev/null || die "handoff: pause record changed during the scrub; do NOT sign in"
  (( rc == 0 )) && python3 -I -c 'import json,sys; sys.exit(json.loads(sys.argv[1]).get("result") != "HANDOFF-READY")' "$report" 2>/dev/null \
    || die "handoff FAILED (rc $rc); do NOT sign in. The browser may have been restarted; nothing was stopped or removed."
  printf 'handoff_at=%s\n' "$(date +%s)" >> "$STATE_FILE"
  log "HANDOFF-READY: browser restarted, no service worker, Cache Storage, agent tab or timer survived (verified)."
  log "Sign in now through the viewer ('access'). Keep the agent path closed until you are done; 'resume' reopens it"
  log "and the agent then acts with the signed-in session. docs/browseros-neo.md 'Login status' lists what this does not cover."
}

cmd_resume() {
  take_lock
  # Everything `up` checked is re-checked, and must equal what `up` started,
  # BEFORE the gate is started.
  cmd_check
  manifest_matches_or_die "resume"
  verify_running_neo
  enforce_bindings
  open_gate
}

report_status() {  # read-only reconciliation; non-zero when unsafe or unknown
  compose ps || { log "agent path: UNKNOWN (Docker did not answer)"; return 1; }
  local bad rc=0 grc=0 state open=0 foreign foreign_msg
  bad="$(binding_problems)" || rc=$?
  (( rc == 0 )) || { log "agent path: UNKNOWN (cannot inspect live bindings)"; return 1; }
  [[ -z "$bad" ]] || { log "EXPOSED: unexpected live binding: $(tr '\n' ' ' <<<"$bad")— remove the pilot now (operator approval): $APPROVED_DOWN"; return 1; }
  foreign="$(foreign_gates)" || { log "agent path: UNKNOWN (cannot list pilot gates)"; return 1; }
  [[ -z "$foreign" ]] && foreign_msg="" || foreign_msg="another pilot gate is running: $foreign"
  gate_running || grc=$?
  (( grc != 2 )) || { log "agent path: UNKNOWN (cannot query mcp-gate)"; return 1; }
  if (( grc == 0 )) || port_open "$MCP_PORT"; then open=1; fi
  state="$(sed -n 's/^state=//p' "$STATE_FILE" 2>/dev/null || true)"
  if (( open )); then
    if [[ -e "$STATE_FILE" ]]; then
      log "agent path: INCONSISTENT — pause recorded ($state) but the agent path is OPEN at 127.0.0.1:$MCP_PORT. Do NOT type secrets; run 'pause'."
      return 1
    fi
    log "agent path: OPEN at http://127.0.0.1:$MCP_PORT/mcp"
  elif [[ ! -e "$STATE_FILE" ]]; then
    log "agent path: CLOSED but not drained (gate stopped, no pause recorded). Run 'pause' before typing secrets."
  elif [[ "$state" == CLOSED ]]; then
    log "agent path: CLOSED (gate stopped and pause drained the pinned run budget; page side effects can still outlive it)"
  elif [[ "$state" == DRAINING ]]; then
    local dl; dl="$(sed -n 's/^deadline=//p' "$STATE_FILE")"
    log "agent path: DRAINING (closed, drain not completed; deadline $dl). Do NOT type secrets; run 'pause' to finish."
  else
    log "agent path: UNKNOWN pause record in $STATE_FILE"; return 1
  fi
  [[ -z "$foreign_msg" ]] || { log "WARNING: $foreign_msg"; return 1; }
}

cmd_access() {
  cat <<EOF
The viewer has no password; it is published on this host's 127.0.0.1 only.
From your own machine, tunnel it over SSH (SSH is the authentication):

  ssh -N -L ${VNC_PORT}:127.0.0.1:${VNC_PORT} <you>@<this-host>

then open http://127.0.0.1:${VNC_PORT}/ locally: the same local port, and
127.0.0.1, not localhost. The viewer accepts a WebSocket only with Origin
http://127.0.0.1:${VNC_PORT}; any other local port or host name is refused.

LOGIN: only after 'pause' (full drain) and then 'handoff' reports
HANDOFF-READY, and only with the limits in docs/browseros-neo.md, "Login
status". Neo's own 6080 refuses every WebSocket and pages cannot reach Neo's
loopback ports (managed policy); the viewer is the only way in.
EOF
}

cmd_hermes_snippet() {
  sed "s/__BROWSEROS_NEO_MCP_PORT__/$MCP_PORT/" "$ROOT/hermes/mcp/browseros-neo.yaml"
}

cmd_hermes_commands() {  # native `hermes config set` sequence: disabled first, enabled last
  cmd_hermes_snippet | python3 -I -c '
import shlex, sys, yaml
(name, cfg), = yaml.safe_load(sys.stdin)["mcp_servers"].items()
def flat(prefix, v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from flat(f"{prefix}.{k}", x)
    else:
        yield prefix, v
def text(v):
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, list): return "[" + ",".join(map(str, v)) + "]"
    return str(v)
base = f"mcp_servers.{name}"
print(f"hermes config set {base}.enabled false")
for key, val in flat(base, {k: v for k, v in cfg.items() if k != "enabled"}):
    print(f"hermes config set {key} {shlex.quote(text(val))}")
if cfg.get("enabled"):
    print(f"hermes config set {base}.enabled true")
'
}

case "${1:-}" in
  check) cmd_check ;;
  up) cmd_up ;;
  down) shift; cmd_down "${1:-}" ;;
  status) report_status ;;
  pause) shift; cmd_pause "${1:-}" ;;
  resume) cmd_resume ;;
  handoff) shift; cmd_handoff "$@" ;;
  access) cmd_access ;;
  hermes-snippet) cmd_hermes_snippet ;;
  hermes-commands) cmd_hermes_commands ;;
  *) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
