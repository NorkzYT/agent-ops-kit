"""Minimal stdlib CDP client: python3 -I cdp.py <json-commands-file|-> ; runs inside Neo."""
import base64, json, os, socket, sys, time, urllib.request

def ws_connect(url):
    assert url.startswith("ws://")
    hostport, path = url[5:].split("/", 1)
    host, port = hostport.split(":")
    s = socket.create_connection((host, int(port)), timeout=30)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c: raise RuntimeError("ws handshake closed")
        buf += c
    head, rest = buf.split(b"\r\n\r\n", 1)
    if b" 101 " not in head.split(b"\r\n")[0]: raise RuntimeError(head.decode(errors="replace"))
    return WS(s, rest)

class WS:
    def __init__(self, s, rest): self.s, self.buf, self.n = s, rest, 0
    def _read(self, n):
        while len(self.buf) < n:
            c = self.s.recv(65536)
            if not c: raise RuntimeError("ws closed")
            self.buf += c
        d, self.buf = self.buf[:n], self.buf[n:]
        return d
    def send(self, obj):
        data = json.dumps(obj).encode(); mask = os.urandom(4)
        hdr = bytearray([0x81]); L = len(data)
        if L < 126: hdr.append(0x80 | L)
        elif L < 65536: hdr += bytes([0x80 | 126]) + L.to_bytes(2, "big")
        else: hdr += bytes([0x80 | 127]) + L.to_bytes(8, "big")
        self.s.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._read(2); L = b2 & 0x7F
            if L == 126: L = int.from_bytes(self._read(2), "big")
            elif L == 127: L = int.from_bytes(self._read(8), "big")
            payload = self._read(L); op = b1 & 0x0F
            if op == 8: raise RuntimeError("ws close")
            if op in (9, 10): continue
            msg += payload
            if b1 & 0x80: return json.loads(msg)
    def call(self, method, params=None, session=None, timeout=30):
        self.n += 1; mid = self.n
        m = {"id": mid, "method": method, "params": params or {}}
        if session: m["sessionId"] = session
        self.send(m); end = time.time() + timeout; self.events = getattr(self, "events", [])
        while time.time() < end:
            r = self.recv()
            if r.get("id") == mid:
                if "error" in r: raise RuntimeError(f"{method}: {r['error']}")
                return r.get("result", {})
            self.events.append(r)
        raise TimeoutError(method)

def browser(port=9110):
    v = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=10))
    return ws_connect(v["webSocketDebuggerUrl"]), v
