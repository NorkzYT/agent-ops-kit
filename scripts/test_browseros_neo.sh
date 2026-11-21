#!/usr/bin/env bash
# test_browseros_neo.sh — offline checks for the BrowserOS neo pilot:
# the real rendered Compose config passes the fail-closed checker, each unsafe
# mutation of it is refused, the wrapper refuses unsafe engines/folders/settings
# (stub docker; `compose config` is forwarded to the real Docker CLI, which
# needs no daemon), the Hermes snippet, and the MCP gate forwards and cuts.
# Starts no containers. Bounded: every background process is timed out.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAP="$ROOT/scripts/browseros-neo.sh"
CHECK="$ROOT/scripts/lib/browseros_neo_check.py"
IMAGE="$(sed -n 's/^IMAGE="\(.*\)"$/\1/p' "$WRAP")"

pass=0; fail=0
ok() { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no() { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

tmp="$(mktemp -d)"; [[ -d "$tmp" && "$tmp" != / ]] || { echo "mktemp failed" >&2; exit 1; }
trap '[[ -f "$tmp/fake/gate.pid" ]] && kill "$(cat "$tmp/fake/gate.pid")" 2>/dev/null; rm -rf "$tmp"' EXIT
data="$tmp/neo data/data"; mkdir -p "$data"; chmod 700 "$data"
REAL_DOCKER="$(command -v docker || true)"

# --- static ---------------------------------------------------------------
bash -n "$WRAP" && ok "wrapper parses" || no "wrapper parses"
python3 -m py_compile "$CHECK" "$ROOT/docker/browseros-neo/mcp-gate.py" && ok "python compiles" || no "python compiles"
[[ "$IMAGE" == ghcr.io/browseros-ai/neo:*@sha256:* ]] && ok "wrapper pins image by digest" || no "wrapper pins image by digest"
grep -qF "\"$IMAGE\"" "$ROOT/docker/browseros-neo/compose.yaml" && ok "compose uses the wrapper's pinned image" || no "compose/wrapper image mismatch"
wt="$(python3 -I -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["run"]["wall_timeout_seconds"])' "$ROOT/docker/browseros-neo/neo.toml")"
[[ "$wt" == 60 ]] && ok "neo.toml pins run budget (60s)" || no "neo.toml run budget ($wt)"

# --- rendered config + checker --------------------------------------------
if [[ -z "$REAL_DOCKER" ]] || ! docker compose version >/dev/null 2>&1; then
  echo "  skip rendered-config checks: docker compose CLI not available"
else
  cfg="$tmp/cfg.json"
  env -u COMPOSE_FILE BROWSEROS_NEO_DATA="$data" BROWSEROS_NEO_UID=1000 BROWSEROS_NEO_GID=1000 \
    docker compose --env-file /dev/null -p bneo-test --project-directory "$ROOT/docker/browseros-neo" \
    -f "$ROOT/docker/browseros-neo/compose.yaml" config --format json > "$cfg" 2>"$tmp/cfg.err" \
    && ok "compose config renders" || { no "compose config renders"; cat "$tmp/cfg.err"; }
  run_check() { python3 -I "$CHECK" --data "$data" --image "$IMAGE" --compose-dir "$ROOT/docker/browseros-neo" \
    --project bneo-test --vnc-port 16080 --mcp-port 19010 < "$1" 2>"$tmp/check.err"; }
  run_check "$cfg" && ok "real config accepted" || { no "real config accepted"; cat "$tmp/check.err"; }

  # Each mutation is a Python expression over `c` (the config dict); all must be refused.
  while IFS='|' read -r label expr; do
    [[ -n "$label" ]] || continue
    python3 -I - "$cfg" "$tmp/mut.json" "$expr" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1])); n = c["services"]["neo"]; g = c["services"]["mcp-gate"]; v = c["services"]["viewer"]
exec(sys.argv[3])
json.dump(c, open(sys.argv[2], "w"))
EOF
    if run_check "$tmp/mut.json"; then no "refuses: $label"; else ok "refuses: $label"; fi
  done <<'EOF'
