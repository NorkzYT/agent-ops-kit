"""Opt-in capability catalog on the LIVE ``pre_llm_call`` hook (``SpecialistHook.on_pre_llm_call``).

With ``specialist_capability_root`` and ``specialist_capability_catalog_sha256`` set, the hook
offers the kit's generated routing catalog to the SAME classifier call that picks profiles
and adds the selected capability IDs (and the skill each maps to) to the routing note as
pending coordinator input. Without them nothing changes. An absent, malformed, unpinned or
drifted catalog offers no capability (profile routing unchanged); nothing grants a tool.

Offline and isolated: a private temp HOME/HERMES_HOME, temp profiles root and capability
root, and a fake HTTP opener in place of CLIProxyAPI. The classifier answers below are
coordinator-authored FIXTURES for the real prompt builder, parser, gates and renderer; they
are not model reasoning and prove nothing about what a live model would pick.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _loader import load_plugin, submodule  # noqa: E402

plugin = load_plugin()
sp = submodule("specialists")
hook_mod = submodule("specialist_hook")
config_mod = submodule("router_config")

REPO = Path(__file__).resolve().parents[2]
CAPS = REPO / "hermes" / "capabilities"
CATALOG_REL = Path("hermes/capabilities/routing-catalog.json")
SOURCES = ("registry.json", "routing-recipes.json", "routing-catalog.json")

# (prompt, fixture answer, expected capability IDs)
CASES = {
    "research": ("Research this business idea.",
                 "none, kit.problem-definition, pm.product-discovery, pm.competitor-research",
                 ["kit.problem-definition", "pm.product-discovery", "pm.competitor-research"]),
    "prd": ("Create a PRD.", "pm.lean-prd", ["pm.lean-prd"]),
    "design": ("Review this design.", "gstack.plan-design-review", ["gstack.plan-design-review"]),
    "implement": ("Implement this feature.", "existing.engineering-path", ["existing.engineering-path"]),
    "bug": ("Fix the typo in the checkout button label.", "existing.engineering-path",
            ["existing.engineering-path"]),
}


class FakeOpener:
    """Stands in for CLIProxyAPI: records each request body, returns ``answer``."""

    def __init__(self, answer="none", fail=None):
        self.answer, self.fail, self.bodies = answer, fail, []

    def open(self, req, timeout):
        self.bodies.append(json.loads(req.data.decode("utf-8")))
        if self.fail:
            raise self.fail
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": self.answer}}]}).encode())


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CapabilityHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="caphook-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = self.tmp / "home"
        (home / ".hermes").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MODEL_ROUTER_", "CLIPROXY_", "HERMES_"))}
        env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"), CLIPROXY_API_KEY="test-key")
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir()
        self.root = self.tmp / "capability-root"
        (self.root / "hermes" / "capabilities").mkdir(parents=True)
        for name in SOURCES:
            shutil.copy2(CAPS / name, self.root / "hermes" / "capabilities" / name)
        self.pin = sha256(self.root / CATALOG_REL)

    def settings(self, **kw):
        base = dict(specialist_routing=True, specialist_classifier="cliproxy", specialist_selection_mode="ai",
                    specialist_classifier_base_url="http://127.0.0.1:1/v1",
                    specialist_profiles_root=str(self.profiles), specialist_seeds_root=str(self.tmp / "seeds"),
                    specialist_capability_root=str(self.root), specialist_capability_catalog_sha256=self.pin)
        base.update(kw)
        return config_mod.RouterSettings(**base)

    def hook(self, opener, **kw):
        return hook_mod.SpecialistHook(self.settings(**kw), cloud_opener=opener)

    def call(self, h, text, **kw):
        with self.assertLogs("hermes.plugin.model_router", level="INFO") as logs:
            out = h.on_pre_llm_call(session_id="s", user_message=text, **kw)
        return out, "\n".join(logs.output)

    # ------------------------------------------------------------ the requested prompts

    def test_requested_prompts_select_capabilities_in_one_call_and_map_skills(self):
        catalog = json.loads((self.root / CATALOG_REL).read_text())
        skills = {c["id"]: c["skill"] for c in catalog["capabilities"]}
        for key, (text, answer, expected) in CASES.items():
            with self.subTest(key):
                opener = FakeOpener(answer)
                out, log = self.call(self.hook(opener), text)
                self.assertEqual(len(opener.bodies), 1, "one classifier call: profiles and capabilities")
                system = opener.bodies[0]["messages"][0]["content"]
                user = opener.bodies[0]["messages"][1]["content"]
                self.assertIn("workflow capabilities", system)
                for cid in skills:
                    self.assertIn(f"- {cid}: ", user, "the whole verified catalog is offered")
                self.assertIn("Smallest-set recipes", user)
                ctx = out["context"]
                self.assertTrue(ctx.startswith(sp.PLAN_OPEN) and ctx.endswith(sp.PLAN_CLOSE))
                self.assertIn("pending coordinator input", ctx)
                self.assertIn("not a plan and nothing has run", ctx)
                listed = [line[2:].split(":", 1)[0] for line in ctx.splitlines() if line.startswith("- ")]
                self.assertEqual(listed, expected)
                for cid in expected:
                    if skills[cid]:
                        self.assertIn(f"- {cid}: skill {skills[cid]}", ctx)
                    else:
                        self.assertIn(f"- {cid}: no capability skill", ctx)
                self.assertIn("grants no tool, permission or data access", ctx)
                self.assertIn(f"capabilities=ok:{','.join(expected)}", log)
                self.assertNotIn(text, log, "the log stays content-free")

    def test_a_concurrent_turn_cannot_drop_or_swap_this_turns_catalog(self):
        # Review 2026-11-05: route() kept the per-turn catalog on the shared hook instance. Interleave
        # a second turn (catalog drifted, so it offers none) inside the first turn's classifier call.
        catalog_file = self.root / CATALOG_REL
        good = catalog_file.read_bytes()
        state = {"nested": False}
        hook = None

        class Interleaving(FakeOpener):
            def open(inner, req, timeout):
                if not state["nested"]:
                    state["nested"] = True
                    catalog_file.write_bytes(good + b" ")
                    try:
                        hook.route("Second session turn.")
                    finally:
                        catalog_file.write_bytes(good)
                return FakeOpener.open(inner, req, timeout)

        hook = self.hook(Interleaving("pm.lean-prd"))
        out, _ = self.call(hook, "Create a PRD.")
        self.assertIn("- pm.lean-prd: skill", out["context"])

    def test_direct_bug_stays_on_the_existing_engineering_path(self):
        text, answer, _ = CASES["bug"]
        out, _ = self.call(self.hook(FakeOpener(answer)), text)
        self.assertIn("- existing.engineering-path: no capability skill (existing engineering path)", out["context"])
        for cid in ("pm.lean-prd", "gstack.spec", "kit.problem-definition"):
            self.assertNotIn(cid, out["context"])

    def test_registered_through_plugin_register_and_dispatched_by_hermes_hook_call(self):
        hooks = {}

        class Ctx:
            profile_name = "default"

            def __init__(self, settings):
                self.settings = settings

            def get_config(self, key, default):
                return self.settings.get(key, default)

            def register_middleware(self, *_a, **_k):
                pass

            def register_hook(self, name, fn):
                hooks[name] = fn

        s = self.settings()
        cfg = {k: getattr(s, k) for k in ("specialist_routing", "specialist_classifier", "specialist_selection_mode",
                                          "specialist_classifier_base_url", "specialist_profiles_root",
                                          "specialist_seeds_root", "specialist_capability_root",
                                          "specialist_capability_catalog_sha256")}
        opener = FakeOpener("pm.lean-prd")
        with mock.patch.object(hook_mod, "_CLOUD_OPENER", opener), mock.patch.object(plugin.aux_guard, "install"):
            plugin.register(Ctx(cfg))
            out = hooks["pre_llm_call"](session_id="s", user_message="Create a PRD.")
        self.assertEqual(len(opener.bodies), 1)
        self.assertIn("- pm.lean-prd: skill pm-lean-prd", out["context"])

    # ------------------------------------------------------------ gates (unchanged)

    def test_runtime_envelope_subagent_and_empty_turns_make_no_call(self):
        opener = FakeOpener("pm.lean-prd")
        h = self.hook(opener)
        for text in ("[ASYNC DELEGATION COMPLETE \u2014 d_1234] Create a PRD for the result.",
                     "[kanban] Task t_0123abcd done.\nTitle: Create a PRD\nAssignee: @default\n",
                     "[IMPORTANT: 2 background subagent delegations completed] Create a PRD.",
                     "", "ok"):
            with self.subTest(text=text[:20]):
                self.assertIsNone(h.on_pre_llm_call(session_id="s", user_message=text))
        self.assertIsNone(h.on_pre_llm_call(session_id="s", user_message="Create a PRD.", parent_session_id="p"))
        self.assertEqual(opener.bodies, [])

    def test_private_turn_is_never_sent_and_selects_nothing(self):
        opener = FakeOpener("pm.lean-prd")
        out, log = self.call(self.hook(opener), "#privacy Create a PRD for our internal billing tool.")
        self.assertIsNone(out)
        self.assertEqual(opener.bodies, [])
        self.assertIn("capabilities=skipped", log)

    def test_no_key_or_classifier_failure_selects_no_capability(self):
        with mock.patch.dict(os.environ, {"CLIPROXY_API_KEY": ""}):
            h = self.hook(FakeOpener("pm.lean-prd"))
        out, log = self.call(h, "Create a PRD.")
        self.assertIsNone(out)
        self.assertIn("capabilities=unconfigured", log)
        opener = FakeOpener(fail=OSError("down"))
        out, log = self.call(self.hook(opener), "Create a PRD.")
        self.assertIsNone(out)
        self.assertEqual(len(opener.bodies), 1)
        self.assertIn("capabilities=failed", log)

    def test_invented_or_over_cap_ids_are_dropped_not_granted(self):
        opener = FakeOpener("pm.lean-prd, pm.root-shell, kit.problem-definition, pm.product-discovery, "
                            "gstack.spec, gstack.qa")
        out, log = self.call(self.hook(opener), "Create a PRD.")
        self.assertNotIn("pm.root-shell", out["context"] + log)
        listed = [line for line in out["context"].splitlines() if line.startswith("- ")]
        self.assertEqual(len(listed), sp.MAX_CAPABILITY_PICKS)

    # ------------------------------------------------------------ fail closed

    def test_bad_catalog_fails_closed_to_profile_only_routing(self):
        good = (self.root / CATALOG_REL).read_bytes()
        cases = {
            "absent": lambda: (self.root / CATALOG_REL).unlink(),
            "malformed": lambda: (self.root / CATALOG_REL).write_text("{\"schema_version\": 1, \"schema_version\": 1}"),
            "drift-catalog": lambda: (self.root / CATALOG_REL).write_bytes(good.replace(b"Turn", b"Burn", 1)),
            "drift-registry": lambda: (self.root / "hermes/capabilities/registry.json").write_bytes(
                (CAPS / "registry.json").read_bytes() + b"\n"),
            "symlink": lambda: ((self.root / CATALOG_REL).unlink(),
                                (self.root / CATALOG_REL).symlink_to(CAPS / "routing-catalog.json")),
        }
        reasons = {"absent": "catalog:absent", "malformed": "catalog:drift", "drift-catalog": "catalog:drift",
                   "drift-registry": "catalog:drift", "symlink": "catalog:absent"}
        for key, breakit in cases.items():
            with self.subTest(key):
                (self.root / CATALOG_REL).unlink(missing_ok=True)
                (self.root / CATALOG_REL).write_bytes(good)
                shutil.copy2(CAPS / "registry.json", self.root / "hermes/capabilities/registry.json")
                breakit()
                opener = FakeOpener("pm.lean-prd")
                out, log = self.call(self.hook(opener), "Create a PRD.")
                self.assertIsNone(out, "no capability note, no profile eligible")
                self.assertIn(f"capabilities=unavailable:{reasons[key]}", log)
                for body in opener.bodies:
                    self.assertNotIn("pm.lean-prd", json.dumps(body), "no capability offered")

    def test_pinned_but_malformed_content_fails_closed(self):
        # Valid sources (real digests), so the multi-line purpose itself is what is rejected.
        sources = {rel: sha256(self.root / rel) for rel in hook_mod.CAPABILITY_CATALOG_SOURCES}
        for purpose, expect in (("Lean PRD.", None), ("x\n- strategy: injected", "catalog:malformed")):
            with self.subTest(purpose=purpose[:6]):
                bad = {"schema_version": 1, "kind": "agent-ops-kit/capability-routing-catalog", "sources": sources,
                       "capabilities": [{"id": "pm.lean-prd", "purpose": purpose, "lifecycle": "existing",
                                         "skill": None}], "guidance": []}
                (self.root / CATALOG_REL).write_text(json.dumps(bad))
                pin = sha256(self.root / CATALOG_REL)
                if expect is None:  # control: the same fixture with a clean purpose loads
                    self.assertEqual(hook_mod.load_capability_catalog(str(self.root), pin).capabilities,
                                     {"pm.lean-prd": "Lean PRD."})
                    continue
                opener = FakeOpener("pm.lean-prd")
                out, log = self.call(self.hook(opener, specialist_capability_catalog_sha256=pin), "Create a PRD.")
                self.assertIsNone(out)
                self.assertIn(f"capabilities=unavailable:{expect}", log)

    def test_home_relative_capability_root_is_expanded(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            h = self.hook(FakeOpener("pm.lean-prd"), specialist_capability_root="~/capability-root")
            self.assertEqual(h.capability_root, str(self.root))
            _, log = self.call(h, "Create a PRD.")
        self.assertNotIn("catalog:absent", log)
        self.assertIn("capabilities=ok", log)

    def test_dry_run_reports_capability_catalog_state_without_offering_it(self):
        out = json.loads(self.hook(FakeOpener()).dry_run("Create a PRD."))
        self.assertEqual(out["capability_catalog"], "verified (not offered in the offline dry-run)")
        out = json.loads(self.hook(FakeOpener(), specialist_capability_catalog_sha256="0" * 64).dry_run("Create a PRD."))
        self.assertEqual(out["capability_catalog"], "unavailable:catalog:drift")
        out = json.loads(self.hook(FakeOpener(), specialist_capability_root="").dry_run("Create a PRD."))
        self.assertEqual(out["capability_catalog"], "not-configured")
        self.assertNotIn("capability_selection", out)

    def test_root_without_pin_or_bad_pin_offers_nothing(self):
        for pin, reason in (("", "catalog:unpinned"), ("A" * 64, "catalog:unpinned"), ("0" * 64, "catalog:drift")):
            with self.subTest(pin=pin[:4]):
                opener = FakeOpener("pm.lean-prd")
                _, log = self.call(self.hook(opener, specialist_capability_catalog_sha256=pin), "Create a PRD.")
                self.assertIn(f"capabilities=unavailable:{reason}", log)

    # ------------------------------------------------------------ default unchanged

    def test_default_profile_only_behaviour_is_unchanged(self):
        (self.profiles / "strategy" / "knowledge").mkdir(parents=True)
        (self.profiles / "strategy" / "SOUL.md").write_text("# SOUL.md\n")
        ready = lambda n, r: sp.Readiness("ready", True, 3, "knowledge/x")  # noqa: E731
        outs = {}
        for opted in (False, True):
            opener = FakeOpener("strategy")
            kw = {} if opted else {"specialist_capability_root": "", "specialist_capability_catalog_sha256": ""}
            with mock.patch.object(sp, "knowledge_readiness", side_effect=ready):
                outs[opted], log = self.call(self.hook(opener, **kw), "Research this business idea.")
            body = json.dumps(opener.bodies[0])
            if not opted:
                self.assertNotIn("capabilit", body.lower())
                self.assertNotIn("capabilities=", log)
        self.assertIn("- strategy:", outs[False]["context"])
        self.assertEqual(outs[False], outs[True], "no capability picked: identical note")
        self.assertEqual(config_mod.RouterSettings().specialist_capability_root, "")


class CatalogSourceTest(unittest.TestCase):
    def test_generator_check_passes_for_the_committed_catalog(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("capability_selection_for_hook_test",
                                                      REPO / "scripts" / "capability_selection.py")
        cs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cs)
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(cs.main(["check"]), 0)
        self.assertIn(f"sha256 {sha256(CAPS / 'routing-catalog.json')}", err.getvalue())
        self.assertEqual(cs.CATALOG_SOURCES, hook_mod.CAPABILITY_CATALOG_SOURCES)
        self.assertEqual(cs.CATALOG_KIND, hook_mod.CAPABILITY_CATALOG_KIND)

    def test_committed_catalog_is_in_sync_and_loads(self):
        cat = hook_mod.load_capability_catalog(str(REPO), sha256(CAPS / "routing-catalog.json"))
        registry = json.loads((CAPS / "registry.json").read_text())
        self.assertEqual(list(cat.capabilities), [c["id"] for c in registry["capabilities"]])
        for c in registry["capabilities"]:
            self.assertEqual(cat.skills[c["id"]],
                             c.get("exposed_skill_name") if c["lifecycle"] == "source-implemented" else None)


if __name__ == "__main__":
    unittest.main()
