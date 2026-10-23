"""Offline tests for the safer plan UX: pending states in dry-run and the explicit
bulk approval of classifier proposals (``APPROVE_ALL``) in knowledge-apply.

Same synthetic fixture as test_knowledge_apply (read-only library, temp seeds,
canonical store, stub hermes): nothing touches /mnt/books, hermes/profiles,
~/.hermes, a model or a real plan.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_knowledge_apply as tka  # noqa: E402

kp = tka.kp


def incomplete_spec(name="cloud-security", sources=("edu/Cloud Security",), **over):
    """What classify wrote before specs were complete: purpose only."""
    spec = {"name": name, "approved": False, "purpose": "Cloud security specialist.", "in_scope": [],
            "out_of_scope": [], "parent": "", "overlap": [], "boundaries": [], "sources": list(sources),
            "note": "proposed by the classifier, not created."}
    spec.update(over)
    return spec


def complete_spec(name="cloud-security", sources=("edu/Cloud Security",), **over):
    spec = tka.new_profile_spec(name=name, approved=False, sources=list(sources),
                                spec_status="complete", purpose="Cloud security: posture, IAM, logging.")
    spec.update(over)
    return spec


class PlanStatesTest(tka.ApplyFixture):
    """Dry-run on an unedited classifier plan: validation and reporting, never a wall of errors."""

    def unedited_plan(self, spec=None, **extra):
        spec = incomplete_spec() if spec is None else spec
        items = [
            self.item("edu/Strategy Book"),                                                    # clean -> assign
            self.item("edu/Two Files", decision="review", status="needs-review", profile="beta",
                      proposed_profile="beta", review_reasons=["high-risk-content:medical"]),   # existing proposal
            self.item("edu/Cloud Security", decision="review", status="needs-review", profile=spec["name"],
                      proposed_profile="", review_reasons=["no-profile", f"new-profile-proposal:{spec['name']}"],
                      new_profile_suggestion={"name": spec["name"], "purpose": spec["purpose"]}),
            self.item("edu/Amazon Selling Guide", decision="review", status="error", profile="",
                      proposed_profile="", review_reasons=["classifier-error"]),                # no target
            self.item("edu/Copy Of Strategy", decision="existing", status="already-imported", profile="",
                      proposed_profile="", owners=["alpha"]),
        ]
        return self.plan(items, [spec], **extra)

    def test_unedited_plan_dry_run_reports_states_exit_zero_and_writes_nothing(self):
        p = self.run_apply(self.unedited_plan(), "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stderr.strip(), "", p.stderr)
        out = p.stdout
        self.assertIn("ready 1", out)
        self.assertIn("pending review 1", out)
        self.assertIn("pending profile approval 1", out)
        self.assertIn("pending error 1", out)
        self.assertIn("existing 1", out)
        self.assertIn("edu/Strategy Book", out)
        self.assertIn("cloud-security", out)
        self.assertIn("predates complete specs", out)        # legacy proposal: regenerate, not invalid
        self.assertIn("knowledge-classify", out)
        self.assertNotIn("invalid", out.lower().replace("not invalid", ""))
        self.assertIn("APPROVE_ALL=1", out)                  # exact next command
        self.assertNotIn("in_scope", out)                    # pending fields only with VERBOSE
        self.assert_nothing_written()
        self.assertFalse((self.canon / "apply").exists())

    def test_verbose_dry_run_lists_pending_fields(self):
        p = self.run_apply(self.unedited_plan(), "--dry-run", "--verbose")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("in_scope", p.stdout)
        self.assertIn("high-risk-content:medical", p.stdout)
        self.assert_nothing_written()

    def test_actual_apply_with_pending_items_refuses_before_any_write(self):
        p = self.run_apply(self.unedited_plan(), "--approve")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("pending", p.stderr)
        self.assertIn("APPROVE_ALL", p.stderr)
        self.assert_nothing_written()

    def test_real_errors_are_still_errors(self):
        plan = self.unedited_plan()
        data = json.loads(plan.read_text())
        data["items"][0]["source_sha256"] = "0" * 64                     # stale hash on a ready item
        plan.write_text(json.dumps(data))
        p = self.run_apply(plan, "--dry-run")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("stale", p.stderr.lower())
        self.assert_nothing_written()

    def test_approve_all_preview_shows_what_changes_and_writes_nothing(self):
        p = self.run_apply(self.unedited_plan(complete_spec()), "--dry-run", "--approve-all")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout
        self.assertIn("ready 3", out)                        # strategy + beta proposal + new profile
        self.assertIn("pending error 1", out)
        self.assertIn("create profile cloud-security", out)
        self.assertIn("beta <- edu/Two Files", out)
        self.assertIn("SKIP_ERRORS=1", out)
        self.assertRegex(out, r"APPROVE_ALL=[0-9a-f]{16}")  # bound to the plan hash
        self.assert_nothing_written()
        self.assertFalse((self.seeds / "cloud-security").exists())

    def test_approve_all_never_guesses_incomplete_specs_or_errors(self):
        p = self.run_apply(self.unedited_plan(incomplete_spec()), "--approve", "--approve-all")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("pending", p.stderr)
        self.assertFalse((self.seeds / "cloud-security").exists())
        self.assert_nothing_written()
        p = self.run_apply(self.unedited_plan(complete_spec(), name="p2"), "--approve", "--approve-all")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("SKIP_ERRORS", p.stderr)
        self.assert_nothing_written()


class BulkApprovalApplyTest(tka.ApplyFixture):
    def plan_all(self, **extra):
        spec = complete_spec()
        items = [
            self.item("edu/Strategy Book"),
            self.item("edu/Two Files", decision="review", status="needs-review", profile="beta",
                      proposed_profile="beta", review_reasons=["high-risk-content:medical", "high-risk-profile:beta"]),
            self.item("edu/Cloud Security", decision="review", status="needs-review", profile="cloud-security",
                      proposed_profile="", review_reasons=["no-profile", "new-profile-proposal:cloud-security"]),
            self.item("edu/Amazon Selling Guide", decision="review", status="error", profile="",
                      proposed_profile="", review_reasons=["classifier-error"]),
        ]
        return self.plan(items, [spec], **extra)

    def expected_digest(self, plan, skip_errors):
        return kp.approval_digest([("edu/Amazon Selling Guide", "skip" if skip_errors else "review", ""),
                                                        ("edu/Cloud Security", "assign", "cloud-security"),
                                                        ("edu/Strategy Book", "assign", "alpha"),
                                                        ("edu/Two Files", "assign", "beta")],
                                 ["cloud-security"], {"approve_all": True, "skip_errors": skip_errors})

    def test_approve_all_with_skip_errors_applies_everything_and_writes_a_receipt(self):
        plan = self.plan_all()
        before = plan.read_bytes()
        p = self.run_apply(plan, "--approve", "--approve-all", "--skip-errors", "--no-learn")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(plan.read_bytes(), before, "the plan file was edited in place")
        self.assertIn("edu/Strategy Book", self.manifest("alpha"))
        self.assertIn("edu/Two Files", self.manifest("beta"))                      # high-risk accepted explicitly
        self.assertIn("edu/Cloud Security", self.manifest("cloud-security"))     # proposal created
        self.assertNotIn("Amazon", self.manifest("alpha") + self.manifest("beta"))
        soul = (self.seeds / "cloud-security" / "SOUL.md").read_text()
        self.assertIn("Cloud security: posture, IAM, logging.", soul)
        spec_written = self.assignments("cloud-security")["profile_spec"]
        self.assertEqual(spec_written["parent"], "business")
        run_dir = next((self.canon / "apply").glob("*"))
        receipt = json.loads((run_dir / "approval.json").read_text())
        plan_id = kp.load_plan(plan)["_plan_id"]
        self.assertEqual(receipt["plan_id"], plan_id)
        self.assertEqual(receipt["plan_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(receipt["policy"], {"approve_all": True, "skip_errors": True})
        self.assertEqual(receipt["approval_digest"], self.expected_digest(plan, True))
        self.assertEqual(receipt["approved_profiles"], ["cloud-security"])
        self.assertEqual(sorted(c["source"] for c in receipt["changes"]),
                         ["edu/Amazon Selling Guide", "edu/Cloud Security", "edu/Two Files"])
        self.assertEqual({c["source"]: c["to"] for c in receipt["changes"]}["edu/Amazon Selling Guide"], "skip")
        report = json.loads((run_dir / "report.json").read_text())
        self.assertEqual(report["approval_digest"], receipt["approval_digest"])
        self.assertIn("edu/Amazon Selling Guide", report["skipped"])
        self.assertIn("approval", report)
        self.assertEqual(len(list((self.kroot / "beta" / "refs").glob("*.ref.md"))), 2)
        self.assert_library_unchanged()
        # idempotent: same command again changes nothing and keeps one receipt
        p2 = self.run_apply(plan, "--approve", "--approve-all", "--skip-errors", "--no-learn")
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertEqual(self.manifest("beta").count("edu/Two Files"), 1)
        self.assertEqual(self.manifest("cloud-security").count("edu/Cloud Security"), 1)
        self.assertEqual(json.loads((run_dir / "approval.json").read_text())["approval_digest"],
                         receipt["approval_digest"])
        self.assertEqual(len(list((self.canon / "apply").glob("*/approval.json"))), 1)

    def test_approve_all_bound_to_plan_hash_refuses_a_changed_plan(self):
        plan = self.plan_all()
        plan_id = kp.load_plan(plan)["_plan_id"]
        p = self.run_apply(plan, "--approve", "--approve-all", "0123456789abcdef", "--skip-errors", "--no-learn")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("plan id", p.stderr)
        self.assert_nothing_written()
        data = json.loads(plan.read_text())
        data["items"][0]["note"] = "edited after the preview"
        plan.write_text(json.dumps(data, indent=2) + "\n")
        p = self.run_apply(plan, "--approve", "--approve-all", plan_id, "--skip-errors", "--no-learn")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(plan_id, p.stderr)
        self.assert_nothing_written()
        new_id = kp.load_plan(plan)["_plan_id"]
        p = self.run_apply(plan, "--approve", "--approve-all", new_id, "--skip-errors", "--no-learn")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_approve_all_keeps_hand_edits_and_terminal_decisions(self):
        items = [self.item("edu/Strategy Book", decision="skip", profile="",
                           proposed_profile="alpha", review_reasons=["low-confidence"], status="needs-review"),
                 self.item("edu/Two Files", decision="review", status="needs-review", profile="",
                           proposed_profile="alpha", review_reasons=["low-confidence"])]
        p = self.run_apply(self.plan(items), "--approve", "--approve-all", "--no-learn")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Strategy", self.manifest("alpha"))       # skip stays skip
        self.assertIn("edu/Two Files", self.manifest("alpha"))      # proposal accepted

    def test_resolve_plan_semantics(self):
        plan = kp.load_plan(self.plan_all())
        profiles = kp.existing_profiles(self.seeds)
        plain = kp.resolve_plan(plan, profiles)
        self.assertEqual(plain.counts["ready"], 1)
        self.assertEqual(plain.counts["pending-review"], 1)
        self.assertEqual(plain.counts["pending-profile-approval"], 1)
        self.assertEqual(plain.counts["pending-error"], 1)
        self.assertFalse(plain.changes)
        res = kp.resolve_plan(plan, profiles, approve_all=True)
        self.assertEqual(res.counts["ready"], 3)
        self.assertEqual(res.counts["pending-error"], 1)
        self.assertEqual(res.approved_profiles, ["cloud-security"])
        self.assertTrue(res.pending)
        res2 = kp.resolve_plan(plan, profiles, approve_all=True, skip_errors=True)
        self.assertFalse(res2.pending)
        self.assertEqual(res2.counts["skip"], 1)
        self.assertEqual(res2.digest, self.expected_digest(self.plan_all(name="again"), True))
        self.assertEqual(plan["items"][1]["decision"], "review", "resolve_plan mutated the loaded plan")


if __name__ == "__main__":
    unittest.main()