viewer on all interfaces|n["ports"][0]["host_ip"] = "0.0.0.0"
viewer without host_ip|n["ports"][0].pop("host_ip")
gate on all interfaces|g["ports"][0]["host_ip"] = ""
neo publishes 9010 directly|n["ports"].append({"host_ip": "127.0.0.1", "target": 9010, "published": "9010", "protocol": "tcp"})
neo publishes TLS 9011|n["ports"].append({"host_ip": "127.0.0.1", "target": 9011, "published": "9011", "protocol": "tcp"})
unpinned image|n["image"] = "ghcr.io/browseros-ai/neo:latest"
gate on another image|g["image"] = "alpine/socat"
privileged|n["privileged"] = True
host network|n["network_mode"] = "host"
host pid|g["pid"] = "host"
extra capability|n["cap_add"].append("NET_ADMIN")
gate capability|g["cap_add"] = ["NET_RAW"]
no cap_drop ALL|n["cap_drop"] = []
no no-new-privileges|g["security_opt"] = []
seccomp unconfined|n["security_opt"].append("seccomp:unconfined")
hosted mode off|n["environment"]["BROWSERCLAW_HOSTED_MODE"] = "0"
chromium sandbox on|n["environment"]["NEO_SANDBOX"] = "true"
recording extensions on|n["environment"]["NEO_DISABLE_BROWSEROS_EXTENSIONS"] = "false"
remote MCP on|n["environment"]["NEO_MCP_REMOTE"] = "true"
host API TCP|n["environment"]["CLAW_HOST_TCP"] = "0.0.0.0:9300"
extra NEO_ARGS|n["environment"]["NEO_ARGS"] = "--remote-debugging-address=0.0.0.0"
docker socket mount|n["volumes"].append({"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"})
other data folder|n["volumes"][0]["source"] = "/tmp/elsewhere"
extra service|c["services"]["extra"] = dict(g)
viewer service removed|c["services"].pop("viewer")
neo publishes its unchecked websockify 6080|n["ports"][0]["target"] = 6080
neo publishes 6080 next to the viewer|n["ports"].append({"host_ip": "127.0.0.1", "target": 6080, "published": "16081", "protocol": "tcp"})
viewer without the Origin plugin|v["entrypoint"] = [a for a in v["entrypoint"] if a not in ("--auth-plugin", "websockify.auth_plugins.ExpectOrigin")]
viewer accepts another Origin|v["entrypoint"][v["entrypoint"].index("--auth-source") + 1] = "http://127.0.0.1:16080 https://evil.example"
viewer accepts localhost Origin|v["entrypoint"][v["entrypoint"].index("--auth-source") + 1] = "http://localhost:16080"
viewer Origin on another port|v["entrypoint"][v["entrypoint"].index("--auth-source") + 1] = "http://127.0.0.1:16081"
viewer web_auth/extra flag|v["entrypoint"].insert(4, "--verbose")
viewer target changed|v["entrypoint"][-1] = "neo:6080"
viewer command added|v["command"] = ["--record", "/tmp/r"]
viewer own network|v.pop("network_mode"); v["networks"] = {"neo": None}
viewer host network|v["network_mode"] = "host"
viewer publishes a port|v["ports"] = [{"host_ip": "127.0.0.1", "target": 6081, "published": "16082", "protocol": "tcp"}]
viewer as root|v["user"] = "0:0"
viewer writable root|v["read_only"] = False
viewer capability|v["cap_add"] = ["NET_RAW"]
viewer mounts the profile|v["volumes"] = [dict(n["volumes"][0])]
viewer depends_on removed|v.pop("depends_on")
gate writable root|g["read_only"] = False
gate as root|g["user"] = "0:0"
gate as root:root|g["user"] = "root:root"
gate as root:nobody|g["user"] = "root:65534"
gate user 00|g["user"] = "00:0"
neo.toml writable|[v.update(read_only=False) for v in n["volumes"] if v["target"] == "/etc/neo/neo.toml"]
neo.toml from another source|[v.update(source="/tmp/neo.toml") for v in n["volumes"] if v["target"] == "/etc/neo/neo.toml"]
profile mounted into the gate|g["volumes"][0]["source"] = n["volumes"][0]["source"]
gate script from another source|g["volumes"][0]["source"] = "/tmp/mcp-gate.py"
gate volumes_from neo|g["volumes_from"] = ["neo"]
neo tmpfs|n["tmpfs"] = ["/tmp"]
neo sysctls|n["sysctls"] = {"net.ipv4.ip_forward": "1"}
neo extra_hosts|n["extra_hosts"] = ["host.docker.internal:host-gateway"]
neo devices|n["devices"] = ["/dev/dri:/dev/dri"]
neo command (Chromium flags)|n["command"] = ["--remote-debugging-address=0.0.0.0"]
neo entrypoint override|n["entrypoint"] = ["/bin/sh"]
gate entrypoint changed|g["entrypoint"] = ["/bin/sh", "-c", "socat"]
gate command added|g["command"] = ["9010", "neo", "9011"]
external network|c["networks"]["neo"] = {"name": "host", "external": True}
macvlan network|c["networks"]["neo"]["driver"] = "macvlan"
second network|n["networks"] = {"neo": None, "lan": None}
top-level secrets|c["secrets"] = {"s": {"file": "/etc/shadow"}}
top-level volumes|c["volumes"] = {"v": {}}
apparmor override|n["security_opt"].append("apparmor:docker-default")
mount propagation|n["volumes"][0]["bind"]["propagation"] = "rshared"
volume-type data mount|n["volumes"][0]["type"] = "volume"
gate depends_on removed|g.pop("depends_on")
unknown env var|n["environment"]["NEO_EXTRA"] = "1"
PUID root|n["environment"]["PUID"] = "0"
restart always|n["restart"] = "always"
EOF
  for rp in 5900 6080 6081 9010 9011; do
    python3 -I "$CHECK" --data "$data" --image "$IMAGE" --compose-dir "$ROOT/docker/browseros-neo" \
      --project bneo-test --vnc-port "$rp" --mcp-port 19010 < "$cfg" 2>/dev/null \
      && no "refuses: viewer port $rp (listens inside Neo)" || ok "refuses: viewer port $rp (listens inside Neo)"
  done
