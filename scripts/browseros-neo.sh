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
#   browseros-neo.sh handoff --clear-origin https://ORIGIN [--clear-origin ...] [--trusted-host-dev]
#                               after a drained pause: restart Neo's browser, remove listed service
#                               workers (+ their origins' Cache Storage), clear each sign-in origin and
#                               cookies sent to it, close all tabs, verify the scrubbed scope only.
#                               --trusted-host-dev is the operator's same-host development
#                               acknowledgment for this invocation only (trusted host, NOT
#                               isolation); nothing is recorded and `status` never shows it
#   browseros-neo.sh access     print how to reach the viewer from another machine
#   browseros-neo.sh hermes-snippet    print the reference Hermes mcp_servers entry
#   browseros-neo.sh hermes-commands   print the native `hermes config set` commands for it
#   browseros-neo.sh hermes-enable --approve-activation
#                               opt-in: run those commands against the Hermes home that
#                               `hermes config path` names (enabled false first, enabled true
#                               last); refused without the flag or unless this pilot's own
#                               Compose gate is verified open; idempotent; an existing entry
#                               with other values is refused; a failure rolls back
#   browseros-neo.sh hermes-disable    set mcp_servers.browseros_neo.enabled false (entry kept);
#                               exits 0 only when absent or false is read back
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
ws6080_mode=$(stat -c %a -- "$COMPOSE_DIR/websockify-6080")
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
  for k in project data vnc_port mcp_port image engine_id config_sha neo_toml_sha gate_sha ws6080_sha ws6080_mode policy_sha wall_timeout; do
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

# Live proof, inside Neo's network namespace, that the 6080 shim is the
# websockify actually running (a non-executable shim makes PATH lookup fall
# through to the image's unchecked one): 6080 listens on 127.0.0.1 only and
# answers 403 to every WebSocket upgrade, and nothing listens on the viewer
# port (a listener there would carry the one Origin the viewer accepts).
# Point-in-time only.
PORT_PROBE='import socket, sys
vnc = int(sys.argv[1]); bad = []
nov = int(sys.argv[2]) if len(sys.argv) > 2 else 6080  # tests only; the wrapper never passes it
def listeners(port):
    out = []
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = open(f).read().splitlines()[1:]
        except OSError:
            continue
        for r in rows:
            c = r.split()
            if c[3] == "0A" and int(c[1].rsplit(":", 1)[1], 16) == port:
                out.append(c[1].rsplit(":", 1)[0])
    return out
l6080 = listeners(nov)
if not l6080 or set(l6080) != {"0100007F"}:
    bad.append("6080 listeners %r (want 127.0.0.1 only)" % l6080)
if listeners(vnc):
    bad.append("something listens on the viewer port %d inside Neo" % vnc)
for origin in (None, "null", "http://127.0.0.1:6080", "http://127.0.0.1:%d" % vnc, "http://localhost:6080"):
    try:
        s = socket.create_connection(("127.0.0.1", nov), timeout=5)
        s.sendall(("GET /websockify HTTP/1.1\r\nHost: 127.0.0.1:6080\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
                   + ("" if origin is None else "Origin: %s\r\n" % origin) + "\r\n").encode())
        line = s.recv(256).split(b"\r\n", 1)[0]; s.close()
    except OSError as e:
        line = repr(e).encode()
    if not (line.startswith(b"HTTP/1.") and line.split()[1:2] == [b"403"]):
        bad.append("6080 upgrade with Origin %r answered %r (want 403)" % (origin, line[:80]))
print("PROBE-BAD: " + "; ".join(bad) if bad else "PROBE-OK"); sys.exit(1 if bad else 0)'

probe_neo_ports() {  # refuse (stopping nothing) unless the live probe says PROBE-OK
  local id out rc=0
  id="$(running_ids neo)" || die "cannot query neo"
  [[ -n "$id" && "$id" != *$'\n'* ]] || die "neo is not running (exactly one container expected)"
  out="$("$DOCKER" exec -u neo "$id" /usr/bin/python3 -I -c "$PORT_PROBE" "$VNC_PORT" </dev/null 2>&1)" || rc=$?
  (( rc == 0 )) && [[ "$out" == PROBE-OK ]] \
    || refuse_running "Neo's 6080 lockdown is NOT proven in effect (${out:-no output}, rc $rc)"
  log "6080 probe ok: loopback only, every WebSocket upgrade 403; viewer port $VNC_PORT free inside Neo"
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
  # Advisory lines an older wrapper appended (handoff_at, trust_*) are never
  # trusted; they are stripped so the record keeps only the pause.
  [[ ! -e "$STATE_FILE" ]] || { assert_gate_closed "pause recorded in $STATE_FILE but "; strip_legacy_handoff_lines; }
  "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1 || compose pull neo
  start_neo_and_viewer
  enforce_bindings
  probe_neo_ports
  if [[ -e "$STATE_FILE" ]]; then
    assert_gate_closed
    log "agent path stays closed (pause recorded in $STATE_FILE); run 'resume' to reopen it"
    log "Any earlier handoff no longer applies (the browser may be new): run 'pause' and 'handoff' again before signing in."
  else
    open_gate
  fi
  report_status || true
}

