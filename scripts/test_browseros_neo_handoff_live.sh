#!/usr/bin/env bash
# test_browseros_neo_handoff_live.sh — opt-in live proof of the two login gates
# on the pinned image (docs/browseros-neo.md, "Login status"):
#
#   gate 1  a web page Neo loads cannot reach Neo's internal VNC/WebSocket
#           ports (6080, 6081, 5900) or its MCP/CDP/claw ports;
#   gate 2  service workers, Cache Storage, timers and tabs an agent planted
#           do not survive `neo-handoff.py` into the human's sign-in.
#
# It runs ONE throwaway container built from the rendered `neo` service
# (same image digest, mounts, capabilities, limits, environment) with
# `--network none`, no published ports, a scratch profile, and the image's
# entrypoint wrapped in `timeout`, so it exits on its own. Test-only
# deviation: NEO_ARGS carries --ip-address-space-overrides so one local
# attacker server counts as a public web site. It never stops, removes or
# recreates any container; the container (exited) and scratch folder are kept
# and printed. Needs the pinned image already present (it does not pull).
#
#   BROWSEROS_NEO_LIVE=1 bash scripts/test_browseros_neo_handoff_live.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "${BROWSEROS_NEO_LIVE:-}" == 1 ]] || { echo "  skip live handoff test: set BROWSEROS_NEO_LIVE=1"; exit 0; }
IMAGE="$(sed -n 's/^IMAGE="\(.*\)"$/\1/p' "$ROOT/scripts/browseros-neo.sh")"
DIG="${IMAGE#*@}"; REF="${IMAGE%%:*}@$DIG"
docker image inspect "$REF" >/dev/null 2>&1 || { echo "  FAIL pinned image $REF not present (docker pull it first)"; exit 1; }
LIFETIME="${BROWSEROS_NEO_LIVE_SECONDS:-900}"
VIEWER_PORT=16080
TD="$ROOT/scripts/testdata/browseros-neo"

base="${TMPDIR:-$HOME/.cache}"; mkdir -p "$base"
tmp="$(mktemp -d "$base/bneo-handoff-live.XXXXXX")"; echo "  scratch (kept): $tmp"
mkdir -p "$tmp/data" && chmod 700 "$tmp/data"
name="bneo-handoff-live-$(date +%s)-$$"
pass=0; fail=0
ok() { echo "  ok   $*"; pass=$((pass + 1)); }
bad() { echo "  FAIL $*"; fail=$((fail + 1)); }

env -u COMPOSE_FILE -u COMPOSE_PROFILES BROWSEROS_NEO_DATA="$tmp/data" BROWSEROS_NEO_UID="$(id -u)" \
  BROWSEROS_NEO_GID="$(id -g)" BROWSEROS_NEO_VNC_PORT="$VIEWER_PORT" BROWSEROS_NEO_MCP_PORT=19010 \
  docker compose --env-file /dev/null -p bneo-handoff-live --project-directory "$ROOT/docker/browseros-neo" \
  -f "$ROOT/docker/browseros-neo/compose.yaml" config --format json > "$tmp/cfg.json" \
  || { echo "  FAIL compose config"; exit 1; }

# docker run argv from the rendered neo service; ports dropped, network none.
python3 -I - "$tmp/cfg.json" "$name" "$LIFETIME" > "$tmp/run.argv" <<'EOF' || { echo "  FAIL cannot build docker run argv"; exit 1; }
import json, sys
cfg, name, life = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
n = cfg["services"]["neo"]
a = ["run", "-d", "--name", name, "--network", "none", "--hostname", n["hostname"],
     "--shm-size", str(n["shm_size"]), "--pids-limit", str(n["pids_limit"]),
     "--memory", str(n["mem_limit"]), "--cpus", str(n["cpus"]), "--restart", "no"]
a += [x for o in n["security_opt"] for x in ("--security-opt", o)]
a += [x for c in n["cap_drop"] for x in ("--cap-drop", c)]
a += [x for c in n["cap_add"] for x in ("--cap-add", c)]
for v in n["volumes"]:
    a += ["-v", f"{v['source']}:{v['target']}" + (":ro" if v.get("read_only") else "")]
env = dict(n["environment"])
assert env["NEO_ARGS"] == "", env
env["NEO_ARGS"] = "--ip-address-space-overrides=127.0.0.1:8099=public"  # test-only
for k, v in sorted(env.items()):
    a += ["-e", f"{k}={v}"]
a += ["--entrypoint", "/usr/bin/timeout", n["image"].split(":")[0] + "@" + n["image"].split("@")[1],
      "-s", "TERM", "-k", "30", life, "/usr/bin/tini", "--", "/usr/local/bin/neo-entrypoint"]
