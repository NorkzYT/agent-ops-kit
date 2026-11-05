"""gstack.review and gstack.qa (C3): audits that never modify what they inspect.

rota-export-code-review audits a synthetic diff: PR type from its paths, every checklist category
assessed, findings bound to the quoted line they cite, forced-critical classes, scope drift, no fixes,
and no test result without a captured log. capability-cli-qa is a real functional QA pass over this
checkout's capability_workflow.py CLI: each pass/fail comes from a captured command whose exact probe
and exit status the validator re-reads, untested contracts stay not run, and the replay test re-runs
the captured probes to show the captures still describe the code.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_discovery import DiscoveryCase, errors, section  # noqa: E402
from workflow_helpers import EVALS, REG, REPO, cr, mutate, repack, wf  # noqa: E402

import capability_methods as cm  # noqa: E402


def fnd(a, fid):
    return next(f for f in a["findings"] if f["id"] == fid)


def probe(a, pid):
    return next(p for p in a["charter"] if p["id"] == pid)


def tree_digest(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(d)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


class PrType(DiscoveryCase):
    scenario, analysis = "rota-export-code-review", "analysis-audit"

    def test_pr_type_follows_the_checklist_path_rules(self):
        self.assertEqual(cm.pr_type(["docs/guide.md", "README.md"]), "docs-only")
        self.assertEqual(cm.pr_type(["hermes/skills/x/SKILL.md"]), "application")
        self.assertEqual(cm.pr_type([".github/workflows/ci.yml", "Dockerfile"]), "ci-infra")
        self.assertEqual(cm.pr_type(["scripts/run.sh", "Makefile"]), "scripts-tooling")
        self.assertEqual(cm.pr_type(["scripts/tool.py"]), "application")
        self.assertEqual(cm.pr_type(["scripts/run.sh", "docs/a.md"]), "mixed")

    def test_diff_parser_maps_new_file_lines(self):
        text = (EVALS / self.scenario / "inputs" / "rota-export.diff").read_text()
        files = cm.parse_diff(text)
        self.assertEqual(sorted(files), ["docs/rota-export.md", "scripts/billing_macros.py", "scripts/rota_export.py"])
        self.assertIn("shell=True", files["scripts/rota_export.py"]["lines"][17])
        self.assertIn(17, files["scripts/rota_export.py"]["added"])
        self.assertEqual(files["scripts/billing_macros.py"]["added"], {12})
        self.assertIn("invoice-copy", files["scripts/billing_macros.py"]["lines"][11])

    def test_diff_parser_keeps_a_blank_context_line_with_its_space_stripped(self):
        # review 2026-11-05 (wor12): an empty line inside a hunk ended the hunk early
        text = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,4 +1,5 @@\n a = 1\n\n b = 2\n+c = 3\n d = 4\n"
                "\n")  # trailing blank after the hunk is not a phantom line
        files = cm.parse_diff(text)
        self.assertEqual(files["x.py"]["lines"], {1: "a = 1", 2: "", 3: "b = 2", 4: "c = 3", 5: "d = 4"})
        self.assertEqual(files["x.py"]["added"], {4})


class CodeReview(DiscoveryCase):
    scenario, analysis = "rota-export-code-review", "analysis-audit"

    def test_renders_audit_report_bound_to_the_diff_without_changing_it(self):
        before = tree_digest(EVALS / self.scenario)
        out = self.render(self.a)
        self.assertEqual(tree_digest(EVALS / self.scenario), before)
        rep = (out / "review-report.md").read_text()
        self.assertIn("PR type: mixed", rep)
        self.assertIn("Pre-Landing Review: 4 issues (2 critical, 2 informational)", rep)
        self.assertIn("[CRITICAL] (confidence: 9/10) `f-shell` scripts/rota_export.py:17", rep)
        self.assertIn("SCOPE CREEP: scripts/billing_macros.py", section(rep, "Scope check"))
        self.assertIn("python3 -m unittest discover -s tests: not run by this review", section(rep, "Tests"))
        self.assertIn("This review changed no source", rep)
        self.assertIn("shell-injection: findings", section(rep, "Categories"))
        typed = cr.load_json(out / "review-findings.json")
        self.assertEqual(typed["reviewed"][0]["digest"], self.a["reviewed_artifacts"][0]["digest"])
        self.assertEqual([f["id"] for f in typed["findings"]], ["f-shell", "f-rmtree", "f-localtime", "f-notests"])
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "needs_input")
        self.assertTrue(any("no test" in x.lower() for x in result["limitations"]))

    def test_pr_type_and_category_coverage(self):
        self.assert_error(self.errs(lambda b: b.update(pr_type="application")),
                          r"PR type is mixed \(.*docs/rota-export.md.*\), not application")
        self.assert_error(self.errs(lambda b: b["categories"]["view-frontend"].update(status="not-applicable")),
                          r"on a mixed diff every category runs; view-frontend cannot be not-applicable")
        self.assert_error(self.errs(lambda b: b["categories"]["shell-injection"].update(status="clear")),
                          r"category shell-injection is clear but has findings f-shell")
        self.assert_error(self.errs(lambda b: b["categories"]["race-conditions"].update(status="findings")),
                          r"category race-conditions says findings but has none")

    def test_findings_are_bound_to_the_quoted_diff_line(self):
        self.assert_error(self.errs(lambda b: fnd(b, "f-shell")["location"].update(line=16)),
                          r"f-shell: the quoted text is not on scripts/rota_export.py:16")
        self.assert_error(self.errs(lambda b: fnd(b, "f-shell")["location"].update(file="scripts/other.py")),
                          r"f-shell: scripts/other.py is not in the reviewed diff")
        self.assert_error(self.errs(lambda b: fnd(b, "f-localtime")["evidence"][0].update(text="datetime.now(tz=UTC)")),
                          r"f-localtime: quoted text is not in t-rota-export.change")

    def test_forced_critical_classes_and_no_fixes(self):
        self.assert_error(self.errs(lambda b: fnd(b, "f-rmtree").update(severity="informational")),
                          r"f-rmtree is data-loss: CRITICAL on sight")
        self.assert_error(self.errs(lambda b: fnd(b, "f-shell").update(recommendation="@@ -17 +17 @@\n-    subprocess.run(x, shell=True)")),
                          r"f-shell recommendation is a patch")
        self.assertTrue(self.errs(lambda b: b.update(fixes_applied=["f-shell"])))  # the contract has no fix field

    def test_scope_drift_names_files_in_the_diff_and_intent_is_quoted(self):
        self.assert_error(self.errs(lambda b: b["scope_check"]["scope_creep"].append("scripts/unrelated.py")),
                          r"scope creep scripts/unrelated.py is not in the reviewed diff")
        self.assert_error(self.errs(lambda b: b["intent"].update(text="Refactor the billing macros.")), r"intent: quoted text is not in")

    def test_test_results_need_a_real_captured_log(self):
        self.assert_error(self.errs(lambda b: b["test_runs"][0].update(status="supplied")),
                          r"test run 'python3 -m unittest discover -s tests' is supplied but cites no capture")
        self.assert_error(self.errs(lambda b: b["test_runs"][0].update(status="supplied", source_id="K1")),
                          r"K1 is not a real captured command log")


class FunctionalQA(DiscoveryCase):
    scenario, analysis = "capability-cli-qa", "analysis-functional"

    def test_renders_contract_outcomes_from_real_captures_and_changes_nothing(self):
        before = tree_digest(EVALS / self.scenario)
        out = self.render(self.a)
        self.assertEqual(tree_digest(EVALS / self.scenario), before)
        rep = (out / "qa-report.md").read_text()
        self.assertNotIn("SYNTHETIC", rep)
        self.assertIn("Functional contracts: 4 pass, 0 fail, 0 blocked, 3 not run, 0 inconclusive, 4 not applicable", rep)
        self.assertIn("No health score: functional coverage is reported as contract outcomes", rep)
        self.assertIn("Browser surface: not run", rep)
        self.assertIn("`p-dup-keys` invalid-input: pass (captured exit 2, expected 2; c-dup-keys)", section(rep, "Charter"))
        self.assertIn("`p-race` concurrency: not run", section(rep, "Charter"))
        self.assertIn("QA found 0 issues; fixed 0 (audit only)", rep)
        self.assertIn("f-race", section(rep, "Suppressed findings"))
        charter = cr.load_json(out / "qa-charter.json")
        self.assertEqual(charter["captures"]["c-success"]["exit"], 0)
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any("not run" in x for x in result["limitations"]))

    def test_outcomes_must_match_the_captured_exit_and_command(self):
        self.assert_error(self.errs(lambda b: probe(b, "p-dup-keys").update(expect_exit=1)),
                          r"p-dup-keys: captured exit 2, expected 1: the outcome is fail, not pass")
        self.assert_error(self.errs(lambda b: probe(b, "p-success").update(probe="python3 scripts/capability_workflow.py validate --analysis x.json")),
                          r"p-success: capture c-success ran a different command than the probe")
        self.assert_error(self.errs(lambda b: probe(b, "p-overwrite").update(outcome="pass")),
                          r"p-overwrite is pass but cites no capture")
        self.assert_error(self.errs(lambda b: probe(b, "p-race").pop("reason")), r"p-race is not-run without a reason")

    def test_only_real_tagged_captures_count(self):
        b, pack = repack(self.tmp, EVALS / self.scenario, self.a, lambda d: d["items"][0]["topics"].__setitem__(0, "notes"))
        self.assert_error(errors(b, pack, self.store), r"c-success is not a command capture")
        b, pack = repack(self.tmp, EVALS / self.scenario, self.a, lambda d: d["items"][2].pop("captured_file") and d["items"][2].pop("digest"))
        self.assert_error(errors(b, pack, self.store), r"c-dup-keys is not a command capture")
        self.assert_error(self.errs(lambda b: b["charter"].append(dict(probe(b, "p-success"), id="p-browser", surface="browser"))),
                          r"p-browser: browser outcomes need a supplied PNG or JPEG screenshot; c-success is not one")

    def test_failed_probe_needs_a_reproduced_bug_and_bugs_need_failed_probes(self):
        # A test-only copy of the pack whose success capture records exit 1 (not a shipped capture).
        dst = self.tmp / "pack-fail"
        shutil.copytree(EVALS / self.scenario, dst, ignore=shutil.ignore_patterns("inputs"))
        cap = dst / "captured" / "c-success.txt"
        cap.write_text(cap.read_text().replace("exit=0", "exit=1"))
        doc = cr.load_json(dst / "evidence-pack.json")
        doc["items"][0]["digest"] = hashlib.sha256(cap.read_bytes()).hexdigest()
        raw = (json.dumps(doc, indent=2) + "\n").encode()
        (dst / "evidence-pack.json").write_bytes(raw)
        pack, errs = wf.load_pack(dst / "evidence-pack.json")
        self.assertEqual(errs, [])
        b = mutate(self.a, lambda x: x["evidence"].update(digest=hashlib.sha256(raw).hexdigest()))
        self.assert_error(errors(b, pack, self.store), r"p-success: captured exit 1, expected 0: the outcome is fail, not pass")
        b2 = mutate(b, lambda x: probe(x, "p-success").update(outcome="fail"))
        self.assert_error(errors(b2, pack, self.store), r"p-success failed but no bug reproduces it")
        bug = {"id": "b-1", "title": "validate rejects a passing analysis", "severity": "high", "category": "functional", "charter_ids": ["p-success"],
               "repro_steps": ["Run the p-success probe from the repository root."], "expected": "exit 0", "actual": "exit 1"}
        b3 = mutate(b2, lambda x: x["bugs"].append(bug))
        self.assertEqual(errors(b3, pack, self.store), [])
        self.assert_error(errors(mutate(b3, lambda x: x["bugs"][0].update(charter_ids=["p-dup-keys"])), pack, self.store),
                          r"bug b-1 cites p-dup-keys, which did not fail")

    def test_conflicting_captures_of_one_probe_are_rejected(self):
        # Review 2026-11-05: only the first capture decided the outcome; a second capture of the
        # same command with another exit status was ignored.
        dst = self.tmp / "pack-conflict"
        shutil.copytree(EVALS / self.scenario, dst, ignore=shutil.ignore_patterns("inputs"))
        src = dst / "captured" / "c-success.txt"
        alt = dst / "captured" / "c-success-2.txt"
        alt.write_text(src.read_text().replace("exit=0", "exit=1"))
        doc = cr.load_json(dst / "evidence-pack.json")
        item = copy.deepcopy(doc["items"][0])
        item.update(source_id="c-success-2", captured_file="captured/c-success-2.txt",
                    digest=hashlib.sha256(alt.read_bytes()).hexdigest())
        doc["items"].append(item)
        raw = (json.dumps(doc, indent=2) + "\n").encode()
        (dst / "evidence-pack.json").write_bytes(raw)
        pack, errs = wf.load_pack(dst / "evidence-pack.json")
        self.assertEqual(errs, [])
        b = mutate(self.a, lambda x: (x["evidence"].update(digest=hashlib.sha256(raw).hexdigest()),
                                      probe(x, "p-success")["capture_ids"].append("c-success-2")))
        self.assert_error(errors(b, pack, self.store), r"p-success: captures disagree")

    def test_result_reports_capture_outcomes_as_unverified_without_a_receipt(self):
        result = cr.load_json(self.render(self.a) / "result.json")
        self.assertIsNone(result["execution_receipt_ref"])
        self.assertTrue(any("no trusted receipt" in x.lower() for x in result["limitations"]), result["limitations"])

    def test_modes_scale_the_required_charter(self):
        self.assert_error(self.errs(lambda b: b.update(charter=[p for p in b["charter"] if p["contract"] != "auth"])),
                          r"full QA covers every contract \(pass, fail, blocked, not run or not applicable\): missing auth")
        quick = mutate(self.a, lambda b: b.update(qa_mode="quick", charter=[probe(b, "p-success"), probe(b, "p-dup-keys")]))
        self.assertEqual(errors(quick, self.pack, self.store), [])
        self.assert_error(errors(mutate(quick, lambda b: b.update(charter=[probe(b, "p-dup-keys")])), self.pack, self.store),
                          r"quick QA still checks success")
        self.assert_error(self.errs(lambda b: b.update(qa_mode="regression")), r"regression QA needs a baseline qa-report artifact")

    def test_captured_probes_still_describe_the_code(self):
        """Replay: re-run each captured probe from the repository root and compare the exit status."""
        for p in self.a["charter"]:
            if p["outcome"] not in ("pass", "fail"):
                continue
            with self.subTest(probe=p["id"]):
                env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "TMPDIR": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
                argv = p["probe"].split()
                self.assertEqual(argv[0], "python3")
                run = subprocess.run([sys.executable] + argv[1:], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
                cap = cm.parse_capture((EVALS / self.scenario / "captured" / f"{p['capture_ids'][0]}.txt").read_text())
                self.assertEqual(run.returncode, cap["exit"], run.stderr[-400:])


if __name__ == "__main__":
    import unittest
    unittest.main()