start_neo_and_viewer() {  # neo healthy, then viewer running; reuses existing containers, stops nothing
  # `compose up --wait` fails with "container ... has no healthcheck
  # configured" for any listed service whose healthcheck is disabled (the
  # viewer), even after starting it. So only neo, which has a healthcheck, is
  # waited on; the viewer is then started without --wait (--no-deps: neo is
  # not touched again) and must be running.
  compose up -d --no-recreate --wait --wait-timeout "${BROWSEROS_NEO_WAIT:-240}" neo \
    || die "neo did not become healthy (compose up --wait neo failed); viewer not started. Nothing was stopped. Inspect: $DOCKER compose -p $PROJECT ps"
  local id
  id="$(running_ids neo)" || die "cannot query neo after it became healthy"
  [[ -n "$id" && "$id" != *$'\n'* ]] || die "neo is not running after compose up --wait (exactly one container expected)"
  compose up -d --no-deps --no-recreate viewer \
    || die "starting viewer failed; neo is left running. Nothing was stopped."
  id="$(running_ids viewer)" || die "cannot query viewer after starting it"
  [[ -n "$id" && "$id" != *$'\n'* ]] \
    || die "viewer is not running after compose up (exactly one container expected); neo is left running. Nothing was stopped. Logs: $DOCKER compose -p $PROJECT logs viewer"
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
  # container is started as is, never recreated. The record says OPENING
  # before the gate can start, so a failed or partial open never leaves a
  # stale CLOSED (drained) record behind: only a new full `pause` restores it.
  ( umask 077; printf 'state=OPENING\nsince=%s\n' "$(date +%s)" > "$STATE_FILE.tmp" && mv -f "$STATE_FILE.tmp" "$STATE_FILE" ) \
    || die "cannot record OPENING in $STATE_FILE; the gate was not started"
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

strip_legacy_handoff_lines() {  # drop handoff_at/trust_* lines older wrappers wrote; keep the pause state
  # Nothing reads them: sign-in permission is only ever the output of the
  # current successful `handoff`, never a persisted record.
  [[ -e "$STATE_FILE" ]] || return 0
  ( umask 077; sed -e '/^handoff_at=/d' -e '/^trust_mode=/d' -e '/^trust_accepted_at=/d' "$STATE_FILE" > "$STATE_FILE.tmp" \
      && mv -f "$STATE_FILE.tmp" "$STATE_FILE" ) \
    || die "cannot strip old handoff lines from $STATE_FILE; do NOT sign in"
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

gate_quiet_since_drain() {  # the gate container last stopped at least the full drain before closed_at
  # A CLOSED record is a claim about the past; the gate container's own
  # StartedAt/FinishedAt show whether it ran after the drain began (e.g. it
  # was started and stopped outside the wrapper). No gate container at all
  # (removed by an operator `down`) leaves nothing to compare and is accepted.
  local closed w ids rows
  closed="$(sed -n 's/^closed_at=//p' "$STATE_FILE")"
  [[ "$closed" =~ ^[0-9]+$ ]] || die "handoff refused: the CLOSED record has no closed_at. Run 'pause' again."
  w="$(manifest_get wall_timeout)" || die "handoff refused: manifest lacks wall_timeout"
  ids="$(compose ps -aq mcp-gate)" || die "handoff refused: cannot list the mcp-gate container"
  [[ -n "$ids" ]] || return 0
  # shellcheck disable=SC2086
  rows="$("$DOCKER" inspect $ids --format '{{.State.StartedAt}} {{.State.FinishedAt}}')" \
    || die "handoff refused: cannot inspect the mcp-gate container's start/stop times"
  python3 -I -c '
import sys
from datetime import datetime, timezone
closed, budget = int(sys.argv[1]), int(sys.argv[2]) + 5
def ts(v):
    if v.startswith("0001-01-01"):
        return None
    if not v.endswith("Z"):
        raise SystemExit("unexpected Docker timestamp %r" % v)
    head = v[:-1].partition(".")[0]  # whole seconds, floored like closed_at
    return int(datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
bad = []
for row in sys.argv[3].splitlines():
    if not row.strip():
        continue
    started, finished = (ts(x) for x in row.split())
    if started is None:
        continue
    if finished is None or finished < started or finished + budget > closed:
        bad.append(row)
sys.exit("mcp-gate ran after the drain began (StartedAt/FinishedAt %s, closed_at %d)" % (bad, closed) if bad else 0)
' "$closed" "$w" "$rows" || die "handoff refused: the CLOSED record is stale. Run 'pause' (full drain) again. Nothing was touched."
}

cmd_handoff() {  # prepare the running browser for a human sign-in; agent path must be drained
  local a args=() origins=() trusted=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --trusted-host-dev) trusted=1; shift ;;
      --clear-origin)
        a="${2:-}"
        [[ "$a" =~ ^https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:([1-9][0-9]{0,4}))?$ ]] \
          && { [[ -z "${BASH_REMATCH[3]}" ]] || (( BASH_REMATCH[3] <= 65535 )); } \
          || die "handoff: --clear-origin needs an https origin like https://accounts.example.com (got '${a}')"
        args+=(--clear-origin "$a"); origins+=("$a"); shift 2 ;;
      *) die "handoff: unknown option $1" ;;
    esac
  done
  (( ${#args[@]} )) || die "handoff refused: name every disposable sign-in origin with --clear-origin https://ORIGIN (site data on them is cleared). Nothing was touched."
  take_lock
  grep -qx 'state=CLOSED' "$STATE_FILE" 2>/dev/null \
    || die "handoff refused: the agent path is not drained (run 'pause' without --no-wait first). Nothing was touched."
  strip_legacy_handoff_lines
  assert_gate_closed "handoff refused: "
  cmd_check
  manifest_matches_or_die "handoff"
  gate_quiet_since_drain
  verify_running_neo
  enforce_bindings
  probe_neo_ports
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
  log "HANDOFF-READY: browser process restarted (old pages, timers and running workers ended); no service-worker"
  log "registration listed; only one about:blank tab (verified)."
  log "Scope: all site data cleared only on ${origins[*]}; cookies sent to those hosts (incl. parent-domain and"
  log "partitioned) deleted and verified absent; service-worker/Cache Storage on origins that had a worker; HTTP cache."
  log "Cache Storage and other storage on origins with no listed worker were not cleared or checked."
  log "A new browser process resumed the same persistent profile (not the same tab); other origins were not cleared and the"
  log "whole profile is not proven clean."
  log "localStorage/sessionStorage on those origins were cleared but are not verified (usage does not count them)."
  if (( trusted )); then
    trusted_host_notice
    log "Keep the agent path closed until done; 'resume' hands that signed-in session to the agent."
    return 0
  fi
  log "Real-account login is NO-GO (docs/browseros-neo.md 'Login status'; host processes can reach Neo's MCP port on its"
  log "bridge address and drive the viewer, paused or not): use the viewer ('access') only with a disposable"
  log "test account on those origins. Keep the agent path closed until done; 'resume' hands that session to the agent."
  log "(An operator running this in a single-operator development VM who accepts the trusted-host assumption"
  log "acknowledges that per sign-in with --trusted-host-dev; docs/browseros-neo.md 'Same-host development'.)"
}

trusted_host_notice() {  # the exact terms of the operator's same-host development acceptance
  log "SAME-HOST DEVELOPMENT (trusted-host assumption acknowledged by the operator for this handoff only; not recorded):"
  log "you may sign in to your own account through the viewer while supervising it. This is NOT isolation and"
  log "carries no isolation guarantee: every process on this host, including an agent's terminal tool and any"
  log "other local user, can reach Neo's unauthenticated MCP/settings port on its bridge address and watch or"
  log "drive this viewer with a forged Origin, gate paused or not; anything running as your UID can read the"
  log "profile folder (sessions). Not for shared, multi-user or production hosts. From 'pause' until you finish"
  log "typing and run 'resume', run no other agent task on this host (the wrapper cannot enforce that; after"
  log "'resume' the agent drives this signed-in profile). Never paste credentials into chat, and revoke the"
  log "account's sessions if the host or an agent misbehaves. The general real-account status stays NO-GO."
}

cmd_resume() {
  take_lock
  # Everything `up` checked is re-checked, and must equal what `up` started,
  # BEFORE the gate is started.
  cmd_check
  manifest_matches_or_die "resume"
  verify_running_neo
  enforce_bindings
  probe_neo_ports
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
  elif [[ "$state" == OPENING ]]; then
    log "agent path: CLOSED but not drained (an open attempt did not complete and may have opened it). Run 'pause' before typing secrets."
  elif [[ "$state" == CLOSED ]]; then
    log "agent path: CLOSED (gate stopped and pause drained the pinned run budget; page side effects can still outlive it)"
    log "CLOSED alone is not handoff success: status records no handoff or sign-in permission. Sign in only right"
    log "after a 'handoff' run in this pause printed HANDOFF-READY and exited 0; real-account login stays NO-GO."
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

LOGIN: real accounts are NO-GO (docs/browseros-neo.md, "Login status"). Host
processes, including an agent's terminal, can reach Neo's MCP port on its
bridge address and drive this viewer with a forged Origin, paused or not.
Disposable test accounts only, after 'pause' (full drain) and 'handoff'
reports HANDOFF-READY. Neo's own 6080 refuses every WebSocket and pages cannot
reach Neo's loopback ports (managed policy); that does not stop host processes.

Same-host development exception: in a single-operator development VM whose
operator accepts the trusted-host assumption, 'handoff --trusted-host-dev'
acknowledges that for that one invocation; nothing is recorded and 'status'
never shows it. Every check still runs and no isolation is added; only what
that handoff's own output says you may sign in to changes.
EOF
}

cmd_hermes_snippet() {
  sed "s/__BROWSEROS_NEO_MCP_PORT__/$MCP_PORT/" "$ROOT/hermes/mcp/browseros-neo.yaml"
}

cmd_hermes_commands() {  # native `hermes config set` sequence: disabled first, enabled last
  # The warning goes to stderr so stdout stays runnable commands only.
  echo "# Review first. Against the live Hermes home the LAST command activates Neo: a running gateway" >&2
  echo "# connects newly enabled mcp_servers on its next housekeeping tick (MCP config reconcile), no" >&2
  echo "# restart needed. Run only with activation approval, after 'up'. Revoke: set .enabled false." >&2
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

HERMES_HELPER="$ROOT/scripts/lib/browseros_neo_hermes.py"

gate_verified_open() {  # hermes-enable: the agent path is this pilot's own Compose gate, as 'up' recorded it
  # Read-only: the same checks 'resume' runs before it opens the gate, nothing is started
  # or stopped. Proves the port is published by this project's running mcp-gate with the
  # expected bindings, not that the listener is authenticated: there is no cryptographic
  # peer check, so a later swap of the process behind the port is not detected.
  [[ ! -e "$STATE_FILE" ]] || die "hermes-enable refused: a pause is recorded ($STATE_FILE); the operator closed the agent path. Nothing changed."
  cmd_check
  manifest_matches_or_die "hermes-enable"
  verify_running_neo
  enforce_bindings
  local rc=0 foreign
  gate_running || rc=$?
  (( rc == 0 )) || die "hermes-enable refused: this pilot's mcp-gate is $([[ $rc == 1 ]] && echo 'not running' || echo 'in an unknown state'); a listener on 127.0.0.1:$MCP_PORT is not proof of the gate. Nothing changed."
  foreign="$(foreign_gates)" || die "hermes-enable refused: cannot list other pilot gates. Nothing changed."
  [[ -z "$foreign" ]] || die "hermes-enable refused: another pilot gate is running: $foreign (wrong BROWSEROS_NEO_* environment?). Nothing changed."
  port_open "$MCP_PORT" || die "hermes-enable refused: 127.0.0.1:$MCP_PORT does not accept connections. Nothing changed."
  log "gate verified: project $PROJECT mcp-gate running, published on 127.0.0.1:$MCP_PORT only, manifest matches"
}

cmd_hermes_enable() {  # opt-in only; the explicit flag is the operator's activation choice
  [[ "${1:-}" == "--approve-activation" && $# -eq 1 ]] \
    || die "hermes-enable needs --approve-activation (it enables Neo in the current Hermes home); nothing changed. Review 'hermes-commands' first."
  command -v "${BROWSEROS_NEO_HERMES:-hermes}" >/dev/null 2>&1 || die "hermes CLI not found; nothing changed"
  # Lock order: lifecycle lock (never waits), then the helper's Hermes config lock (bounded
  # wait). up/pause/resume/down never take the Hermes lock and hermes-disable never takes the
  # lifecycle lock, so neither can deadlock; a pause tried meanwhile fails fast and can be rerun.
  take_lock
  gate_verified_open
  python3 -I "$HERMES_HELPER" enable /dev/stdin < <(cmd_hermes_snippet)
}

cmd_hermes_disable() {
  command -v "${BROWSEROS_NEO_HERMES:-hermes}" >/dev/null 2>&1 || die "hermes CLI not found; nothing changed"
  python3 -I "$HERMES_HELPER" disable
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
  hermes-enable) shift; cmd_hermes_enable "$@" ;;
  hermes-disable) cmd_hermes_disable ;;
  *) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
