#!/usr/bin/env python3
"""Plain TCP forwarder: the only path from the host to Neo's MCP port.

Runs as its own container on the pilot's private network. `browseros-neo.sh
pause` stops this container, so the host's loopback MCP port refuses new
connections and open ones are cut, while Neo and its noVNC viewer keep
running for the human. It is a cut-off switch, not a filter: while it runs it
forwards every byte, including Neo's /v1 and settings API on the same port.

Stdlib only. Usage: mcp-gate.py <listen_port> <upstream_host> <upstream_port>
"""
import socket
import sys
import threading

MAX_CONNECTIONS = 64
BUF = 65536


def pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(BUF)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client: socket.socket, upstream: tuple, slots: threading.BoundedSemaphore) -> None:
    try:
        try:
            server = socket.create_connection(upstream, timeout=10)
        except OSError as exc:
            print(f"mcp-gate: upstream {upstream[0]}:{upstream[1]} unreachable: {exc}", flush=True)
            return
        server.settimeout(None)
        client.settimeout(None)
        t = threading.Thread(target=pipe, args=(server, client), daemon=True)
        t.start()
        pipe(client, server)
        t.join()
        server.close()
    finally:
        client.close()
        slots.release()


def main(argv: list) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    listen_port, host, port = int(argv[1]), argv[2], int(argv[3])
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    srv = socket.create_server(("0.0.0.0", listen_port), reuse_port=False)
    print(f"mcp-gate: forwarding :{listen_port} -> {host}:{port}", flush=True)
    while True:
        client, _ = srv.accept()
        if not slots.acquire(blocking=False):
            client.close()
            continue
        threading.Thread(target=handle, args=(client, (host, port), slots), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