fi

# --- no automatic destructive Compose operations in the wrapper -------------
if grep -nE -- '--force-recreate|--renew-anon-volumes|compose (rm|kill)\b' "$WRAP"; then no "wrapper has no --force-recreate/rm/kill"
else ok "wrapper has no --force-recreate/rm/kill"; fi
[[ "$(grep -cE 'compose (down|stop)\b' "$WRAP")" == 2 ]] && grep -qE '^  compose stop -t 2 mcp-gate ' "$WRAP" \
  && grep -qE '^  compose down --remove-orphans$' "$WRAP" \
  && ok "only two stop/down call sites: pause's gate stop and approved down" || no "unexpected stop/down call sites"

# --- wrapper fail-closed, stub docker -------------------------------------
stub="$tmp/bin/docker"; mkdir -p "$tmp/bin"
cat > "$stub" <<EOF
#!/usr/bin/env bash
case "\$1" in
  version) echo "\${STUB_DOCKER_VERSION:-28.2.2}" ;;
  context) [[ -z "\${STUB_DOCKER_CONTEXT_FAIL:-}" ]] || exit 1
           [[ "\$2" == show ]] && echo default || echo "\${STUB_DOCKER_ENDPOINT:-unix:///var/run/docker.sock}" ;;
  info) echo stub-engine ;;
  ps) [[ -n "\${STUB_DOCKER_PS:-}" ]] && echo c1 ; true ;;
  inspect) printf '%s\n' "\${STUB_DOCKER_PS:-}" ;;
  compose) shift; exec "$REAL_DOCKER" compose "\$@" ;;
  *) echo "stub docker: unexpected \$*" >&2; exit 99 ;;
esac
EOF
chmod +x "$stub"
wrap() { env BROWSEROS_NEO_DOCKER="$stub" BROWSEROS_NEO_DATA="$data" BROWSEROS_NEO_PROJECT=bneo-test "$@"; }
expect_refused() {  # label, then env assignments / args
  local label="$1"; shift
  local out
  if out="$(wrap "$@" 2>&1)"; then no "wrapper refuses: $label"; printf '%s\n' "$out"; else ok "wrapper refuses: $label"; fi
}
if [[ -n "$REAL_DOCKER" ]] && docker compose version >/dev/null 2>&1; then
  wrap bash "$WRAP" check >"$tmp/w.out" 2>&1 && ok "wrapper check passes on a safe setup" || { no "wrapper check passes on a safe setup"; cat "$tmp/w.out"; }
  expect_refused "Docker engine < 28" STUB_DOCKER_VERSION=27.5.1 bash "$WRAP" check
  expect_refused "remote DOCKER_HOST" DOCKER_HOST=tcp://10.0.0.5:2375 bash "$WRAP" check
  expect_refused "remote docker context" STUB_DOCKER_ENDPOINT=ssh://build@10.0.0.5 bash "$WRAP" check
  expect_refused "folder in use by another container" STUB_DOCKER_PS="/other-neo|someproject|$data|" bash "$WRAP" check
  expect_refused "parent of folder mounted by another container" STUB_DOCKER_PS="/other|p2|$(dirname "$data")|" bash "$WRAP" check
  expect_refused "docker context unreadable (fail closed)" STUB_DOCKER_CONTEXT_FAIL=1 bash "$WRAP" check
  chmod 755 "$data"; expect_refused "group/world-readable profile" bash "$WRAP" check; chmod 700 "$data"
  expect_refused "missing profile folder" BROWSEROS_NEO_DATA="$tmp/absent" bash "$WRAP" check
  # A COMPOSE_FILE override publishing on all interfaces is ignored, not merged.
  printf 'services:\n  neo:\n    ports: ["0.0.0.0:6081:6080"]\n' > "$tmp/override.yaml"
  wrap env COMPOSE_FILE="$ROOT/docker/browseros-neo/compose.yaml:$tmp/override.yaml" bash "$WRAP" check >"$tmp/w.out" 2>&1 \
    && ok "COMPOSE_FILE override is ignored" || { no "COMPOSE_FILE override is ignored"; cat "$tmp/w.out"; }