print("\n".join(a))
EOF
mapfile -t argv < "$tmp/run.argv"
docker "${argv[@]}" > "$tmp/container.id" || { echo "  FAIL docker run"; exit 1; }
echo "  container (kept, exits by itself within ${LIFETIME}s): $name"
state=""
for _ in $(seq 1 60); do
  state="$(docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' "$name" 2>/dev/null)"
  [[ "$state" == "running healthy" || "$state" == exited* ]] && break; sleep 3
done
[[ "$state" == "running healthy" ]] && ok "Neo healthy with the shim and policy mounted" || { bad "Neo not healthy: $state"; docker logs "$name" > "$tmp/neo.log" 2>&1; exit 1; }

dx() { docker exec -u neo "$name" "$@"; }
dx mkdir -p /tmp/atk /tmp/t
for f in attack.html sw.js check.html; do docker exec -i -u neo "$name" sh -c "cat > /tmp/atk/$f" < "$TD/$f"; done
for f in cdp.py drive.py; do docker exec -i -u neo "$name" sh -c "cat > /tmp/t/$f" < "$TD/$f"; done
docker exec -d -u neo "$name" sh -c 'cd /tmp/atk && exec python3 -I -m http.server 8098 --bind 127.0.0.1 > /tmp/atk/loopback.log 2>&1'
docker exec -d -u neo "$name" sh -c 'cd /tmp/atk && exec python3 -I -m http.server 8099 --bind 127.0.0.1 > /tmp/atk/public.log 2>&1'
# Positive control: an unchecked websockify a page should be able to open.
docker exec -d -u neo "$name" sh -c 'exec python3 -I -m websockify 127.0.0.1:6090 localhost:5900 > /tmp/atk/ws6090.log 2>&1'
# The viewer exactly as compose renders it (uid, args), in Neo's namespace.
mapfile -t vep < <(python3 -I -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["services"]["viewer"]["entrypoint"]))' "$tmp/cfg.json")
vuser="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["services"]["viewer"]["user"])' "$tmp/cfg.json")"
docker exec -d -u "$vuser" "$name" "${vep[@]}"
sleep 3

field() { python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1]))["results"] or {}; print(r.get(sys.argv[2], "<none>"))' "$1" "$2"; }
up() {  # HTTP status of a WebSocket upgrade to PORT with ORIGIN, from inside Neo
  docker exec "$name" curl -s -m 3 -o /dev/null -w '%{http_code}' -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' -H "Origin: $2" "http://127.0.0.1:$1/websockify"
}

echo "-- gate 1: page -> internal VNC/WebSocket"
ls_line="$(docker exec "$name" sh -c 'awk "\$4==\"0A\"{print \$2}" /proc/net/tcp' | tr '\n' ' ')"
[[ "$ls_line" == *"0100007F:17C0"* && "$ls_line" != *"00000000:17C0"* ]] && ok "6080 listens on 127.0.0.1 only ($ls_line)" || bad "6080 binding: $ls_line"
for o in http://127.0.0.1:8098 http://127.0.0.1:6080 "http://127.0.0.1:$VIEWER_PORT" null; do
  c="$(up 6080 "$o")"; [[ "$c" == 403 ]] && ok "6080 upgrade Origin $o -> 403" || bad "6080 upgrade Origin $o -> $c"
done
c="$(docker exec "$name" curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:6080/vnc.html)"; [[ "$c" == 200 ]] && ok "6080 static noVNC still served (healthcheck)" || bad "6080 static -> $c"
c="$(up 6081 "http://127.0.0.1:$VIEWER_PORT")"; [[ "$c" == 101 ]] && ok "viewer 6081 accepts Origin http://127.0.0.1:$VIEWER_PORT -> 101" || bad "viewer allowed Origin -> $c"
c="$(up 6081 http://127.0.0.1:6081)"; [[ "$c" == 403 ]] && ok "viewer 6081 refuses its own in-Neo Origin -> 403" || bad "viewer :6081 Origin -> $c"

dx python3 -I /tmp/t/drive.py 'http://127.0.0.1:8098/attack.html?cdp=__CDP__' 45 > "$tmp/loopback.json"
[[ "$(field "$tmp/loopback.json" ws_6090_unchecked_control)" == OPEN+DATA:RFB* ]] \
  && ok "control: loopback page opens an unchecked websockify (RFB banner read)" || bad "control failed: $(field "$tmp/loopback.json" ws_6090_unchecked_control)"
