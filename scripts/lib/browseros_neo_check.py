#!/usr/bin/env python3
"""Fail-closed allowlist check of the BrowserOS neo pilot's rendered Compose config.

Reads `docker compose config --format json` on stdin and refuses (exit 1, one
line per problem on stderr) anything outside the pilot's envelope. It is an
allowlist, not a denylist: every top-level key, service key, mount (target
AND source), network, entrypoint/command, user, capability and security_opt
must be exactly what docker/browseros-neo/compose.yaml renders to. Anything
else -- volumes_from, tmpfs, configs, secrets, sysctls, extra_hosts, devices,
an external/host/macvlan network, extra Chromium flags via `command` -- is
refused because it is not on the list.

Usage: browseros_neo_check.py --data <canonical abs path> --image <pinned ref>
           --compose-dir <canonical abs path> --project <name>
           --vnc-port N --mcp-port N  < config.json
Stdlib only.
"""
import argparse
import json
import os
import re
import sys

EXPECTED_SERVICES = {"neo", "mcp-gate"}
TOP_LEVEL_KEYS = {"name", "services", "networks", "x-neo-image"}

NEO_KEYS = {
    "image", "init", "hostname", "shm_size", "stop_grace_period", "ports", "volumes",
    "environment", "security_opt", "cap_drop", "cap_add", "pids_limit", "mem_limit",
    "cpus", "networks", "restart", "command", "entrypoint",
}
GATE_KEYS = {
    "image", "entrypoint", "command", "user", "read_only", "ports", "volumes", "security_opt",
    "cap_drop", "pids_limit", "mem_limit", "healthcheck", "depends_on", "networks", "restart",
}
GATE_ENTRYPOINT = ["/usr/bin/python3", "-I", "/gate/mcp-gate.py", "9010", "neo", "9010"]
NEO_CAPS = {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"}
NEO_ENV_KEYS = {
    "PUID", "PGID", "NEO_SCREEN", "BROWSERCLAW_HOSTED_MODE", "NEO_SANDBOX",
    "NEO_SOFTWARE_GRAPHICS", "NEO_DISABLE_BROWSEROS_EXTENSIONS", "NEO_ARGS",
}
NEO_REQUIRED_ENV = {
    "BROWSERCLAW_HOSTED_MODE": "1",
    "NEO_SANDBOX": "false",
    "NEO_DISABLE_BROWSEROS_EXTENSIONS": "true",
    "NEO_ARGS": "",
}
NONROOT_USER = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")
# Keys Compose adds to a bind mount on its own; anything else (propagation,
# selinux relabel, volume/tmpfs options, consistency) is refused.
MOUNT_KEYS = {"type", "source", "target", "read_only", "bind"}


def _port_tuples(svc):
    out = []
    for p in svc.get("ports") or []:
        pub = p.get("published")
        out.append((p.get("host_ip") or "", int(p.get("target")),
                    int(pub) if pub not in (None, "") else None, p.get("protocol") or "tcp"))
    return out


def _mounts(name, svc, expected, errs):
    """expected: {target: (source, read_only)} -- exact set, exact sources."""
    got = {}
    for v in svc.get("volumes") or []:
        extra = set(v) - MOUNT_KEYS
        if extra:
            errs.append(f"{name}: mount {v.get('target')} has options {sorted(extra)} outside the allowlist")
        bind = v.get("bind") or {}
        if set(bind) - {"create_host_path"}:
            errs.append(f"{name}: mount {v.get('target')} bind options {sorted(bind)} outside the allowlist")
        if v.get("type") != "bind":
            errs.append(f"{name}: mount {v.get('target')} must be a bind mount, got {v.get('type')!r}")
        if v.get("target") in got:
            errs.append(f"{name}: {v.get('target')} mounted twice")
        got[v.get("target")] = (str(v.get("source") or ""), bool(v.get("read_only")))
    if set(got) != set(expected):
        errs.append(f"{name}: mounts must be exactly {sorted(expected)}, got {sorted(got)}")
    for target, (src, ro) in expected.items():
        if target in got and got[target] != (src, ro):
            errs.append(f"{name}: {target} must be {'read-only ' if ro else ''}bind of {src}, got {got[target]}")


def check(cfg, data, image, compose_dir, project, vnc_port, mcp_port):
    errs = []
    if not os.path.isabs(data) or os.path.normpath(data) != data:
        errs.append(f"--data must be a canonical absolute path, got {data!r}")
    if "@sha256:" not in image or not re.fullmatch(r"[0-9a-f]{64}", image.split("@sha256:", 1)[1]):
        errs.append(f"expected image is not digest-pinned: {image}")
    if vnc_port == mcp_port:
        errs.append("viewer and MCP ports must differ")

    extra_top = set(cfg) - TOP_LEVEL_KEYS
    if extra_top:
        errs.append(f"top-level keys {sorted(extra_top)} are not allowed")
    if cfg.get("name") != project:
        errs.append(f"project name must be {project!r}, got {cfg.get('name')!r}")
    nets = cfg.get("networks") or {}
    want_net = {"neo": {"name": f"{project}_neo", "ipam": {}}}
    if nets != want_net:
        errs.append(f"networks must be exactly {want_net} (default bridge driver, not external), got {nets}")

    services = cfg.get("services") or {}
    if set(services) != EXPECTED_SERVICES:
        errs.append(f"services must be exactly {sorted(EXPECTED_SERVICES)}, got {sorted(services)}")

    for name, svc in services.items():
        if name not in EXPECTED_SERVICES:
            continue
        allowed = NEO_KEYS if name == "neo" else GATE_KEYS
        extra = set(svc) - allowed
        if extra:
            errs.append(f"{name}: keys {sorted(extra)} are not allowed")
        if svc.get("image") != image:
            errs.append(f"{name}: image {svc.get('image')!r} is not the pinned {image!r}")
        if svc.get("networks") != {"neo": None}:
            errs.append(f"{name}: networks must be exactly the pilot's 'neo' network, got {svc.get('networks')}")
        if [str(c).upper() for c in svc.get("cap_drop") or []] != ["ALL"]:
            errs.append(f"{name}: cap_drop must be exactly [ALL]")
        if [str(s).replace("=", ":") for s in svc.get("security_opt") or []] != ["no-new-privileges:true"]:
            errs.append(f"{name}: security_opt must be exactly [no-new-privileges:true], got {svc.get('security_opt')}")
        if svc.get("restart") != "no":
            errs.append(f"{name}: restart must be 'no'")

    neo = services.get("neo") or {}
    gate = services.get("mcp-gate") or {}

    # neo: the image's own entrypoint/command (it drops to the neo user itself).
    if neo.get("entrypoint") is not None or neo.get("command") is not None:
        errs.append("neo: entrypoint/command overrides are not allowed (they can pass Chromium flags)")
    caps = {str(c).upper().removeprefix("CAP_") for c in neo.get("cap_add") or []}
    if not caps <= NEO_CAPS:
        errs.append(f"neo: cap_add {sorted(caps - NEO_CAPS)} is outside the allowed set")
    if neo.get("init") not in (False, None):
        errs.append("neo: init must be false (the image runs tini)")
    if neo.get("hostname") != "neo":
        errs.append("neo: hostname must be 'neo'")
    if _port_tuples(neo) != [("127.0.0.1", 6080, vnc_port, "tcp")]:
        errs.append(f"neo: must publish only 127.0.0.1:{vnc_port}->6080/tcp, got {_port_tuples(neo)}")
    env = {k: ("" if v is None else str(v)) for k, v in (neo.get("environment") or {}).items()}
    if set(env) != NEO_ENV_KEYS:
        errs.append(f"neo: environment keys must be exactly {sorted(NEO_ENV_KEYS)}, got {sorted(env)}")
    for key, want in NEO_REQUIRED_ENV.items():
        if env.get(key, "").strip().lower() != want:
            errs.append(f"neo: {key} must be {want!r}, got {env.get(key)!r}")
    for key in ("PUID", "PGID"):
        if not re.fullmatch(r"[1-9][0-9]*", env.get(key, "")):
            errs.append(f"neo: {key} must be a non-root numeric id, got {env.get(key)!r}")
    _mounts("neo", neo, {"/data": (data, False),
                         "/etc/neo/neo.toml": (os.path.join(compose_dir, "neo.toml"), True)}, errs)

    # mcp-gate: exact forwarder, non-root, read-only.
    if gate.get("entrypoint") != GATE_ENTRYPOINT or gate.get("command") is not None:
        errs.append(f"mcp-gate: entrypoint must be exactly {GATE_ENTRYPOINT} with no command")
    if not NONROOT_USER.fullmatch(str(gate.get("user") or "")):
        errs.append(f"mcp-gate: user must be numeric non-root uid:gid, got {gate.get('user')!r}")
    if gate.get("read_only") is not True:
        errs.append("mcp-gate: read_only root filesystem is required")
    if gate.get("cap_add"):
        errs.append("mcp-gate: cap_add is not allowed")
    if _port_tuples(gate) != [("127.0.0.1", 9010, mcp_port, "tcp")]:
        errs.append(f"mcp-gate: must publish only 127.0.0.1:{mcp_port}->9010/tcp, got {_port_tuples(gate)}")
    if gate.get("healthcheck") != {"disable": True}:
        errs.append("mcp-gate: healthcheck must be disabled")
    dep = gate.get("depends_on") or {}
    if set(dep) != {"neo"} or (dep.get("neo") or {}).get("condition") != "service_healthy":
        errs.append("mcp-gate: depends_on must be exactly neo: service_healthy")
    _mounts("mcp-gate", gate, {"/gate/mcp-gate.py": (os.path.join(compose_dir, "mcp-gate.py"), True)}, errs)
    return errs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--compose-dir", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--vnc-port", type=int, required=True)
    ap.add_argument("--mcp-port", type=int, required=True)
    a = ap.parse_args(argv)
    try:
        cfg = json.load(sys.stdin)
    except ValueError as exc:
        print(f"refused: config is not JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(cfg, dict):
        print("refused: config is not a JSON object", file=sys.stderr)
        return 1
    try:
        errs = check(cfg, a.data, a.image, a.compose_dir, a.project, a.vnc_port, a.mcp_port)
    except (AttributeError, TypeError, ValueError) as exc:  # malformed shapes fail closed
        errs = [f"config has an unexpected shape: {exc!r}"]
    for e in errs:
        print(f"refused: {e}", file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