fi
expect_refused "equal ports" BROWSEROS_NEO_VNC_PORT=20000 BROWSEROS_NEO_MCP_PORT=20000 bash "$WRAP" check
expect_refused "privileged port" BROWSEROS_NEO_VNC_PORT=80 bash "$WRAP" check
expect_refused "bad project name" BROWSEROS_NEO_PROJECT='../x' bash "$WRAP" check
expect_refused "relative profile folder" BROWSEROS_NEO_DATA=rel/data bash "$WRAP" check
expect_refused "profile folder inside the repo" BROWSEROS_NEO_DATA="$ROOT/data/neo" bash "$WRAP" check
ln -s "$(dirname "$data")" "$tmp/alias"
expect_refused "profile folder through a symlinked parent" BROWSEROS_NEO_DATA="$tmp/alias/data" bash "$WRAP" check
expect_refused "profile folder with trailing slash" BROWSEROS_NEO_DATA="$data/" bash "$WRAP" check
expect_refused "profile folder with dot-dot" BROWSEROS_NEO_DATA="$data/../data" bash "$WRAP" check
main_tree="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
if [[ -n "$main_tree" && "$(realpath -m "$main_tree/..")" != "$ROOT" ]]; then
  expect_refused "profile folder inside the main checkout" BROWSEROS_NEO_DATA="$(realpath -m "$main_tree/..")/neo-data" bash "$WRAP" check
fi

# --- Hermes snippet ---------------------------------------------------------
snip="$(BROWSEROS_NEO_MCP_PORT=29999 bash "$WRAP" hermes-snippet)"
python3 -I - "$snip" <<'EOF' && ok "hermes snippet: loopback gate URL, untrusted, risky tools excluded, no headers" || no "hermes snippet"
import sys, yaml
s = yaml.safe_load(sys.argv[1])["mcp_servers"]["browseros_neo"]
assert s["url"] == "http://127.0.0.1:29999/mcp", s["url"]
assert s["trust"] == "untrusted"
assert {"credentials", "payment_cards", "solve_captcha"} <= set(s["tools"]["exclude"])
assert "headers" not in s and s["timeout"] > 60
EOF
cmds="$(BROWSEROS_NEO_MCP_PORT=29999 bash "$WRAP" hermes-commands)"
python3 -I - "$cmds" <<'EOF' && ok "hermes-commands: native config set, disabled first, exclusions before enable, enabled last" || { no "hermes-commands"; printf '%s\n' "$cmds"; }
import shlex, sys
lines = [" ".join(shlex.split(l)) for l in sys.argv[1].splitlines()]
assert all(l.startswith("hermes config set mcp_servers.browseros_neo.") for l in lines), lines
assert lines[0].endswith(".enabled false") and lines[-1].endswith(".enabled true"), lines
ex = [i for i, l in enumerate(lines) if ".tools.exclude " in l]
assert ex and lines[ex[0]].endswith("[credentials,payment_cards,solve_captcha]"), lines
assert any(l.endswith(".trust untrusted") for l in lines)
assert any(l.endswith(".url http://127.0.0.1:29999/mcp") for l in lines)
EOF

# --- MCP gate: forwards, then refuses once stopped --------------------------
python3 -I - "$ROOT/docker/browseros-neo/mcp-gate.py" <<'EOF' && ok "mcp-gate forwards bytes and refuses after stop" || no "mcp-gate forwards/refuses"
import socket, subprocess, sys, threading, time
up = socket.create_server(("127.0.0.1", 0)); up_port = up.getsockname()[1]
def echo():
    c, _ = up.accept(); c.sendall(c.recv(100).upper()); c.close()
threading.Thread(target=echo, daemon=True).start()
probe = socket.socket(); probe.bind(("127.0.0.1", 0)); gate_port = probe.getsockname()[1]; probe.close()
gate = subprocess.Popen([sys.executable, "-I", sys.argv[1], str(gate_port), "127.0.0.1", str(up_port)])
try:
    for _ in range(50):
        try:
            s = socket.create_connection(("127.0.0.1", gate_port), timeout=1); break
        except OSError:
            time.sleep(0.1)
    s.settimeout(5); s.sendall(b"ping"); assert s.recv(100) == b"PING"; s.close()
finally:
    gate.terminate(); gate.wait(timeout=5)
try:
    socket.create_connection(("127.0.0.1", gate_port), timeout=1)
    sys.exit("gate port still open after stop")
except OSError:
    pass
EOF

