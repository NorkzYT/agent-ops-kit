#!/usr/bin/env bash
# test_healthcheck.sh — prove the cliproxyapi healthcheck is no longer a
# false-green. It extracts the real CMD-SHELL command from docker-compose.yml
# and runs it against (a) a live server that returns 401 -> must be HEALTHY, and
# (b) a dead port -> must be UNHEALTHY. Regression for the `|| exit 0` bug.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PORT=18317
fail=0

# Pull the exact healthcheck command (the line that probes /v1/models) and point
# it at the test port, so the test tracks whatever the compose file actually has.
raw="$(grep -F '8317/v1/models' docker-compose.yml | head -n1)"
cmd="$(sed -E 's/.*"CMD-SHELL", "(.*)"\].*/\1/' <<<"$raw")"
cmd="${cmd//8317/$PORT}"
[[ -n "$cmd" && "$cmd" != "$raw" ]] || { echo "FAIL: could not extract healthcheck command"; exit 1; }
echo "healthcheck cmd: $cmd"

# (a) server up, returns 401 to everything -> healthcheck must succeed
python3 - "$PORT" >/dev/null 2>&1 <<'PY' &
import sys, http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"unauthorized"}')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
srv=$!
for _ in $(seq 1 50); do curl -s -o /dev/null "http://127.0.0.1:$PORT/v1/models" 2>/dev/null && break; sleep 0.1; done

if sh -c "$cmd" >/dev/null 2>&1; then echo "  ok   up (401) -> healthy"; else echo "  FAIL up (401) reported unhealthy"; fail=1; fi

kill "$srv" 2>/dev/null; wait "$srv" 2>/dev/null
# give the OS a moment to release the port
for _ in $(seq 1 20); do curl -s -o /dev/null "http://127.0.0.1:$PORT/v1/models" 2>/dev/null || break; sleep 0.1; done

# (b) server down -> healthcheck must fail (this is what the old `|| exit 0` broke)
if sh -c "$cmd" >/dev/null 2>&1; then echo "  FAIL down -> still reported healthy (false green)"; fail=1; else echo "  ok   down -> unhealthy"; fi

[[ "$fail" -eq 0 ]] && echo "PASS: healthcheck regression" || echo "FAIL: healthcheck regression"
exit "$fail"
