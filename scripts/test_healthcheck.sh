#!/usr/bin/env bash
# test_healthcheck.sh — prove the cliproxyapi healthcheck reports real liveness.
# It extracts the exact healthcheck argv from docker-compose.yml (CMD bash/sh -c
# or CMD-SHELL, Compose `$$` unescaped, whatever path it probes) and runs it
# against a local mock, so the test tracks whatever the compose file
# actually checks:
#   (a) endpoint returns 200   -> HEALTHY
#   (b) endpoint returns a 5xx -> UNHEALTHY  (status line is not 200)
#   (c) dead port              -> UNHEALTHY  (regression for the old `|| exit 0`)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PORT=18317
fail=0

# Extract the cliproxyapi healthcheck argv from docker-compose.yml with PyYAML
# (already used by the CI `stack` job; preinstalled on ubuntu-latest). No live
# `docker compose config`: that would need a real .env. Supported forms only —
# anything else fails closed:
#   test: ["CMD", "bash"|"sh", "-c", "<script>"]
#   test: ["CMD-SHELL", "<script>"]  or  test: "<script>"   (run via sh -c)
# Compose interpolation: `$$` is a literal `$`; any other `$` would be expanded
# from the environment by Compose, so it is rejected rather than guessed.
# Prints one argv element per NUL-terminated record; exit 2 on unsupported input.
extract_healthcheck() { # $1 = compose file
  python3 - "$1" <<'PY'
import re, sys, yaml
def die(msg):
    print(f"unsupported healthcheck: {msg}", file=sys.stderr); sys.exit(2)
try:
    doc = yaml.safe_load(open(sys.argv[1]))
    test = doc["services"]["cliproxyapi"]["healthcheck"]["test"]
except Exception as e:
    die(f"cannot read services.cliproxyapi.healthcheck.test ({type(e).__name__})")
if isinstance(test, str):
    test = ["CMD-SHELL", test]
if not isinstance(test, list) or not all(isinstance(x, str) for x in test):
    die("test must be a string or a list of strings")
if len(test) == 2 and test[0] == "CMD-SHELL":
    argv = ["sh", "-c", test[1]]
elif len(test) == 4 and test[0] == "CMD" and test[1] in ("bash", "sh") and test[2] == "-c":
    argv = test[1:]
else:
    die(f"form {test[:3]!r} is not CMD-SHELL or CMD bash/sh -c")
script = argv[2]
if not script.strip():
    die("empty command")
if re.search(r"\$(?!\$)", script.replace("$$", "")):
    die("unescaped $ (Compose would interpolate it)")
script = script.replace("$$", "$")
if script.count("8317") != 1:
    die("command must probe port 8317 exactly once")
argv[2] = script
sys.stdout.write("".join(a + "\0" for a in argv))
PY
}

# Fail-closed self-checks: unsupported or ambiguous forms must not extract.
FIXDIR="$(mktemp -d)"; trap 'rm -rf "$FIXDIR"' EXIT
fixture() { printf 'services:\n  cliproxyapi:\n    healthcheck:\n      test: %s\n' "$1" >"$FIXDIR/c.yml"; }
# shellcheck disable=SC2016  # literal $PORT is the point of that fixture
for bad in '["CMD", "wget", "-qO-", "http://127.0.0.1:8317/"]' \
           '["CMD-SHELL", "exit 0"]' \
           '["CMD-SHELL", "curl -f http://127.0.0.1:$PORT/healthz"]' \
           '["NONE"]' '[]' '42' '["CMD", "bash", "-c"]'; do
  fixture "$bad"
  if extract_healthcheck "$FIXDIR/c.yml" >/dev/null 2>&1; then
    echo "  FAIL extractor accepted unsupported test: $bad"; fail=1
  else echo "  ok   rejects $bad"; fi
done
fixture '"wget -q -O- http://127.0.0.1:8317/healthz"'
if [[ "$(extract_healthcheck "$FIXDIR/c.yml" | tr '\0' '|')" == "sh|-c|wget -q -O- http://127.0.0.1:8317/healthz|" ]]
then echo "  ok   string form -> sh -c"; else echo "  FAIL string form not extracted as sh -c"; fail=1; fi

mapfile -d '' argv < <(extract_healthcheck docker-compose.yml)
[[ ${#argv[@]} -eq 3 ]] || { echo "FAIL: could not extract healthcheck command"; exit 1; }
argv[2]="${argv[2]//8317/$PORT}"          # retarget at the throwaway test port
echo "healthcheck argv: ${argv[0]} ${argv[1]} ${argv[2]}"

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
trap 'stop_mock; rm -rf "$FIXDIR"' EXIT

# (a) endpoint up, returns 200 -> healthcheck must succeed
start_mock "$PORT" 200
wait_up "$PORT" || { echo "FAIL: mock (200) did not start"; exit 1; }
if "${argv[@]}" >/dev/null 2>&1; then echo "  ok   200 -> healthy"; else echo "  FAIL 200 reported unhealthy"; fail=1; fi
stop_mock; wait_down "$PORT" || true

# (b) endpoint up, returns 500 -> healthcheck must fail (status line is not 200)
start_mock "$PORT" 500
wait_up "$PORT" || { echo "FAIL: mock (500) did not start"; exit 1; }
if "${argv[@]}" >/dev/null 2>&1; then echo "  FAIL 500 -> reported healthy"; fail=1; else echo "  ok   500 -> unhealthy"; fi
stop_mock; wait_down "$PORT" || true

# (c) port dead -> healthcheck must fail (this is what the old `|| exit 0` broke)
if "${argv[@]}" >/dev/null 2>&1; then echo "  FAIL down -> reported healthy (false green)"; fail=1; else echo "  ok   down -> unhealthy"; fi

[[ "$fail" -eq 0 ]] && echo "PASS: healthcheck regression" || echo "FAIL: healthcheck regression"
exit "$fail"