# --- lifecycle state machine, fake Docker -----------------------------------
# A bounded fake engine: containers are files, the gate is a real loopback
# listener (killed on stop, and by `timeout` after 120 s at most), `compose
# config` goes to the real CLI. A fake `sleep` records the drain length and
# can run a hook mid-drain. No real container is created or touched.
if [[ -n "$REAL_DOCKER" ]] && docker compose version >/dev/null 2>&1; then
fk="$tmp/fake"; mkdir -p "$fk/run" "$fk/exist" "$fk/bin"
read -r VNC MCP < <(python3 -I -c 'import socket
a=[socket.socket() for _ in range(2)]
[s.bind(("127.0.0.1",0)) for s in a]; print(*[s.getsockname()[1] for s in a])')
cat > "$fk/bin/docker" <<EOF
#!/usr/bin/env bash
# fake docker for test_browseros_neo.sh; state in \$FK
FK="$fk"; REAL="$REAL_DOCKER"; IMAGE="$IMAGE"; CDIR="$ROOT/docker/browseros-neo"
EOF
cat >> "$fk/bin/docker" <<'EOF'
echo "$*" >> "$FK/calls"; echo "$*" >> "$FK/calls.all"
P="${BROWSEROS_NEO_PROJECT:-browseros-neo}"
running() { [[ -e "$FK/run/$1" ]]; }
exists() { [[ -e "$FK/exist/$1" ]]; }
start() {
  : > "$FK/run/$1"; : > "$FK/exist/$1"
  if [[ "$1" == mcp-gate && -z "${FAKE_GATE_NOLISTEN:-}" ]]; then
    timeout 120 python3 -I -c 'import socket,sys
s=socket.create_server(("127.0.0.1", int(sys.argv[1]))); open(sys.argv[2],"w").write("up")
while True: s.accept()[0].close()' "$BROWSEROS_NEO_MCP_PORT" "$FK/gate.ready" >/dev/null 2>&1 9>&- &
    echo $! > "$FK/gate.pid"
    for _ in $(seq 1 50); do [[ -e "$FK/gate.ready" ]] && break; /bin/sleep 0.05; done
  fi
}
stop() {
  [[ -z "${FAKE_STOP_NOOP:-}" ]] || return 0
  rm -f "$FK/run/$1"
  if [[ "$1" == mcp-gate && -f "$FK/gate.pid" ]]; then
    kill "$(cat "$FK/gate.pid")" 2>/dev/null; wait "$(cat "$FK/gate.pid")" 2>/dev/null
    for _ in $(seq 1 50); do python3 -I -c 'import socket,sys
s=socket.socket(); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) else 1)' "$BROWSEROS_NEO_MCP_PORT" && break; /bin/sleep 0.05; done
    rm -f "$FK/gate.pid" "$FK/gate.ready"
  fi
}
case "$1" in
  version) echo 28.2.2 ;;
  context) [[ "$2" == show ]] && echo default || echo unix:///var/run/docker.sock ;;
  info) echo "${FAKE_ENGINE_ID:-engine-A}" ;;
  image) exit 0 ;;
  ps)
    if [[ "$*" == *label=com.docker.compose.service=mcp-gate* ]]; then
      [[ -z "${FAKE_FOREIGN_GATE:-}" ]] || echo "$FAKE_FOREIGN_GATE $IMAGE"
    else running neo && echo "$P-neo-1"; true; fi ;;
  inspect)
    [[ -z "${FAKE_INSPECT_FAIL:-}" ]] || { echo "fake: inspect failed" >&2; exit 1; }
    fmt="${*: -1}"; shift
    for id in "$@"; do
      [[ "$id" == --format ]] && break
      svc="${id#"$P"-}"; svc="${svc%-1}"
      case "$fmt" in
        *NetworkSettings.Ports*)
          case "$svc" in
            neo) echo "neo 6081/tcp=${FAKE_NEO_BIND:-127.0.0.1}:$BROWSEROS_NEO_VNC_PORT" ;;
            viewer) echo "viewer" ;;
            *) echo "mcp-gate 9010/tcp=127.0.0.1:$BROWSEROS_NEO_MCP_PORT" ;;
          esac ;;
        *Config.Image*) echo "${FAKE_NEO_IMAGE:-$IMAGE}|/data=${FAKE_NEO_DATA:-$BROWSEROS_NEO_DATA};/etc/neo/neo.toml=$CDIR/neo.toml;" ;;
        *) echo "/$id|$P|$BROWSEROS_NEO_DATA|$CDIR/neo.toml|" ;;
      esac
    done ;;
  compose)
    orig=("$@"); shift
    while [[ $# -gt 0 ]]; do case "$1" in --env-file|-p|--project-directory|-f) shift 2 ;; *) break ;; esac; done
    sub="$1"; shift
    case "$sub" in
      config) exec "$REAL" "${orig[@]}" ;;
      ps)
        q=0 all=0 status=""; svcs=()
        while [[ $# -gt 0 ]]; do case "$1" in -q) q=1 ;; -aq) q=1; all=1 ;; -a) all=1 ;; --status) status="$2"; shift ;; *) svcs+=("$1") ;; esac; shift; done
        [[ -z "${FAKE_PS_FAIL:-}" ]] || exit 1
        (( ${#svcs[@]} )) || svcs=(neo viewer mcp-gate)
        for s in "${svcs[@]}"; do
          if running "$s" || { (( all )) && exists "$s"; }; then (( q )) && echo "$P-$s-1" || echo "$P-$s-1 running"; fi
        done ;;
      up)
        [[ -z "${FAKE_UP_FAIL:-}" ]] || exit 1
        for a in "$@"; do case "$a" in neo|viewer|mcp-gate) running "$a" || start "$a" ;; esac; done ;;
      stop)
        svcs=(); while [[ $# -gt 0 ]]; do case "$1" in -t) shift ;; *) svcs+=("$1") ;; esac; shift; done
        (( ${#svcs[@]} )) || svcs=(mcp-gate viewer neo)
        for s in "${svcs[@]}"; do stop "$s"; done ;;
      down) for s in mcp-gate viewer neo; do stop "$s"; rm -f "$FK/exist/$s"; done ;;
      pull) exit 0 ;;
      *) echo "fake compose: unexpected $sub" >&2; exit 99 ;;
    esac ;;
  *) echo "fake docker: unexpected $*" >&2; exit 99 ;;
esac
EOF
cat > "$fk/bin/sleep" <<EOF
#!/usr/bin/env bash
echo "\$1" >> "$fk/sleeps"
if [[ -n "\${FAKE_SLEEP_HOOK:-}" && "\$1" != 0.5 ]]; then eval "\$FAKE_SLEEP_HOOK"; fi
exit 0
EOF
chmod +x "$fk/bin/docker" "$fk/bin/sleep"
fdata="$tmp/life/data"; mkdir -p "$fdata"; chmod 700 "$fdata"
fstate="$tmp/life/state-bneo-life"
life() {  # life [VAR=val ...] cmd [args]; bounded, output in $fk/out, rc returned
  echo "== $*" >> "$fk/calls.all"
  env PATH="$fk/bin:$PATH" BROWSEROS_NEO_DOCKER="$fk/bin/docker" BROWSEROS_NEO_DATA="$fdata" \
    BROWSEROS_NEO_PROJECT=bneo-life BROWSEROS_NEO_VNC_PORT="$VNC" BROWSEROS_NEO_MCP_PORT="$MCP" \
    timeout 60 "$@" > "$fk/out" 2>&1
}
fake_gate_up() { life bash "$fk/bin/docker" compose -p bneo-life up -d mcp-gate; }  # raw bypass of the wrapper
gate_listening() { python3 -I -c 'import socket,sys
s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1])))==0 else 1)' "$MCP"; }
expect() {  # expect LABEL want_rc(0|nz) pattern [!antipattern] -- cmd...
  local label="$1" want="$2" pat="$3" anti="" rc=0; shift 3
  [[ "$1" == '!'* ]] && { anti="${1#!}"; shift; }
  shift  # --
  life "$@" || rc=$?
  local rc_ok=0
  if [[ "$want" == 0 ]]; then (( rc == 0 )) && rc_ok=1; else (( rc != 0 )) && rc_ok=1; fi
  if (( rc_ok )) && grep -qE -- "$pat" "$fk/out" && { [[ -z "$anti" ]] || ! grep -qiE -- "$anti" "$fk/out"; }; then
    ok "$label"
  else no "$label (rc=$rc)"; sed 's/^/      /' "$fk/out"; fi
}
UNSAFE='browser is yours|safe to type'
W="$fstate/agent-paused"