for k in ws_6080_neo_novnc ws_localhost_6080 ws_6081_viewer ws_5900_xvnc ws_9110_cdp_leaked_url; do
  v="$(field "$tmp/loopback.json" "$k")"; [[ "$v" != OPEN* && "$v" != "<none>" ]] && ok "loopback page $k: $v" || bad "loopback page $k: $v"
done
for k in mcp_9010_cors cdp_9110_json http_5900_xvnc; do
  v="$(field "$tmp/loopback.json" "$k")"; [[ "$v" != READ:200* && "$v" != "<none>" ]] && ok "loopback page $k: $v" || bad "loopback page $k: $v"
done

dx python3 -I /tmp/t/drive.py 'http://127.0.0.1:8099/attack.html?cdp=__CDP__' 45 > "$tmp/public.json"
for k in ws_6090_unchecked_control ws_6080_neo_novnc ws_6081_viewer ws_5900_xvnc ws_9110_cdp_leaked_url; do
  v="$(field "$tmp/public.json" "$k")"; [[ "$v" == "error close:1006" ]] && ok "public page $k blocked without a prompt: $v" || bad "public page $k: $v"
done
for k in mcp_9010_cors claw_9210 cdp_9110_json; do
  v="$(field "$tmp/public.json" "$k")"; [[ "$v" == fail:TypeError* ]] && ok "public page $k blocked: $v" || bad "public page $k: $v"
done

echo "-- gate 2: agent-planted state vs neo-handoff.py"
dx python3 -I /tmp/t/drive.py 'http://127.0.0.1:8098/attack.html?plant=1' 45 > "$tmp/plant.json"
[[ "$(field "$tmp/plant.json" sw)" == registered:* && "$(field "$tmp/plant.json" cache)" == put ]] \
  && ok "planted SW, Cache Storage entry and 1 s timer" || bad "plant: $(cat "$tmp/plant.json")"
dx python3 -I /tmp/t/drive.py 'http://127.0.0.1:8098/check.html' 20 > "$tmp/before.json"
[[ "$(field "$tmp/before.json" sw)" == ALIVE ]] && ok "negative control: planted SW answers /check.html before handoff" || bad "SW not observed before handoff: $(cat "$tmp/before.json")"

docker exec -i -u neo "$name" /usr/bin/python3 -I - --clear-origin https://accounts.example.com \
  < "$ROOT/docker/browseros-neo/neo-handoff.py" > "$tmp/handoff.json"; hrc=$?
echo "  handoff report: $(cat "$tmp/handoff.json")"
hget() { python3 -I -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])).get(sys.argv[2])))' "$tmp/handoff.json" "$1"; }
[[ $hrc == 0 && "$(hget result)" == '"HANDOFF-READY"' ]] && ok "neo-handoff.py rc 0 HANDOFF-READY" || bad "neo-handoff.py rc $hrc"
[[ "$(hget service_workers_found)" == *127.0.0.1:8098* ]] && ok "handoff found the planted SW" || bad "handoff did not see the SW: $(hget service_workers_found)"
[[ "$(hget old_browser_pid)" != "$(hget new_browser_pid)" ]] && ok "browser process replaced ($(hget old_browser_pid) -> $(hget new_browser_pid))" || bad "browser not restarted"

n1="$(dx grep -c beacon /tmp/atk/loopback.log)"; sleep 5; n2="$(dx grep -c beacon /tmp/atk/loopback.log)"
[[ "$n1" == "$n2" ]] && ok "planted timer dead: no beacons in 5 s after handoff ($n1 total)" || bad "beacons continue after handoff ($n1 -> $n2)"
targets="$(docker exec -i -u neo "$name" python3 -I -c 'import sys; sys.path.insert(0,"/tmp/t"); import cdp,json; b,_=cdp.browser(); print(json.dumps([[t["type"],t["url"]] for t in b.call("Target.getTargets")["targetInfos"]]))')"
[[ "$targets" != *8098* && "$targets" != *8099* ]] && ok "no attacker tab or worker after handoff: $targets" || bad "attacker target survived: $targets"
dx python3 -I /tmp/t/drive.py 'http://127.0.0.1:8098/check.html' 20 > "$tmp/after.json"
r="$(python3 -I -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["results"], sort_keys=True))' "$tmp/after.json")"
[[ "$r" == '{"caches": [], "hb": '*', "hb_advanced": false, "regs": []}' ]] \
  && ok "after handoff: /check.html served by the network, no SW, no caches, timer stopped: $r" || bad "after handoff: $r"

docker logs "$name" > "$tmp/neo.log" 2>&1
echo "  $pass passed, $fail failed  (container $name kept; scratch $tmp kept)"
(( fail == 0 ))
