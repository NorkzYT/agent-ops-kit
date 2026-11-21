#!/usr/bin/env bash
# test_browseros_neo_viewer_origin.sh — opt-in: run the viewer's websockify
# (exactly the entrypoint arguments compose.yaml renders, minus the port
# numbers) on host loopback against a fake VNC server, and check which
# WebSocket Origins it accepts. Starts no containers and deletes nothing; the
# scratch folder is printed and kept.
#
# Needs the websockify package and noVNC files from the pinned image, copied
# out read-only, e.g.:
#   docker save <pinned image> -o img.tar   # then untar each layer and keep
#   usr/lib/python3/dist-packages/websockify and usr/share/novnc
#   BROWSEROS_NEO_IMAGE_ROOT=<folder holding usr/> bash scripts/test_browseros_neo_viewer_origin.sh
# Without BROWSEROS_NEO_IMAGE_ROOT it skips (exit 0).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IR="${BROWSEROS_NEO_IMAGE_ROOT:-}"
if [[ -z "$IR" ]]; then echo "  skip viewer Origin test: BROWSEROS_NEO_IMAGE_ROOT not set"; exit 0; fi
PKG="$IR/usr/lib/python3/dist-packages"
[[ -f "$PKG/websockify/auth_plugins.py" && -f "$IR/usr/share/novnc/vnc.html" ]] \
  || { echo "BROWSEROS_NEO_IMAGE_ROOT lacks websockify or novnc" >&2; exit 1; }
command -v docker >/dev/null && docker compose version >/dev/null 2>&1 \
  || { echo "  skip viewer Origin test: docker compose CLI not available"; exit 0; }

tmp="$(mktemp -d)"; echo "  scratch (kept): $tmp"
port="$(python3 -I -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
mkdir -p "$tmp/data"
env -u COMPOSE_FILE BROWSEROS_NEO_DATA="$tmp/data" BROWSEROS_NEO_UID=1000 BROWSEROS_NEO_GID=1000 \
  BROWSEROS_NEO_VNC_PORT="$port" \
  docker compose --env-file /dev/null -p bneo-origin --project-directory "$ROOT/docker/browseros-neo" \
  -f "$ROOT/docker/browseros-neo/compose.yaml" config --format json > "$tmp/cfg.json" \
  || { echo "  FAIL compose config"; exit 1; }

timeout 60 python3 -I - "$tmp/cfg.json" "$PKG" "$IR/usr/share/novnc" "$port" <<'EOF'
import base64, json, os, socket, subprocess, sys, threading, time
cfg, pkg, web, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
ep = json.load(open(cfg))["services"]["viewer"]["entrypoint"]
assert ep[:4] == ["/usr/bin/python3", "-I", "-m", "websockify"], ep
args = ep[4:]
# Swap only the in-container addresses: web root, listen port, VNC target.
fake_vnc = socket.create_server(("127.0.0.1", 0)); vnc_port = fake_vnc.getsockname()[1]
args = [web if a == "/usr/share/novnc" else a for a in args]
assert args[-2:] == ["6081", "localhost:5900"], args
args[-2:] = [str(port), f"127.0.0.1:{vnc_port}"]
print("  websockify args:", " ".join(args))
connects = []
def vnc():
    while True:
        c, _ = fake_vnc.accept(); connects.append(1)
        c.sendall(b"RFB 003.008\n"); time.sleep(0.2); c.close()
threading.Thread(target=vnc, daemon=True).start()
runner = ("import sys, runpy; sys.path.insert(0, sys.argv[1]); sys.argv = ['websockify'] + sys.argv[2:]; "
          "runpy.run_module('websockify', run_name='__main__')")
ws = subprocess.Popen([sys.executable, "-I", "-c", runner, pkg, *args],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/")
def upgrade(origin, host=f"127.0.0.1:{port}"):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    hdr = [f"GET /websockify HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
           f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", "Sec-WebSocket-Protocol: binary"]
    if origin is not None:
        hdr.append(f"Origin: {origin}")
    s.sendall(("\r\n".join(hdr) + "\r\n\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(4096)
        if not chunk: break
        data += chunk
    status = data.split(b"\r\n", 1)[0].decode(errors="replace")
    body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
    if " 101 " in status + " " and b"RFB" not in body:
        s.settimeout(3)
        try: body += s.recv(4096)
        except OSError: pass
    s.close()
    return status, body
fails = 0
def check(label, cond):
    global fails
    print(("  ok   " if cond else "  FAIL ") + label); fails += 0 if cond else 1
try:
    for _ in range(100):
        try: socket.create_connection(("127.0.0.1", port), timeout=1).close(); break
        except OSError: time.sleep(0.1)
    allowed = f"http://127.0.0.1:{port}"
    n0 = len(connects)
    for label, origin in [("foreign site", "https://evil.example"),
                          ("DNS-rebinding name on the same port", f"http://rebind.example:{port}"),
                          ("localhost spelling", f"http://localhost:{port}"),
                          ("other local port", f"http://127.0.0.1:{port + 1}"),
                          ("https scheme", f"https://127.0.0.1:{port}"),
                          ("null origin (sandboxed iframe/file:)", "null"),
                          ("no Origin header", None)]:
        st, _ = upgrade(origin)
        check(f"refuses WebSocket from {label}: {st}", " 403 " in st + " ")
    check("refused upgrades never reached the VNC server", len(connects) == n0)
    st, body = upgrade(allowed)
    check(f"accepts WebSocket from {allowed}: {st}", " 101 " in st + " ")
    check("accepted upgrade relays the VNC server's bytes", b"RFB" in body)
    st, _ = upgrade(allowed, host="rebind.example")
    print(f"  note: Host header is not checked (allowed Origin + Host rebind.example -> {st})")
finally:
    ws.terminate(); ws.wait(timeout=5)
sys.exit(1 if fails else 0)
EOF
rc=$?
echo "browseros-neo viewer Origin: $([[ $rc == 0 ]] && echo passed || echo FAILED)"
exit "$rc"