expect "up: checks, starts neo, opens the gate" 0 'agent path: OPEN' -- bash "$WRAP" up
[[ -f "$fstate/manifest" ]] && grep -q '^wall_timeout=60$' "$fstate/manifest" && grep -q '^engine_id=engine-A$' "$fstate/manifest" \
  && ok "up writes the manifest (pinned budget, engine)" || no "up writes the manifest"
gate_listening && ok "gate listener is up" || no "gate listener is up"

: > "$fk/sleeps"
expect "pause --no-wait: CLOSED path but DRAINING, says do not type" 0 'DRAINING, not drained' "!$UNSAFE|^\[agent-ops-kit\] CLOSED:" -- bash "$WRAP" pause --no-wait
grep -qx 'state=DRAINING' "$W" && ok "--no-wait records DRAINING" || no "--no-wait records DRAINING"
[[ ! -s "$fk/sleeps" ]] && ok "--no-wait does not wait" || no "--no-wait does not wait"
! gate_listening && ok "gate port refuses after pause" || no "gate port refuses after pause"
expect "status after --no-wait: DRAINING, not CLOSED" 0 'agent path: DRAINING' "!$UNSAFE" -- bash "$WRAP" status
expect "up while DRAINING keeps the gate closed" 0 'stays closed' -- bash "$WRAP" up
fake_gate_up
expect "up refuses (does not stop) a gate running under a pause record" nz 'pause recorded .* but mcp-gate is running' -- bash "$WRAP" up
[[ -e "$fk/run/mcp-gate" ]] && ok "  ... and left it running (no automatic stop)" || no "  ... up stopped the gate itself"
life bash "$WRAP" pause --no-wait
! gate_listening && ok "gate still closed after up" || no "gate still closed after up"

