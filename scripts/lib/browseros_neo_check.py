#!/usr/bin/env python3
"""Fail-closed check of the BrowserOS neo pilot's rendered Compose config.

Reads `docker compose config --format json` on stdin and refuses (exit 1, one
line per problem on stderr) anything outside the pilot's security envelope:
non-loopback or extra published ports, Neo's unauthenticated MCP port
published directly, an unpinned image, privileged or host-namespace modes,
the Chromium sandbox flag without its required seccomp relaxation, hosted
mode off, remote MCP on, bundled recording extensions on, docker.sock or
extra mounts, and a data mount other than the expected folder.

Usage: browseros_neo_check.py --data <abs path> --image <pinned ref>
                              --vnc-port N --mcp-port N  < config.json
Stdlib only.
"""
import argparse
import json
import sys

EXPECTED_SERVICES = {"neo", "mcp-gate"}
NEO_REQUIRED_ENV = {
    "BROWSERCLAW_HOSTED_MODE": "1",
    "NEO_SANDBOX": "false",
    "NEO_DISABLE_BROWSEROS_EXTENSIONS": "true",
}
# Env names the pinned image documents (or the SDK image documents) as
# widening exposure; any value is refused.
NEO_FORBIDDEN_ENV = {"NEO_MCP_REMOTE", "CLAW_HOST_TCP"}
HOST_MODES = ("network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup")


def _ports(svc):
    return svc.get("ports") or []


def _published(port):
    pub = port.get("published")
    return int(pub) if pub not in (None, "") else None


def check(cfg, data, image, vnc_port, mcp_port):
    errs = []
    services = cfg.get("services") or {}
    names = set(services)
    if names != EXPECTED_SERVICES:
        errs.append(f"services must be exactly {sorted(EXPECTED_SERVICES)}, got {sorted(names)}")

    if "@sha256:" not in image or len(image.split("@sha256:", 1)[1]) != 64:
        errs.append(f"expected image is not digest-pinned: {image}")

    for name, svc in services.items():
        if svc.get("image") != image:
            errs.append(f"{name}: image {svc.get('image')!r} is not the pinned {image!r}")
        if svc.get("build"):
            errs.append(f"{name}: build is not allowed; use the pinned image")
        if svc.get("privileged"):
            errs.append(f"{name}: privileged is not allowed")
        for mode in HOST_MODES:
            val = str(svc.get(mode) or "")
            if val == "host" or val.startswith("container:") or val.startswith("service:"):
                errs.append(f"{name}: {mode}={val} is not allowed")
        if svc.get("devices"):
            errs.append(f"{name}: devices are not allowed")
        caps = [str(c).upper() for c in (svc.get("cap_drop") or [])]
        if "ALL" not in caps:
            errs.append(f"{name}: cap_drop must include ALL")
        allowed_add = {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"} if name == "neo" else set()
        extra = {str(c).upper().removeprefix("CAP_") for c in (svc.get("cap_add") or [])} - allowed_add
        if extra:
            errs.append(f"{name}: cap_add {sorted(extra)} is outside the allowed set")
        sec = [str(s).replace("=", ":") for s in (svc.get("security_opt") or [])]
        if "no-new-privileges:true" not in sec:
            errs.append(f"{name}: security_opt no-new-privileges:true is required")
        for s in sec:
            if "unconfined" in s:
                errs.append(f"{name}: security_opt {s} is not allowed")
        for p in _ports(svc):
            host_ip = p.get("host_ip") or ""
            if host_ip != "127.0.0.1":
                errs.append(f"{name}: port {p.get('target')} published on {host_ip or 'all interfaces'}, only 127.0.0.1 is allowed")
            if (p.get("protocol") or "tcp") != "tcp":
                errs.append(f"{name}: non-tcp port {p}")
        for v in svc.get("volumes") or []:
            src = str(v.get("source") or "")
            if "docker.sock" in src or src in ("/", "/var/run", "/run"):
                errs.append(f"{name}: mount of {src} is not allowed")

    neo = services.get("neo") or {}
    gate = services.get("mcp-gate") or {}

    neo_ports = [(int(p.get("target")), _published(p)) for p in _ports(neo)]
    if neo_ports != [(6080, vnc_port)]:
        errs.append(f"neo: must publish only 6080 as {vnc_port}, got {neo_ports}")
    gate_ports = [(int(p.get("target")), _published(p)) for p in _ports(gate)]
    if gate_ports != [(9010, mcp_port)]:
        errs.append(f"mcp-gate: must publish only 9010 as {mcp_port}, got {gate_ports}")
    if vnc_port == mcp_port:
        errs.append("viewer and MCP ports must differ")

    env = neo.get("environment") or {}
    for key, want in NEO_REQUIRED_ENV.items():
        if str(env.get(key, "")).strip().lower() != want:
            errs.append(f"neo: {key} must be {want!r}, got {env.get(key)!r}")
    for key in NEO_FORBIDDEN_ENV:
        if key in env:
            errs.append(f"neo: {key} must not be set")
    if str(env.get("NEO_ARGS", "")).strip():
        errs.append("neo: NEO_ARGS must be empty (it can re-enable remote debugging or features)")

    data_mounts = [v for v in neo.get("volumes") or [] if v.get("target") == "/data"]
    if len(data_mounts) != 1 or data_mounts[0].get("type") != "bind" or data_mounts[0].get("source") != data:
        errs.append(f"neo: /data must be one bind mount of {data}")
    other = [v for v in neo.get("volumes") or [] if v.get("target") not in ("/data", "/etc/neo/neo.toml")]
    if other:
        errs.append(f"neo: unexpected mounts {[v.get('target') for v in other]}")
    toml = [v for v in neo.get("volumes") or [] if v.get("target") == "/etc/neo/neo.toml"]
    if len(toml) != 1 or not toml[0].get("read_only"):
        errs.append("neo: /etc/neo/neo.toml must be mounted read-only")

    gate_mounts = gate.get("volumes") or []
    if [(v.get("target"), bool(v.get("read_only"))) for v in gate_mounts] != [("/gate/mcp-gate.py", True)]:
        errs.append("mcp-gate: only /gate/mcp-gate.py read-only may be mounted")
    if not gate.get("read_only"):
        errs.append("mcp-gate: read_only root filesystem is required")
    if str(gate.get("user") or "") in ("", "0", "root") or str(gate.get("user")).startswith("0:"):
        errs.append("mcp-gate: must run as a non-root user")
    return errs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--vnc-port", type=int, required=True)
    ap.add_argument("--mcp-port", type=int, required=True)
    a = ap.parse_args(argv)
    try:
        cfg = json.load(sys.stdin)
    except ValueError as exc:
        print(f"config is not JSON: {exc}", file=sys.stderr)
        return 1
    errs = check(cfg, a.data, a.image, a.vnc_port, a.mcp_port)
    for e in errs:
        print(f"refused: {e}", file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
