#!/usr/bin/env bash
# test_browseros_neo_hermes_registration.sh — opt-in: prove the BrowserOS neo
# Hermes entry's tool exclusions hold at REGISTRATION, not just in a listing.
#
# In a throwaway HERMES_HOME it runs the native `hermes config set` commands
# that `browseros-neo.sh hermes-commands` prints, starts a stdlib fake MCP
# server (Streamable HTTP, JSON responses) that lists credentials,
# payment_cards, solve_captcha and two browser tools, and calls Hermes' real
# discover_mcp_tools() with the installed runtime's interpreter. It then reads
# the tool registry. A control home without exclusions must register all five.
# Never reads or writes the live Hermes home; starts no containers. Skips
# (exit 0) when Hermes is not installed. Bounded: every process is timed out.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAP="$ROOT/scripts/browseros-neo.sh"

command -v hermes >/dev/null 2>&1 || { echo "skip: hermes not installed"; exit 0; }
# A throwaway home outside ~/.hermes makes Hermes build a whole runtime for
# that data root (~2 GB, minutes); its own scratch area does not.
base="$HOME/.hermes/cache/scratch"; [[ -d "$base" && ! -L "$base" ]] || base="${TMPDIR:-/tmp}"
tmp="$(mktemp -d -p "$base" neo-hermes-reg.XXXXXX)"; [[ -d "$tmp" && "$tmp" != / ]] || { echo "mktemp failed" >&2; exit 1; }
fake_pid=""
# BROWSEROS_NEO_TEST_KEEP=1 keeps the throwaway homes (and prints the folder) instead of deleting them.
if [[ "${BROWSEROS_NEO_TEST_KEEP:-}" == 1 ]]; then
  trap '[[ -n "$fake_pid" ]] && kill "$fake_pid" 2>/dev/null; echo "scratch kept: $tmp"' EXIT
else
  trap '[[ -n "$fake_pid" ]] && kill "$fake_pid" 2>/dev/null; rm -rf "$tmp"' EXIT