: > "$fk/sleeps"
expect "pause: drains pinned budget, CLOSED with explicit limits" 0 'not proof the browser is quiet' "!$UNSAFE" -- bash "$WRAP" pause
[[ "$(cat "$fk/sleeps")" == 65 ]] && ok "pause waits pinned 60 s + 5 s" || no "pause wait was '$(cat "$fk/sleeps")'"
expect "status after drain: CLOSED" 0 'agent path: CLOSED \(gate stopped and pause drained' -- bash "$WRAP" status

fake_gate_up
expect "status: raw gate start under a pause record is INCONSISTENT" nz 'INCONSISTENT' "!agent path: (PAUSED|CLOSED)" -- bash "$WRAP" status

expect "pause: gate reopened mid-drain is detected" nz 'reopened during the drain' "!$UNSAFE|^\[agent-ops-kit\] CLOSED:" \
  -- env FAKE_SLEEP_HOOK="bash $fk/bin/docker compose -p bneo-life up -d mcp-gate" bash "$WRAP" pause
grep -qx 'state=DRAINING' "$W" && ok "mid-drain reopen leaves DRAINING, not CLOSED" || no "mid-drain reopen state: $(cat "$W")"

expect "pause: stop that silently fails is not reported closed" nz 'mcp-gate is running; agent path NOT closed' "!$UNSAFE" \
  -- env FAKE_STOP_NOOP=1 bash "$WRAP" pause --no-wait
life bash "$fk/bin/docker" compose -p bneo-life stop mcp-gate
expect "pause: another project's pilot gate fails closed" nz 'another pilot gate is running' \
  -- env FAKE_FOREIGN_GATE="other-gate-1 otherproj" bash "$WRAP" pause --no-wait
expect "status: unreadable gate state is UNKNOWN" nz 'UNKNOWN' -- env FAKE_PS_FAIL=1 bash "$WRAP" status

# lifecycle lock: a held lock refuses, and nothing reaches the engine's `up`
: > "$fk/calls"
( exec 8>>"$fstate/lock"; flock 8; /bin/sleep 5 ) & lp=$!
for _ in $(seq 1 50); do flock -n "$fstate/lock" true 2>/dev/null || break; /bin/sleep 0.05; done
expect "resume refused while another lifecycle command holds the lock" nz 'lifecycle lock .* is held' -- bash "$WRAP" resume
expect "pause refused while the lock is held" nz 'lifecycle lock .* is held' -- bash "$WRAP" pause --no-wait
wait "$lp" 2>/dev/null
flock -w 10 "$fstate/lock" true || no "test lock holder did not release"
! grep -q 'compose.* up ' "$fk/calls" && ok "locked resume started nothing" || no "locked resume started something"

# resume re-checks everything BEFORE starting the gate
resume_refused() {  # label pattern [env...]
  local label="$1" pat="$2"; shift 2
  : > "$fk/calls"
  expect "resume refused: $label" nz "$pat" -- env "$@" bash "$WRAP" resume
  ! grep -q 'up -d --no-deps' "$fk/calls" && ! gate_listening && ok "  ... and the gate was not started" || no "  ... gate started on $label"
}
resume_refused "different engine" 'engine_id changed' FAKE_ENGINE_ID=engine-B
cp "$fstate/manifest" "$tmp/manifest.bak"
sed -i 's/^neo_toml_sha=.*/neo_toml_sha=0000/' "$fstate/manifest"
resume_refused "neo.toml drift since up" 'neo_toml_sha changed'
expect "pause with neo.toml drift: closes the gate but refuses a drain claim" nz 'DRAINING with unknown budget' "!$UNSAFE|^\[agent-ops-kit\] CLOSED:" -- bash "$WRAP" pause
grep -qx 'deadline=unknown' "$W" && ok "drift pause records an unknown deadline" || no "drift pause deadline"
cp "$tmp/manifest.bak" "$fstate/manifest"
sed -i 's/^config_sha=.*/config_sha=0000/' "$fstate/manifest"
resume_refused "rendered config changed since up" 'config_sha changed'
cp "$tmp/manifest.bak" "$fstate/manifest"
resume_refused "running neo on another image" 'not the pinned image' FAKE_NEO_IMAGE=ghcr.io/browseros-ai/neo:latest
resume_refused "running neo on another profile folder" 'does not mount' FAKE_NEO_DATA=/elsewhere
mv "$fstate/manifest" "$tmp/manifest.moved"
resume_refused "no manifest from up" 'no manifest'
mv "$tmp/manifest.moved" "$fstate/manifest"

