#!/usr/bin/env python3
"""specialist-activate — install, enable and check the specialist-router pilot.

  status   Read-only. Repo plugin vs. installed plugin (sha256), the
           ``specialist_*`` settings read back through ``hermes config get``,
           gateway state, whether a restart is pending, per-profile knowledge
           readiness. Changes nothing.
  install  Copy ``hermes/plugins/model-router`` into ``$HERMES_HOME/plugins``,
           set the ``specialist_*`` keys with ``hermes config set`` and read
           every key and file back. Never restarts the gateway.
  enable   ``install``, then restart the gateway and check that a fresh
           ``model_router registered (... specialists=on)`` line appears. Asks
           first (``--yes`` skips the question). Refuses to restart from inside
           the gateway or an agent turn it serves; prints the command to run in
           your own terminal instead.
  disable  Set ``specialist_routing: false`` and read it back. No restart.

Settings: ``specialist_routing`` is set true. ``--max``, ``--require-ready``,
``--classifier`` and ``--profiles`` are written when given; a key you do not
pass keeps its current value, or gets the default (4, false, none, all) when
it is missing. ``hermes config set`` rewrites ``config.yaml`` without its
comments, so a ``config.yaml.bak.<timestamp>`` copy is made before the first
write. It stores ``none`` as ``null``; the plugin reads that as ``none``.

Never reads ``.env`` or secrets. Writes only through ``hermes config set``,
the config backup, ``$HERMES_HOME/plugins/model-router`` and its backup under
``$HERMES_HOME/backups/agent-ops-kit/``.

Exit codes: 0 done, 1 preflight/write/read-back failure, 2 usage,
3 installed and verified but the gateway was not restarted (refused, no TTY
without --yes, or the gateway is not running), 4 restarted but
``specialists=on`` was not seen within --wait seconds.

Environment: HERMES_HOME (default ~/.hermes), GATEWAY_UNIT (default
hermes-gateway.service). SPECIALIST_PROC_ROOT replaces /proc in tests only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
PLUGIN = "model-router"
PLUGIN_SRC = REPO / "hermes" / "plugins" / PLUGIN
PREFIX = f"plugins.entries.{PLUGIN}.settings"
DEFAULTS: Dict[str, Any] = {
    "specialist_max_profiles": 4,
    "specialist_require_ready": False,
    "specialist_classifier": "none",
    "specialist_selection_mode": "ai",
    "specialist_profiles": [],
}
MODES = ("ai", "hybrid", "keywords")
KEYS = ("specialist_routing",) + tuple(DEFAULTS)
OLLAMA_KEYS = ("ollama_base_url", "ollama_model")
# Set in the gateway process and the agent/tool processes it spawns.
GATEWAY_MARKERS = ("_HERMES_GATEWAY", "HERMES_GATEWAY", "HERMES_GATEWAY_MODE",
                   "HERMES_GATEWAY_SESSION", "HERMES_CRON_SESSION")
# Services that run agent turns for the gateway: restarting from there kills
# the turn that asked for the restart.
AGENT_UNITS = ("claude-max-proxy.service",)
REGISTERED = "model_router registered"


def out(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"warn: {msg}", file=sys.stderr, flush=True)


class Fail(Exception):
    pass


def specialists_module():
    """The plugin's ``specialists`` module, loaded the way specialist-route.py does."""
    path = REPO / "scripts" / "specialist-route.py"
    spec = importlib.util.spec_from_file_location("specialist_route_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod._load("specialists")


# --------------------------------------------------------------------------- #
# Hermes CLI (the only writer of config.yaml)
# --------------------------------------------------------------------------- #
class Hermes:
    def __init__(self, home: Path):
        self.home = home
        self.bin = shutil.which("hermes")
        self.env = dict(os.environ, HERMES_HOME=str(home))

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        if not self.bin:
            raise Fail("hermes not found on PATH (install it: make hermes-install)")
        return subprocess.run([self.bin, *args], env=self.env, capture_output=True,
                              text=True, timeout=120)

    def get(self, key: str) -> Tuple[bool, Any]:
        r = self._run("config", "get", "--json", key)
        if r.returncode != 0:
            if "not set" in (r.stdout + r.stderr):
                return False, None
            raise Fail(f"hermes config get {key} failed: {(r.stderr or r.stdout).strip()}")
        try:
            return True, json.loads(r.stdout)
        except ValueError:
            raise Fail(f"hermes config get --json {key}: unreadable output")

    def set(self, key: str, literal: str) -> None:
        r = self._run("config", "set", key, literal)
        if r.returncode != 0:
            raise Fail(f"hermes config set {key} failed: {(r.stderr or r.stdout).strip()}")

    def restart_gateway(self) -> int:
        if not self.bin:
            raise Fail("hermes not found on PATH")
        return subprocess.run([self.bin, "gateway", "restart"], env=self.env).returncode


def literal(value: Any) -> str:
    """The ``hermes config set`` literal for a value (it coerces true/4/[a, b])."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(value) + "]"
    return str(value)


def same(key: str, want: Any, got: Any) -> bool:
    if key == "specialist_classifier" and want == "none":
        return got in (None, "none")   # hermes config set stores none as null
    if key == "specialist_profiles" and want == []:
        return got in (None, [])
    if isinstance(want, bool) or isinstance(got, bool):
        return type(got) is bool and got == want
    return got == want


def show(value: Any) -> str:
    return "(unset)" if value is _UNSET else json.dumps(value)


_UNSET = object()


# --------------------------------------------------------------------------- #
# Plugin files
# --------------------------------------------------------------------------- #
def plugin_files(root: Path) -> List[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plugin_diff(dest: Path) -> List[str]:
    """Repo plugin files that are missing or different in ``dest``."""
    return [rel for rel in plugin_files(PLUGIN_SRC)
            if not (dest / rel).is_file() or sha(dest / rel) != sha(PLUGIN_SRC / rel)]


def install_plugin(home: Path, changed: List[str], stamp: str) -> Optional[Path]:
    dest = home / "plugins" / PLUGIN
    backup = None
    if dest.is_dir():
        backup = home / "backups" / "agent-ops-kit" / f"{PLUGIN}.{stamp}"
        shutil.copytree(dest, backup, ignore=shutil.ignore_patterns("__pycache__"))
    for rel in changed:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # copyfile, not copy2: a fresh mtime is what marks the restart pending.
        shutil.copyfile(PLUGIN_SRC / rel, target)
        shutil.copymode(PLUGIN_SRC / rel, target)
    shutil.rmtree(dest / "__pycache__", ignore_errors=True)
    return backup


# --------------------------------------------------------------------------- #
# Gateway and process context
# --------------------------------------------------------------------------- #
def proc_root() -> Path:
    return Path(os.environ.get("SPECIALIST_PROC_ROOT") or "/proc")


def _stat_fields(proc: Path, pid: str) -> List[str]:
    raw = (proc / pid / "stat").read_text()
    return raw.rsplit(")", 1)[1].split()   # [state, ppid, ...]; starttime is [19]


def process_start(proc: Path, pid: int) -> Optional[float]:
    """Wall-clock start time of ``pid`` from /proc (None when unknown)."""
    try:
        ticks = int(_stat_fields(proc, str(pid))[19])
        btime = next(int(line.split()[1]) for line in (proc / "stat").read_text().splitlines()
                     if line.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def ancestors(proc: Path) -> List[int]:
    chain, pid = [], "self"
    for _ in range(64):
        try:
            ppid = int(_stat_fields(proc, pid)[1])
        except (OSError, ValueError, IndexError):
            break
        if ppid <= 1:
            break
        chain.append(ppid)
        pid = str(ppid)
    return chain


def gateway_unit() -> str:
    return os.environ.get("GATEWAY_UNIT") or "hermes-gateway.service"


def gateway_state(unit: str) -> Dict[str, Any]:
    """``{"state": active|inactive|...|unknown, "pid": int, "since": float|None}``."""
    systemctl = shutil.which("systemctl")
    info: Dict[str, Any] = {"state": "unknown", "pid": 0, "since": None}
    if not systemctl:
        return info
    try:
        r = subprocess.run([systemctl, "--user", "show", "-p", "ActiveState", "-p", "MainPID", unit],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return info
    if r.returncode != 0:
        return info
    vals = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    info["state"] = vals.get("ActiveState") or "unknown"
    try:
        info["pid"] = int(vals.get("MainPID") or 0)
    except ValueError:
        pass
    if info["pid"]:
        info["since"] = process_start(proc_root(), info["pid"])
    return info


def restart_blockers(gateway_pid: int, unit: str) -> List[str]:
    """Why this process must not restart the gateway (empty = it may)."""
    reasons = []
    for key in GATEWAY_MARKERS:
        if os.environ.get(key, "").strip().lower() not in ("", "0", "false", "no", "off"):
            reasons.append(f"{key} is set: this runs inside a Hermes gateway session")
    proc = proc_root()
    if gateway_pid and gateway_pid in ancestors(proc):
        reasons.append(f"the gateway (pid {gateway_pid}) is an ancestor of this process")
    try:
        cgroup = (proc / "self" / "cgroup").read_text()
    except OSError:
        cgroup = ""
    for u in (unit,) + AGENT_UNITS:
        if f"/{u}" in cgroup:
            reasons.append(f"this process runs in {u}, which serves Hermes turns")
    return reasons


def restart_pending(home: Path, gw: Dict[str, Any]) -> Optional[bool]:
    """True when config.yaml or the installed plugin changed after the gateway started."""
    if gw["state"] != "active" or not gw["since"]:
        return None
    paths = [home / "config.yaml"] + [home / "plugins" / PLUGIN / rel
                                      for rel in plugin_files(PLUGIN_SRC)]
    newest = max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)
    return newest > gw["since"]


def registrations(log: Path, offset: int) -> List[str]:
    try:
        with log.open("rb") as fh:
            if fh.seek(0, 2) < offset:   # rotated: read the new file from the start
                offset = 0
            fh.seek(offset)
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line for line in data.splitlines() if REGISTERED in line]


# --------------------------------------------------------------------------- #
# Readiness preflight (stat-only, never opens a book)
# --------------------------------------------------------------------------- #
def profiles_root(home: Path) -> Path:
    return home.parent.parent / "profiles" if home.parent.name == "profiles" else home / "profiles"


def readiness(home: Path, allow: List[str]) -> List[Tuple[str, Any]]:
    sp = specialists_module()
    root = profiles_root(home)
    names = allow or [*(s.name for s in sp.REGISTRY), *sp.dynamic_specialists(root)]
    return [(n, sp.knowledge_readiness(n, root)) for n in names]


def print_readiness(rows: List[Tuple[str, Any]]) -> None:
    counts: Dict[str, int] = {}
    for _, r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    out("  readiness       : " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for status in sorted(counts):
        names = [n + (" (stale)" if r.stale else "") for n, r in rows if r.status == status]
        out(f"    {status:<14}{', '.join(names)}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def read_settings(h: Hermes, keys: Tuple[str, ...] = KEYS) -> Dict[str, Any]:
    """The named plugin settings that are set, one key at a time: other
    settings (private_terms, private_paths) are never read."""
    got = {}
    for key in keys:
        found, value = h.get(f"{PREFIX}.{key}")
        if found:
            got[key] = value
    return got


def preflight(h: Hermes) -> None:
    if not h.bin:
        raise Fail("hermes not found on PATH (install it: make hermes-install)")
    if not (h.home / "config.yaml").is_file():
        raise Fail(f"{h.home}/config.yaml not found (run: make hermes-install)")
    found, enabled = h.get("plugins.enabled")
    if not found or not isinstance(enabled, list) or PLUGIN not in enabled:
        raise Fail(f"{PLUGIN} is not in plugins.enabled of {h.home}/config.yaml; "
                   "this tool will not edit that list. Add it (hermes config edit), then re-run")


def status(args) -> int:
    home = hermes_home()
    h = Hermes(home)
    unit = gateway_unit()
    out(f"specialist routing status  (HERMES_HOME={home})")
    out(f"  hermes          : {h.bin or 'not found on PATH'}")
    cfg = home / "config.yaml"
    out(f"  config          : {cfg}{'' if cfg.is_file() else '  (missing: make hermes-install)'}")
    dest = home / "plugins" / PLUGIN
    diff = plugin_diff(dest)
    total = len(plugin_files(PLUGIN_SRC))
    if not dest.is_dir():
        out(f"  plugin files    : not installed ({dest})")
    elif diff:
        out(f"  plugin files    : {len(diff)} of {total} differ from the repo: {', '.join(diff)}")
    else:
        out(f"  plugin files    : in sync with the repo ({total} files)")
    settings: Dict[str, Any] = {}
    if h.bin and cfg.is_file():
        found, enabled = h.get("plugins.enabled")
        out(f"  plugins.enabled : {'has ' + PLUGIN if found and isinstance(enabled, list) and PLUGIN in enabled else 'MISSING ' + PLUGIN}")
        settings = read_settings(h)
        for key in KEYS:
            note = "" if key in settings else f"  -> plugin default {json.dumps(DEFAULTS.get(key, False))}"
            out(f"    {key:<26}{show(settings.get(key, _UNSET))}{note}")
    gw = gateway_state(unit)
    since = dt.datetime.fromtimestamp(gw["since"]).strftime("%Y-%m-%d %H:%M:%S") if gw["since"] else "?"
    out(f"  gateway         : {unit} {gw['state']}" + (f", pid {gw['pid']} since {since}" if gw["pid"] else ""))
    pending = restart_pending(home, gw)
    out("  restart pending : " + {True: "yes (config or plugin changed after the gateway started)",
                                   False: "no", None: "unknown"}[pending])
    allow = settings.get("specialist_profiles") or []
    print_readiness(readiness(home, allow if isinstance(allow, list) else []))
    on = settings.get("specialist_routing") is True
    if diff or not on:
        out("  next            : make specialist-install DRY_RUN=1, then make specialist-install")
    elif pending:
        out("  next            : make specialist-enable   (or make hermes-restart, from your own terminal)")
    return 0


def desired(args, current: Dict[str, Any], enable: bool) -> Dict[str, Any]:
    sp = specialists_module()
    want: Dict[str, Any] = {"specialist_routing": enable}
    if not enable:
        return want
    if args.max is not None:
        if not 0 <= args.max <= sp.HARD_MAX_PROFILES:
            raise Fail(f"--max must be 0-{sp.HARD_MAX_PROFILES}")
        want["specialist_max_profiles"] = args.max
    if args.require_ready is not None:
        want["specialist_require_ready"] = args.require_ready
    if args.classifier is not None:
        if args.classifier not in ("none", "local", "cliproxy"):
            raise Fail("--classifier must be none, local or cliproxy")
        want["specialist_classifier"] = args.classifier
    if args.mode is not None:
        if args.mode not in MODES:
            raise Fail("--mode must be ai, hybrid or keywords")
        want["specialist_selection_mode"] = args.mode
    if args.profiles is not None:
        names = [] if args.profiles.strip() in ("", "all") else args.profiles.split(",")
        dynamic = sp.dynamic_specialists(profiles_root(hermes_home()))
        known, unknown = sp.split_allowed(names, extra_known=dynamic)
        if unknown:
            raise Fail(f"unknown profiles: {', '.join(unknown)} (known: {', '.join([*sp.BY_NAME, *dynamic])})")
        want["specialist_profiles"] = list(known)
    for key, default in DEFAULTS.items():
        # null reads as the default (hermes config set stores none as null).
        want.setdefault(key, default if current.get(key) is None else current[key])
    return want


def apply(args, enable: bool, label: str) -> int:
    home = hermes_home()
    h = Hermes(home)
    preflight(h)
    current = read_settings(h)
    want = desired(args, current, enable)
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    dry = " (dry run: nothing written)" if args.dry_run else ""

    out(f"specialist routing {label}  (HERMES_HOME={home}){dry}")
    if enable:
        allow = want["specialist_profiles"]
        rows = readiness(home, allow)
        print_readiness(rows)
        ready = [n for n, r in rows if r.status == "ready"]
        absent = [n for n, r in rows if r.status == "not-installed"]
        if absent:
            warn(f"{len(absent)} not installed, always dropped (make hermes-profile NAME=<profile>)")
        if want["specialist_require_ready"] and not ready:
            raise Fail("require_ready is on but no allowed profile has a knowledge skill: nothing "
                       "would route. Run make knowledge-learn NAME=<profile> first, or REQUIRE_READY=0")
        if not want["specialist_require_ready"] and len(ready) < len(rows):
            warn("profiles without a knowledge skill still route (status shown in the note); "
                 "REQUIRE_READY=1 drops them")
        if want["specialist_classifier"] == "local" and len(read_settings(h, OLLAMA_KEYS)) < 2:
            warn("classifier local needs ollama_base_url and ollama_model in the plugin settings; "
                 "until then it never suggests anything")
        if want.get("specialist_selection_mode", "ai") == "ai" and want["specialist_classifier"] == "none":
            warn("selection mode ai with classifier none selects no specialist (fail-closed): set "
                 "CLASSIFIER=cliproxy (or local), or MODE=hybrid/keywords for deterministic routing")
        if want["specialist_classifier"] == "cliproxy":
            warn("classifier cliproxy sends public turns that reach the classifier (typed task plus "
                 "verified attachments) to CLIProxyAPI; it needs CLIPROXY_API_KEY in the gateway "
                 "environment (~/.hermes/.env) and never runs for #privacy turns")

    dest = home / "plugins" / PLUGIN
    changed = plugin_diff(dest) if enable else []
    out(f"  plugin files    : {len(changed)} to copy into {dest}" if changed else
        f"  plugin files    : {'in sync' if enable else 'not touched'}")
    sets = [(k, v) for k, v in want.items() if not (k in current and same(k, v, current[k]))]
    for key, value in want.items():
        mark = "set" if (key, value) in sets else "ok "
        out(f"  {mark} {PREFIX}.{key}: {show(current.get(key, _UNSET))} -> {json.dumps(value)}")
    if args.dry_run:
        return 0

    if changed:
        backup = install_plugin(home, changed, stamp)
        out(f"  copied {len(changed)} plugin files" + (f" (previous copy: {backup})" if backup else ""))
    if sets:
        cfg = home / "config.yaml"
        bak = cfg.with_name(f"config.yaml.bak.{stamp}")
        shutil.copyfile(cfg, bak)
        os.chmod(bak, 0o600)
        out(f"  backed up config.yaml -> {bak}")
        for key, value in sets:
            h.set(f"{PREFIX}.{key}", literal(value))

    # Read back exactly what Hermes will load.
    after = read_settings(h, tuple(want))
    bad = [f"{k}: want {json.dumps(v)}, got {show(after.get(k, _UNSET))}"
           for k, v in want.items() if not same(k, v, after.get(k))]
    left = plugin_diff(dest) if enable else []
    if bad or left:
        raise Fail("read-back failed: " + "; ".join(bad + [f"plugin file differs: {f}" for f in left]))
    out(f"  verified        : {len(want)} settings read back"
        + (f", {len(plugin_files(PLUGIN_SRC))} plugin files match the repo" if enable else ""))
    return 0


def install(args) -> int:
    rc = apply(args, enable=True, label="install")
    if rc == 0 and not args.dry_run:
        out("  gateway         : not restarted. It keeps the old settings until it restarts:")
        out("                    make specialist-enable   (or make hermes-restart)")
    return rc


def disable(args) -> int:
    rc = apply(args, enable=False, label="disable")
    if rc == 0 and not args.dry_run:
        out("  gateway         : not restarted. Run in your own terminal: make hermes-restart")
    return rc


def enable(args) -> int:
    rc = apply(args, enable=True, label="enable")
    home = hermes_home()
    unit = gateway_unit()
    gw = gateway_state(unit)
    if rc != 0 or args.dry_run:
        blockers = restart_blockers(gw["pid"], unit)
        if args.dry_run:
            out("  restart         : " + ("refused here: " + "; ".join(blockers) if blockers
                                         else "would restart the gateway after asking"))
        return rc
    if gw["state"] != "active":
        out(f"  restart         : {unit} is {gw['state']}; not starting it from here.")
        out("                    Start it in your own terminal: hermes gateway start, then make specialist-status")
        return 3
    if restart_pending(home, gw) is False:
        out("  restart         : not needed (the gateway started after the last change)")
        return 0
    blockers = restart_blockers(gw["pid"], unit)
    if blockers:
        out("  restart         : REFUSED from here: " + "; ".join(blockers))
        out("                    Run in your own terminal: make hermes-restart && make specialist-status")
        return 3
    if not args.yes:
        if not sys.stdin.isatty():
            out("  restart         : no TTY to ask on. Re-run with YES=1, or: make hermes-restart")
            return 3
        answer = input(f"Restart {unit} now? Active chats are drained first. [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            out("  restart         : skipped. When ready: make hermes-restart && make specialist-status")
            return 0

    log = home / "logs" / "agent.log"
    offset = log.stat().st_size if log.exists() else 0
    out(f"  restart         : hermes gateway restart ({unit}, pid {gw['pid']})")
    if h_rc := Hermes(home).restart_gateway():
        raise Fail(f"hermes gateway restart exited {h_rc}; check: hermes gateway status")
    deadline = time.monotonic() + args.wait
    lines: List[str] = []
    while True:
        now = gateway_state(unit)
        lines = registrations(log, offset)
        if now["state"] == "active" and any("specialists=on" in line for line in lines):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    out(f"  gateway         : {now['state']}, pid {now['pid'] or '?'}"
        + (" (new)" if now["pid"] and now["pid"] != gw["pid"] else ""))
    if now["state"] != "active":
        raise Fail(f"{unit} is {now['state']} after the restart; check: hermes gateway status, make hermes-logs")
    if any("specialists=on" in line for line in lines):
        out("  verified        : model_router registered with specialists=on after the restart")
        return 0
    seen = lines[-1].split(REGISTERED, 1)[1].strip() if lines else "nothing"
    warn(f"no 'specialists=on' registration in {log} within {args.wait}s (last seen: {seen}). "
         "Check: make specialist-status, grep 'model_router' the log")
    return 4


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Install, enable and check the specialist-router pilot")
    ap.add_argument("command", choices=("status", "install", "enable", "disable"))
    ap.add_argument("--max", type=int, default=None, help="specialist_max_profiles (0-15; the plugin's HARD_MAX_PROFILES)")
    rr = ap.add_mutually_exclusive_group()
    rr.add_argument("--require-ready", dest="require_ready", action="store_true", default=None)
    rr.add_argument("--no-require-ready", dest="require_ready", action="store_false")
    ap.add_argument("--classifier", default=None, help="none | local | cliproxy")
    ap.add_argument("--mode", default=None, help="specialist_selection_mode: ai (default) | hybrid | keywords")
    ap.add_argument("--profiles", default=None, help="allowlist a,b,c (all = every profile)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    ap.add_argument("--yes", action="store_true", help="enable: restart without asking")
    ap.add_argument("--wait", type=int, default=90, help="enable: seconds to wait for the registration line")
    args = ap.parse_args(argv)
    try:
        return {"status": status, "install": install, "enable": enable, "disable": disable}[args.command](args)
    except Fail as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
