"""Strict-input hardening and policy narrowing for the capability registry/planner.

Covers the Milestone A review concerns: a FIFO must not block the loader before
fstat, nesting depth is bounded (no RecursionError), project policy also narrows
producers the planner would add automatically, and a kit skill's own SKILL.md is
not a name collision with the capability that implements it. Temp dirs only.
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_plan import REG, REPO, cr, envelope, plan, selection, stage_ids  # noqa: E402


class FifoAndDepthTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_fifo_is_refused_without_waiting_for_a_writer(self):
        fifo = self.tmp / "input.json"
        os.mkfifo(fifo)
        outcome = {}

        def run():
            try:
                cr.load_json(fifo)
                outcome["result"] = "loaded"
            except cr.RegistryError as exc:
                outcome["result"] = str(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(3)
        if t.is_alive():  # release the blocked open so the test process can exit
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            t.join(3)
            self.fail("load_json blocked on a FIFO before checking the file type")
        self.assertIn("not a regular file", outcome["result"])

    def test_nesting_depth_is_bounded(self):
        for text in ("[" * 65 + "]" * 65, '{"a":' * 65 + "1" + "}" * 65):
            with self.subTest(kind=text[0]), self.assertRaisesRegex(cr.RegistryError, "nesting"):
                cr.parse_json(text)
        self.assertEqual(cr.parse_json("[" * 64 + "]" * 64), cr.parse_json("[" * 64 + "]" * 64))

    def test_pathological_depth_is_a_registry_error_not_a_recursion_error(self):
        with self.assertRaisesRegex(cr.RegistryError, "nesting"):
            cr.parse_json("[" * 200000 + "]" * 200000)

    def test_brackets_inside_strings_do_not_count_as_nesting(self):
        self.assertEqual(cr.parse_json('{"a": "' + "[{" * 500 + '"}')["a"], "[{" * 500)

    def test_depth_limit_applies_to_files(self):
        p = self.tmp / "deep.json"
        p.write_text("[" * 100 + "]" * 100)
        with self.assertRaisesRegex(cr.RegistryError, "nesting"):
            cr.load_json(p)


class ProjectPolicyNarrowsProducersTest(unittest.TestCase):
    def project(self, approved):
        p = cr.load_json(REPO / "hermes/projects/examples/example-web.json")
        p = copy.deepcopy(p)
        p["approved_capability_ids"] = approved
        return p

    def test_unapproved_producer_is_not_added_and_the_input_is_requested(self):
        p = plan(selection("pm.lean-prd"), project=self.project(["pm.lean-prd"]))
        self.assertEqual(stage_ids(p), ["pm.lean-prd"])
        self.assertEqual(p["status"], "needs_input")
        need = next(n for n in p["needs_input"] if n["input"] == "opportunity")
        self.assertIn("project policy", need["reason"])
        self.assertTrue(any("kit.problem-definition" in r and "project policy" in r for r in p["reasons"]))

    def test_an_undefined_consumer_stays_blocked_even_when_input_is_also_needed(self):
        reg = copy.deepcopy(REG)
        c = next(c for c in reg["capabilities"] if c["id"] == "pm.lean-prd")
        c.update(lifecycle="planned", implementation={"paths": [], "digest": None})
        p = cr.build_plan(reg, selection("pm.lean-prd"), envelope(), repo=REPO, project=self.project(["pm.lean-prd"]))
        self.assertEqual(p["status"], "blocked")  # blocked outranks needs_input: nothing undefined looks runnable
        self.assertTrue(any(n["input"] == "opportunity" for n in p["needs_input"]))
        self.assertFalse(p["executable_now"])

    def test_approved_producer_is_still_added(self):
        p = plan(selection("pm.lean-prd"), project=self.project(["pm.lean-prd", "kit.problem-definition"]))
        self.assertEqual(stage_ids(p), ["kit.problem-definition", "pm.lean-prd"])

    def test_without_a_project_manifest_producers_are_added_as_before(self):
        self.assertEqual(stage_ids(plan(selection("pm.lean-prd"))), ["kit.problem-definition", "pm.lean-prd"])


class OwnSkillIsNotACollisionTest(unittest.TestCase):
    def errors_for(self, cap, kit_names):
        lock = cr.load_registry()[1]
        caps = {c["id"]: c for c in REG["capabilities"]}
        caps[cap["id"]] = cap
        errs = cr._capability_semantics(cap, caps, lock, cr.lock_index(lock), set(REG["reserved_skill_names"]),
                                        kit_names, REPO)
        return [e for e in errs if "collides" in e]

    def test_capability_implementing_the_kit_skill_does_not_collide_with_it(self):
        cap = copy.deepcopy(next(c for c in REG["capabilities"] if c["id"] == "kit.problem-definition"))
        rel = "hermes/skills/problem-definition/SKILL.md"
        cap["implementation"] = {"paths": [rel], "digest": "0" * 64}
        self.assertEqual(self.errors_for(cap, {"problem-definition": rel}), [])

    def test_another_capability_exposing_an_existing_kit_skill_name_collides(self):
        cap = copy.deepcopy(next(c for c in REG["capabilities"] if c["id"] == "kit.operational-analysis"))
        cap["exposed_skill_name"] = "problem-definition"
        self.assertTrue(self.errors_for(cap, {"problem-definition": "hermes/skills/problem-definition/SKILL.md"}))


if __name__ == "__main__":
    unittest.main()