# exposure and inspect errors fail closed WITHOUT stopping anything, and name the approved command
resume_refused "inspect error" 'cannot inspect' FAKE_INSPECT_FAIL=1
life bash "$fk/bin/docker" compose -p bneo-life up -d neo
: > "$fk/calls"
expect "resume: exposed viewer refuses, says nothing was stopped, names approved down" nz \
  'EXPOSED: unexpected live binding: neo 6081/tcp=0.0.0.0.*Nothing was stopped.*BROWSEROS_NEO_PROJECT=bneo-life .*down --approve-down' \
  -- env FAKE_NEO_BIND=0.0.0.0 bash "$WRAP" resume
[[ -e "$fk/run/neo" ]] && ! grep -qE ' (stop|down|rm|kill)( |$)' "$fk/calls" && ok "exposed viewer: nothing stopped automatically" || no "exposed viewer: wrapper stopped something"
expect "status: exposed viewer reported EXPOSED" nz 'EXPOSED' -- env FAKE_NEO_BIND=0.0.0.0 bash "$WRAP" status
expect "status: inspect error is UNKNOWN, not OPEN/CLOSED" nz 'UNKNOWN' -- env FAKE_INSPECT_FAIL=1 bash "$WRAP" status

expect "resume: listener never answers -> refuses, may be OPEN, tells operator to pause" nz 'did not accept connections.*may be OPEN.*Nothing was stopped.*pause' -- env FAKE_GATE_NOLISTEN=1 bash "$WRAP" resume
[[ -e "$W" && -e "$fk/run/mcp-gate" ]] && ok "failed resume keeps the pause record and does not stop the gate" || no "failed resume state"
expect "status after failed resume: INCONSISTENT, not CLOSED" nz 'INCONSISTENT' -- bash "$WRAP" status
expect "operator pause closes it again" 0 'DRAINING, not drained' -- bash "$WRAP" pause --no-wait
expect "resume: all checks pass -> OPEN, record removed" 0 'agent path OPEN' -- bash "$WRAP" resume
[[ ! -e "$W" ]] && gate_listening && ok "resume removed the pause record, gate listening" || no "resume final state"
expect "status after resume: OPEN" 0 'agent path: OPEN' -- bash "$WRAP" status
# up with existing containers never recreates: drift refuses before any compose up
cp "$fstate/manifest" "$tmp/manifest.bak2"
sed -i 's/^gate_sha=.*/gate_sha=0000/' "$fstate/manifest"
: > "$fk/calls"
expect "up refuses mcp-gate.py drift with existing containers (no recreate)" nz 'up refused: gate_sha changed.*never recreated automatically.*down --approve-down' -- bash "$WRAP" up
! grep -qE 'compose.* (up|stop|down) ' "$fk/calls" && ok "  ... and touched no container" || no "  ... drifted up touched containers"
cp "$tmp/manifest.bak2" "$fstate/manifest"
: > "$fk/calls"
expect "down without approval is refused and does nothing" nz 'Nothing was done.*down --approve-down' -- bash "$WRAP" down
! grep -q 'compose' "$fk/calls" && [[ -e "$fk/run/neo" ]] && ok "  ... no compose call, neo still running" || no "  ... unapproved down acted"
expect "down --approve-down" 0 'stopped; profile kept' -- bash "$WRAP" down --approve-down
expect "status after down without pause: CLOSED but not drained" 0 'CLOSED but not drained' -- bash "$WRAP" status
# Every engine call the wrapper made in this whole run, grouped by wrapper command:
# no --force-recreate anywhere, compose stop only under `pause` (mcp-gate only),
# compose down only under `down --approve-down`, every compose up uses --no-recreate.
python3 -I - "$fk/calls.all" <<'PYEOF' && ok "lifecycle audit: no force-recreate; stop only in pause (gate); down only approved; up --no-recreate" || no "lifecycle audit"
import re, sys
cmd, bad = "", []
for line in open(sys.argv[1]):
    line = line.rstrip("\n")
    if line.startswith("== "):
        cmd = line[3:]; continue
    wrapper = "browseros-neo.sh" in cmd
    if not wrapper or not line.startswith("compose "):
        continue
    sub = re.sub(r"^compose (?:(?:--env-file|-p|--project-directory|-f) \S+ )*", "", line)
    if "--force-recreate" in line:
        bad.append((cmd, line))
    verb = sub.split()[0]
    if verb == "stop" and (" pause" not in cmd or sub.split()[-1] != "mcp-gate"):
        bad.append((cmd, line))
    if verb == "down" and "--approve-down" not in cmd:
        bad.append((cmd, line))
    if verb == "up" and "--no-recreate" not in sub and "FAKE_SLEEP_HOOK" not in cmd:  # the hook is the test's raw bypass
        bad.append((cmd, line))
    if verb in ("rm", "kill", "restart"):
        bad.append((cmd, line))
for b in bad:
    print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
[[ -f "$fk/gate.pid" ]] && kill "$(cat "$fk/gate.pid")" 2>/dev/null
fi

echo "browseros-neo: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