fi
pass=0; fail=0
ok() { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no() { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

# The installed runtime's interpreter and import path, from Hermes itself.
mkdir -p "$tmp/probe-home"
HERMES_HOME="$tmp/probe-home" timeout 60 hermes --run-module site > "$tmp/site.txt" 2>/dev/null \
  && HERMES_HOME="$tmp/probe-home" timeout 60 hermes --run-module sysconfig > "$tmp/sysconfig.txt" 2>/dev/null \
  || { echo "skip: this Hermes cannot report its runtime (--run-module)"; exit 0; }
read -r PY PYPATH < <(python3 -I - "$tmp/site.txt" "$tmp/sysconfig.txt" <<'EOF'
import ast, json, re, sys
site = open(sys.argv[1]).read()
paths = ast.literal_eval(re.search(r"sys\.path = (\[.*?\])", site, re.S).group(1))
prefix = re.search(r'^\s*prefix = "([^"]+)"', open(sys.argv[2]).read(), re.M).group(1)
print(prefix + "/bin/python3", json.dumps([p for p in paths if "__editable__" not in p]))
EOF
) || { no "read the Hermes runtime path"; exit 1; }
[[ -x "$PY" ]] || { no "Hermes runtime interpreter $PY"; exit 1; }

port="$(python3 -I -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
cat > "$tmp/fake_mcp.py" <<'EOF'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
TOOLS = ["navigate_page", "take_snapshot", "credentials", "payment_cards", "solve_captcha"]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        if obj is not None: self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "s1"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self): self._send(405)
    def do_DELETE(self): self._send(200)
    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        m, i = msg.get("method"), msg.get("id")
        with open(sys.argv[2], "a") as f:
            f.write(str(m) + "\n")
        if i is None: return self._send(202)
        if m == "initialize":
            r = {"protocolVersion": msg["params"].get("protocolVersion", "2025-03-26"),
                 "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-neo", "version": "0"}}
        elif m == "tools/list":
            r = {"tools": [{"name": t, "description": t, "inputSchema": {"type": "object", "properties": {}}} for t in TOOLS]}
        elif m == "ping":
            r = {}
        else:
            return self._send(200, {"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "unsupported"}})
        self._send(200, {"jsonrpc": "2.0", "id": i, "result": r})
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
EOF
cat > "$tmp/probe.py" <<'EOF'
import json, os, sys
sys.path[:0] = json.loads(sys.argv[2])
home = os.environ["HERMES_HOME"]
assert home.startswith(sys.argv[1] + "/"), "refusing: HERMES_HOME is not the throwaway home"
import hermes_bootstrap  # noqa: F401  (same start-up as the hermes launcher)
from tools.mcp_tool_discovery import discover_mcp_tools
from tools.registry import registry
from tools import mcp_tool as core
if len(sys.argv) > 3 and sys.argv[3] == "reconcile":
    # The running gateway's no-restart path: its housekeeping tick calls this.
    from tools.mcp_tool_discovery import reconcile_mcp_servers_with_config
    reconcile_mcp_servers_with_config()
else:
    discover_mcp_tools()
print(json.dumps({"tools": sorted(registry.get_tool_names_for_toolset("mcp-browseros_neo")),
                  "trust": {str(k): v for k, v in core._server_trust_levels.items()}}), flush=True)
os._exit(0)
EOF
timeout 300 python3 -I "$tmp/fake_mcp.py" "$port" "$tmp/requests.log" & fake_pid=$!
for _ in $(seq 1 50); do python3 -I -c 'import socket,sys; socket.create_connection(("127.0.0.1",int(sys.argv[1])),1)' "$port" 2>/dev/null && break; sleep 0.1; done

register() {  # register HOME_NAME with_exclusions(0|1) -> JSON on stdout
  local home="$tmp/$1" cmd
  mkdir -p "$home"
  while IFS= read -r cmd; do
    [[ "$2" == 1 || "$cmd" != *".tools.exclude "* ]] || continue
    # shellcheck disable=SC2086  # the printed commands are shell words
    eval "HERMES_HOME=\"$home\" timeout 60 $cmd" >/dev/null || { echo "config set failed: $cmd" >&2; return 1; }
  done < <(BROWSEROS_NEO_MCP_PORT="$port" bash "$WRAP" hermes-commands 2>/dev/null)
  HERMES_HOME="$home" timeout 90 "$PY" -I "$tmp/probe.py" "$tmp" "$PYPATH" 2>"$tmp/$1.err"
}

out="$(register home-excl 1)" || { no "register with exclusions"; cat "$tmp/home-excl.err"; }
python3 -I - "$out" <<'EOF' && ok "registered tools exclude credentials/payment_cards/solve_captcha; trust untrusted" || { no "registration with exclusions: $out"; tail -5 "$tmp/home-excl.err"; }
import json, sys
r = json.loads(sys.argv[1])
assert r["tools"] == ["mcp__browseros_neo__navigate_page", "mcp__browseros_neo__take_snapshot"], r
assert r["trust"].get("browseros_neo") == "untrusted", r
EOF
ctl="$(register home-ctl 0)" || { no "control registration"; cat "$tmp/home-ctl.err"; }
python3 -I - "$ctl" <<'EOF' && ok "control without exclusions registers all five (the probe sees them)" || no "control registration: $ctl"
import json, sys
r = json.loads(sys.argv[1])
assert len(r["tools"]) == 5 and "mcp__browseros_neo__credentials" in r["tools"], r
EOF
grep -q 'enabled: true' "$tmp/home-excl/config.yaml" && ok "throwaway config ends enabled" || no "throwaway config enabled"

# Staged: every command but the last (enabled true). The gateway's MCP config
# reconcile (no restart) must connect nothing; after the last one it must
# register the filtered tools. Shows the last command is the activation.
home="$tmp/home-staged"; mkdir -p "$home"; mapfile -t cmds < <(BROWSEROS_NEO_MCP_PORT="$port" bash "$WRAP" hermes-commands 2>/dev/null)
staged_ok=1
for cmd in "${cmds[@]:0:${#cmds[@]}-1}"; do
  eval "HERMES_HOME=\"$home\" timeout 60 $cmd" >/dev/null || staged_ok=0
done
: > "$tmp/requests.log"
pre="$(HERMES_HOME="$home" timeout 90 "$PY" -I "$tmp/probe.py" "$tmp" "$PYPATH" reconcile 2>"$tmp/staged-pre.err")" || staged_ok=0
pre_reqs="$(wc -l < "$tmp/requests.log")"
python3 -I -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["tools"] == [], r' "$pre" 2>/dev/null && (( staged_ok && pre_reqs == 0 )) \
  && ok "before 'enabled true': reconcile registers no tool and sends the server no request" || no "staged pre-enable: $pre (requests $pre_reqs)"
eval "HERMES_HOME=\"$home\" timeout 60 ${cmds[-1]}" >/dev/null && [[ "${cmds[-1]}" == *".enabled true" ]] || no "staged final command: ${cmds[-1]}"
post="$(HERMES_HOME="$home" timeout 90 "$PY" -I "$tmp/probe.py" "$tmp" "$PYPATH" reconcile 2>"$tmp/staged-post.err")"
python3 -I -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["tools"] == ["mcp__browseros_neo__navigate_page", "mcp__browseros_neo__take_snapshot"], r' "$post" 2>/dev/null \
  && ok "after 'enabled true': reconcile alone (no restart) registers the two filtered tools" || { no "staged post-enable: $post"; tail -5 "$tmp/staged-post.err"; }

echo "browseros-neo hermes registration: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
