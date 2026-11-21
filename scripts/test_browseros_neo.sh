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
trap 'rm -rf "$tmp"' EXIT
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
  run_check() { python3 -I "$CHECK" --data "$data" --image "$IMAGE" --vnc-port 16080 --mcp-port 19010 < "$1" 2>"$tmp/check.err"; }
  run_check "$cfg" && ok "real config accepted" || { no "real config accepted"; cat "$tmp/check.err"; }

  # Each mutation is a Python expression over `c` (the config dict); all must be refused.
  while IFS='|' read -r label expr; do
    [[ -n "$label" ]] || continue
    python3 -I - "$cfg" "$tmp/mut.json" "$expr" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1])); n = c["services"]["neo"]; g = c["services"]["mcp-gate"]
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
extra service|c["services"]["viewer"] = dict(g)
gate writable root|g["read_only"] = False
gate as root|g["user"] = "0:0"
neo.toml writable|[v.update(read_only=False) for v in n["volumes"] if v["target"] == "/etc/neo/neo.toml"]
EOF
fi

# --- wrapper fail-closed, stub docker -------------------------------------
stub="$tmp/bin/docker"; mkdir -p "$tmp/bin"
cat > "$stub" <<EOF
#!/usr/bin/env bash
case "\$1" in
  version) echo "\${STUB_DOCKER_VERSION:-28.2.2}" ;;
  context) [[ "\$2" == show ]] && echo default || echo "\${STUB_DOCKER_ENDPOINT:-unix:///var/run/docker.sock}" ;;
  ps) printf '%s\n' "\${STUB_DOCKER_PS:-}" ;;
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
  expect_refused "folder in use by another container" STUB_DOCKER_PS="other-neo someproject" bash "$WRAP" check
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

echo "browseros-neo: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
