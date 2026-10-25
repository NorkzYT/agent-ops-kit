#!/usr/bin/env bash
# test_healthcheck.sh — prove the cliproxyapi healthcheck reports real liveness.
# It extracts the exact CMD-SHELL command from docker-compose.yml (whichever
# form the file uses — inline or multi-line array, whatever path it probes) and
# runs it against a local mock, so the test tracks whatever the compose file
# actually checks:
#   (a) endpoint returns 200   -> HEALTHY
#   (b) endpoint returns a 5xx -> UNHEALTHY  (wget errors on a server error)
#   (c) dead port              -> UNHEALTHY  (regression for the old `|| exit 0`)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PORT=18317
fail=0

# The cliproxyapi healthcheck is the one quoted string in the compose file that
# probes the proxy's local port (127.0.0.1:8317). Keying off the host:port makes
# extraction independent of the probed path (/healthz, /v1/models, …) and of
# whether `test:` uses the inline or the multi-line array form. No eval, no YAML
# guesswork — just the literal command string, retargeted at the test port.
raw="$(grep -oE '"[^"]*127\.0\.0\.1:8317[^"]*"' docker-compose.yml | head -n1)"
cmd="${raw%\"}"; cmd="${cmd#\"}"          # strip the surrounding double quotes
cmd="${cmd//8317/$PORT}"                  # retarget at the throwaway test port
[[ -n "$cmd" ]] || { echo "FAIL: could not extract healthcheck command"; exit 1; }
echo "healthcheck cmd: $cmd"

MOCK_PID=""
# Start a mock that answers every GET with a fixed status; sets MOCK_PID.
start_mock() { # $1 = port  $2 = http status
  python3 - "$1" "$2" >/dev/null 2>&1 <<'PY' &
import sys, http.server
port, status = int(sys.argv[1]), int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(status); self.end_headers(); self.wfile.write(b'x')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
  MOCK_PID=$!
}
stop_mock() { [[ -n "$MOCK_PID" ]] && kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""; }
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
wait_up()   { for _ in $(seq 1 50); do port_open "$1" && return 0; sleep 0.1; done; return 1; }
wait_down() { for _ in $(seq 1 50); do port_open "$1" || return 0; sleep 0.1; done; return 1; }
trap stop_mock EXIT

# (a) endpoint up, returns 200 -> healthcheck must succeed
start_mock "$PORT" 200
wait_up "$PORT" || { echo "FAIL: mock (200) did not start"; exit 1; }
if sh -c "$cmd" >/dev/null 2>&1; then echo "  ok   200 -> healthy"; else echo "  FAIL 200 reported unhealthy"; fail=1; fi
stop_mock; wait_down "$PORT" || true

# (b) endpoint up, returns 500 -> healthcheck must fail (wget errors on 5xx)
start_mock "$PORT" 500
wait_up "$PORT" || { echo "FAIL: mock (500) did not start"; exit 1; }
if sh -c "$cmd" >/dev/null 2>&1; then echo "  FAIL 500 -> reported healthy"; fail=1; else echo "  ok   500 -> unhealthy"; fi
stop_mock; wait_down "$PORT" || true

# (c) port dead -> healthcheck must fail (this is what the old `|| exit 0` broke)
if sh -c "$cmd" >/dev/null 2>&1; then echo "  FAIL down -> reported healthy (false green)"; fail=1; else echo "  ok   down -> unhealthy"; fi

[[ "$fail" -eq 0 ]] && echo "PASS: healthcheck regression" || echo "FAIL: healthcheck regression"
exit "$fail"
