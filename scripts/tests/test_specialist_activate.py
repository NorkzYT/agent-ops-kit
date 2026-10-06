"""Tests for scripts/specialist-activate.py (make specialist-status/-install/-enable/-disable).

Offline: a temp HERMES_HOME, a stub ``hermes`` (config get/set + gateway
restart against a JSON store), a stub ``systemctl`` and a fake /proc tree. The
real gateway, the live config and the network are never touched.

Set SPECIALIST_REAL_HERMES=1 to also run ``install`` through the real
``hermes config set`` in a temp HERMES_HOME (never restarts anything).

Run: python3 -m unittest discover -s scripts/tests -p 'test_specialist*.py'
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _loader import REPO  # noqa: E402

SCRIPT = REPO / "scripts" / "specialist-activate.py"
PLUGIN_SRC = REPO / "hermes" / "plugins" / "model-router"
PREFIX = "plugins.entries.model-router.settings"

STUB_HERMES = r'''#!/usr/bin/env python3
import json, os, sys
home = os.environ["HERMES_HOME"]
store = os.path.join(home, "stub-config.json")
gw = os.path.join(home, "stub-gateway.json")
with open(os.path.join(home, "stub-calls.log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
def load():
    return json.load(open(store)) if os.path.exists(store) else {}
a = sys.argv[1:]
if a[:2] == ["config", "get"]:
    key, node = a[-1], load()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            print("Config key not set: " + key); sys.exit(1)
        node = node[part]
    print(json.dumps(node)); sys.exit(0)
if a[:2] == ["config", "set"]:
    key, raw = a[2], a[3]
    words = {"true": True, "false": False, "none": None, "null": None}
    if raw.lower() in words:
        val = words[raw.lower()]
    elif raw.lstrip("-").isdigit():
        val = int(raw)
    elif raw.startswith("["):
        val = [x.strip() for x in raw[1:-1].split(",") if x.strip()]
    else:
        val = raw
    data = load(); node = data
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = val
    json.dump(data, open(store, "w"))
    with open(os.path.join(home, "config.yaml"), "a") as f:
        f.write("# set " + key + "\n")
    sys.exit(0)
if a[:2] == ["gateway", "restart"]:
    state = json.load(open(gw))
    state["pid"] += 1
    json.dump(state, open(gw, "w"))
    on = load().get("plugins", {}).get("entries", {}).get("model-router", {}).get("settings", {}).get("specialist_routing")
    if os.environ.get("STUB_REGISTER") == "off":
        on = False
    os.makedirs(os.path.join(home, "logs"), exist_ok=True)
    with open(os.path.join(home, "logs", "agent.log"), "a") as f:
        f.write("2026-10-06 INFO hermes.plugin.model_router: model_router registered "
                "(privacy=True complexity=True local=unset specialists=%s)\n" % ("on" if on is True else "off"))
    sys.exit(0)
sys.exit(0)
'''

STUB_SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os
gw = os.path.join(os.environ["HERMES_HOME"], "stub-gateway.json")
state = json.load(open(gw)) if os.path.exists(gw) else {"state": "inactive", "pid": 0}
print("ActiveState=%s\nMainPID=%d" % (state["state"], state["pid"]))
'''


def stat_line(pid, ppid, ticks=0):
    return f"{pid} (proc) S {ppid} " + " ".join(["0"] * 17) + f" {ticks} 0 0\n"


class ActivateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="specialist-activate-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "hermes"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("hermes", STUB_HERMES), ("systemctl", STUB_SYSTEMCTL)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.cfg = self.home / "config.yaml"
        self.cfg.write_text("# rendered by hermes-install\n")
        self.settings_store({"privacy_routing": True})
        self.proc = self.tmp / "proc"
        self.fake_proc(cgroup="0::/user.slice/user@1000.service/app.slice/vte-spawn.scope\n",
                       parents=[(500, 1)])

    # -- fixtures --------------------------------------------------------- #
    def settings_store(self, settings, enabled=("model-router",)):
        data = {"plugins": {"enabled": list(enabled),
                            "entries": {"model-router": {"settings": dict(settings)}}}}
        (self.home / "stub-config.json").write_text(json.dumps(data))

    def settings(self):
        data = json.loads((self.home / "stub-config.json").read_text())
        return data["plugins"]["entries"]["model-router"]["settings"]

    def gateway(self, state="active", pid=4242, started_ago=10_000.0):
        (self.home / "stub-gateway.json").write_text(json.dumps({"state": state, "pid": pid}))
        clk = os.sysconf("SC_CLK_TCK")
        btime = int(time.time() - started_ago)
        (self.proc / "stat").write_text(f"cpu 0\nbtime {btime}\n")
        (self.proc / str(pid)).mkdir(parents=True, exist_ok=True)
        (self.proc / str(pid) / "stat").write_text(stat_line(pid, 1, 0 * clk))

    def fake_proc(self, cgroup, parents):
        """``parents``: [(pid, ppid), ...] walked upward from /proc/self."""
        (self.proc / "self").mkdir(parents=True, exist_ok=True)
        (self.proc / "self" / "cgroup").write_text(cgroup)
        (self.proc / "self" / "stat").write_text(stat_line(999, parents[0][0]))
        for pid, ppid in parents:
            (self.proc / str(pid)).mkdir(parents=True, exist_ok=True)
            (self.proc / str(pid) / "stat").write_text(stat_line(pid, ppid))
        (self.proc / "stat").write_text(f"btime {int(time.time()) - 10_000}\n")

    def ready_profile(self, name="strategy"):
        skill = self.home / "profiles" / name / "skills" / "knowledge" / f"{name}-knowledge"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}-knowledge\n---\n")
        (self.home / "profiles" / name / "knowledge").mkdir()

    def run_cmd(self, *args, env=None, stdin=subprocess.DEVNULL):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("HERMES_", "_HERMES_", "STUB_"))}
        e.update(PATH=f"{self.bin}:/usr/bin:/bin", HERMES_HOME=str(self.home),
                 HOME=str(self.tmp), SPECIALIST_PROC_ROOT=str(self.proc))
        e.update(env or {})
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=e, stdin=stdin,
                              capture_output=True, text=True, timeout=120)

    def calls(self):
        log = self.home / "stub-calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def writes(self):
        return [c for c in self.calls() if c.startswith(("config set", "gateway"))]

    def backups(self):
        return sorted(self.home.glob("config.yaml.bak.*"))

    def plugin_matches(self):
        dest = self.home / "plugins" / "model-router"
        for src in PLUGIN_SRC.rglob("*"):
            if src.is_file() and "__pycache__" not in src.parts and src.suffix != ".pyc":
                got = dest / src.relative_to(PLUGIN_SRC)
                if not got.is_file() or hashlib.sha256(got.read_bytes()).digest() != \
                        hashlib.sha256(src.read_bytes()).digest():
                    return False
        return True

    # -- status ----------------------------------------------------------- #
    def test_status_is_read_only(self):
        self.gateway()
        before = self.cfg.read_bytes()
        r = self.run_cmd("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plugin files    : not installed", r.stdout)
        self.assertIn("specialist_routing        (unset)", r.stdout)
        self.assertIn("hermes-gateway.service active, pid 4242", r.stdout)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertFalse((self.home / "plugins").exists())
        self.assertEqual(self.backups(), [])

    # -- install ---------------------------------------------------------- #
    def test_dry_run_writes_nothing(self):
        r = self.run_cmd("install", "--dry-run", "--max", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dry run", r.stdout)
        self.assertIn(f"set {PREFIX}.specialist_routing: (unset) -> true", r.stdout)
        self.assertIn(f"set {PREFIX}.specialist_max_profiles: (unset) -> 3", r.stdout)
        self.assertEqual(self.writes(), [])
        self.assertFalse((self.home / "plugins").exists())
        self.assertEqual(self.backups(), [])

    def test_install_sets_keys_copies_plugin_and_never_restarts(self):
        self.gateway()
        r = self.run_cmd("install")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.settings()
        self.assertIs(s["specialist_routing"], True)
        self.assertEqual(s["specialist_max_profiles"], 4)
        self.assertIs(s["specialist_require_ready"], False)
        self.assertIsNone(s["specialist_classifier"])     # none is stored as null
        self.assertEqual(s["specialist_profiles"], [])
        self.assertIs(s["privacy_routing"], True)          # other keys untouched
        self.assertTrue(self.plugin_matches())
        self.assertEqual(len(self.backups()), 1)
        self.assertIn("verified        : 5 settings read back", r.stdout)
        self.assertNotIn("gateway restart", "\n".join(self.calls()))
        self.assertIn("not restarted", r.stdout)

    def test_install_is_idempotent(self):
        self.assertEqual(self.run_cmd("install").returncode, 0)
        n = len(self.writes())
        r = self.run_cmd("install")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.writes()), n)
        self.assertEqual(len(self.backups()), 1)
        self.assertIn("plugin files    : in sync", r.stdout)

    def test_install_keeps_existing_values_unless_overridden(self):
        self.settings_store({"specialist_max_profiles": 2, "specialist_profiles": ["strategy"]})
        r = self.run_cmd("install")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.settings()["specialist_max_profiles"], 2)
        self.assertEqual(self.settings()["specialist_profiles"], ["strategy"])
        r = self.run_cmd("install", "--max", "5", "--profiles", "all", "--classifier", "local")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.settings()
        self.assertEqual((s["specialist_max_profiles"], s["specialist_profiles"],
                          s["specialist_classifier"]), (5, [], "local"))

    def test_install_rejects_bad_input_before_writing(self):
        for args in (("--max", "7"), ("--profiles", "strategy,bogus"), ("--classifier", "cloud")):
            r = self.run_cmd("install", *args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("error:", r.stderr)
        self.assertEqual(self.writes(), [])

    def test_install_refuses_without_plugin_enabled_or_config(self):
        self.settings_store({}, enabled=("other",))
        r = self.run_cmd("install")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not in plugins.enabled", r.stderr)
        self.cfg.unlink()
        r = self.run_cmd("install")
        self.assertEqual(r.returncode, 1)
        self.assertIn("make hermes-install", r.stderr)
        self.assertEqual(self.writes(), [])

    def test_require_ready_needs_a_ready_profile(self):
        r = self.run_cmd("install", "--require-ready")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing would route", r.stderr)
        self.assertEqual(self.writes(), [])
        self.ready_profile("strategy")
        r = self.run_cmd("install", "--require-ready")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ready 1", r.stdout)
        self.assertIs(self.settings()["specialist_require_ready"], True)

    def test_read_back_mismatch_fails(self):
        # A hermes that accepts the write but stores something else.
        stub = self.bin / "hermes"
        stub.write_text(stub.read_text().replace('node[parts[-1]] = val', 'node[parts[-1]] = "x"'))
        r = self.run_cmd("install")
        self.assertEqual(r.returncode, 1)
        self.assertIn("read-back failed", r.stderr)

    # -- enable ----------------------------------------------------------- #
    def test_enable_restarts_and_verifies(self):
        self.gateway()
        r = self.run_cmd("enable", "--yes", "--wait", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sum(c == "gateway restart" for c in self.calls()), 1)
        self.assertIn("pid 4243 (new)", r.stdout)
        self.assertIn("registered with specialists=on", r.stdout)

    def test_enable_reports_when_specialists_not_on(self):
        self.gateway()
        r = self.run_cmd("enable", "--yes", "--wait", "1", env={"STUB_REGISTER": "off"})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("specialists=off", r.stderr)

    def test_enable_without_tty_or_yes_does_not_restart(self):
        self.gateway()
        r = self.run_cmd("enable")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("YES=1", r.stdout)
        self.assertIs(self.settings()["specialist_routing"], True)
        self.assertNotIn("gateway restart", self.calls())

    def test_enable_refused_inside_gateway_session(self):
        self.gateway()
        r = self.run_cmd("enable", "--yes", env={"HERMES_GATEWAY_SESSION": "1"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("REFUSED", r.stdout)
        self.assertIn("make hermes-restart", r.stdout)
        self.assertNotIn("gateway restart", self.calls())

    def test_enable_refused_under_gateway_process(self):
        self.gateway(pid=4242)
        self.fake_proc(cgroup="0::/user.slice/app.slice/other.scope\n",
                       parents=[(700, 4242), (4242, 1)])
        r = self.run_cmd("enable", "--yes")
        self.assertEqual(r.returncode, 3)
        self.assertIn("the gateway (pid 4242) is an ancestor", r.stdout)
        self.assertNotIn("gateway restart", self.calls())

    def test_enable_refused_from_agent_service(self):
        self.gateway()
        self.fake_proc(cgroup="0::/user.slice/app.slice/claude-max-proxy.service\n",
                       parents=[(800, 1)])
        r = self.run_cmd("enable", "--yes")
        self.assertEqual(r.returncode, 3)
        self.assertIn("claude-max-proxy.service", r.stdout)
        self.assertNotIn("gateway restart", self.calls())

    def test_enable_skips_restart_when_gateway_is_newer(self):
        self.run_cmd("install")
        self.gateway(started_ago=-60)          # started after every change
        r = self.run_cmd("enable", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not needed", r.stdout)
        self.assertNotIn("gateway restart", self.calls())

    def test_enable_never_starts_a_stopped_gateway(self):
        self.gateway(state="inactive", pid=0)
        r = self.run_cmd("enable", "--yes")
        self.assertEqual(r.returncode, 3)
        self.assertIn("not starting it", r.stdout)
        self.assertNotIn("gateway restart", self.calls())

    def test_enable_dry_run_reports_the_guard(self):
        self.gateway()
        r = self.run_cmd("enable", "--dry-run", env={"HERMES_GATEWAY_SESSION": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("refused here", r.stdout)
        self.assertEqual(self.writes(), [])

    # -- disable ---------------------------------------------------------- #
    def test_disable_turns_it_off_without_restart(self):
        self.gateway()
        self.assertEqual(self.run_cmd("install").returncode, 0)
        r = self.run_cmd("disable")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIs(self.settings()["specialist_routing"], False)
        self.assertNotIn("gateway restart", self.calls())


class MakefileTest(unittest.TestCase):
    def make_n(self, *args, env=None):
        e = dict(os.environ, **(env or {}))
        return subprocess.run(["make", "-n", *args], cwd=REPO, env=e,
                              capture_output=True, text=True, timeout=30).stdout

    @unittest.skipUnless(shutil.which("make"), "make not installed")
    def test_help_lists_the_activation_targets(self):
        out = subprocess.run(["make", "help"], cwd=REPO, capture_output=True, text=True).stdout
        for target in ("specialist-status", "specialist-install", "specialist-enable", "specialist-disable"):
            self.assertIn(target, out)

    @unittest.skipUnless(shutil.which("make"), "make not installed")
    def test_yes_counts_only_on_the_command_line(self):
        self.assertIn("== \"1\" ]] && args+=(--yes)", self.make_n("specialist-enable", "YES=1"))
        self.assertIn('[[ "1" == "1" ]] && args+=(--yes)', self.make_n("specialist-enable", "YES=1"))
        self.assertIn('[[ "" == "1" ]] && args+=(--yes)', self.make_n("specialist-enable", env={"YES": "1"}))

    @unittest.skipUnless(shutil.which("make"), "make not installed")
    def test_install_target_never_passes_yes(self):
        self.assertNotIn("--yes", self.make_n("specialist-install", "YES=1"))


@unittest.skipUnless(os.environ.get("SPECIALIST_REAL_HERMES") == "1" and shutil.which("hermes"),
                     "set SPECIALIST_REAL_HERMES=1 with hermes on PATH")
class RealHermesTest(unittest.TestCase):
    """``install`` through the real ``hermes config set`` in a temp HERMES_HOME."""

    def test_install_round_trip(self):
        tmp = Path(tempfile.mkdtemp(prefix="specialist-real-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "h"
        home.mkdir()
        (home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - model-router\n  entries:\n    model-router:\n"
            "      settings:\n        privacy_routing: true\n        specialist_max_profiles: 2\n")
        env = dict(os.environ, HERMES_HOME=str(home), SPECIALIST_PROC_ROOT=str(tmp / "noproc"))
        r = subprocess.run([sys.executable, str(SCRIPT), "install", "--classifier", "none"],
                           env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = (home / "config.yaml").read_text()
        self.assertIn("specialist_routing: true", text)
        self.assertIn("specialist_max_profiles: 2", text)
        self.assertIn("privacy_routing: true", text)
        r = subprocess.run([sys.executable, str(SCRIPT), "install"], env=env,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("\n  set ", r.stdout)                 # idempotent


if __name__ == "__main__":
    unittest.main()
