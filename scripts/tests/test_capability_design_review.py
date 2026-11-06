"""DES-01 (review half): adapted gstack design-review audit over actual supplied artifacts.

queue-page-review audits a supplied synthetic copy of an existing page (ui-evidence);
trial-guide-review audits the design package the consultation rendered. The
validator re-reads every reviewed artifact by digest and runs static checks;
findings must cite failing checks, verbatim excerpts or supplied captures.
Browser, screenshot, assistive-technology and usability results are never
inferred: without captured evidence they are reported as not run.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import struct
import sys
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_design_consultation import ratio  # noqa: E402
from test_capability_discovery import DiscoveryCase, ctx, errors, load, section  # noqa: E402
from workflow_helpers import EVALS, REG, REPO, TempDirCase, cr, mutate, wf  # noqa: E402

PAGE = "t-queue-review.current-queue-page"


def finding(a, fid):
    return next(f for f in a["findings"] if f["id"] == fid)


def tiny_png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))


class QueuePageReview(DiscoveryCase):
    scenario, analysis = "queue-page-review", "analysis-audit"

    def checks(self):
        c = ctx(self.pack, self.store)
        return wf.design_static_checks(self.a, c)

    def test_static_checks_inspect_the_supplied_page(self):
        res = self.checks()[PAGE]
        failing = {k for k, v in res.items() if v["status"] == "fail"}
        for cid in ("doc-lang", "meta-viewport", "landmark-main", "skip-link", "heading-order", "img-alt", "form-label",
                    "control-name", "link-target", "focus-visible", "tabindex-positive", "inline-handler",
                    "external-resource", "reduced-motion", "copy-slop", "css-contrast", "css-slop"):
            self.assertIn(cid, failing)
        for cid in ("doc-title", "duplicate-id", "html-structure"):
            self.assertEqual(res[cid]["status"], "pass", cid)
        self.assertEqual(res["script"]["status"], "not-evaluated")
        for cid in ("target-size", "keyboard-order", "screen-reader-output", "visual-hierarchy", "reflow-320"):
            self.assertEqual(res[cid]["status"], "not-evaluated", cid)
        want = f"{ratio('#9aa0a6', '#ffffff'):.2f}:1"
        self.assertTrue(any(want in d for d in res["css-contrast"]["details"]), res["css-contrast"]["details"])
        self.assertTrue(any("https://cdn.example.com/chat-widget.js" in d for d in res["external-resource"]["details"]))

    def test_grade_wording_matches_the_arithmetic_and_names_what_the_score_covers(self):
        # Final review 2026-11-05: the report said "a medium half a letter", but letters are whole
        # (3.5 and 3.0 points both read B), and accessibility is graded outside the weighted score.
        self.assertEqual([wf._grade(p) for p in (4.0, 3.5, 3.0, 2.5, 0.5)], ["A", "B", "B", "C", "F"])
        grades = section((self.render(self.a) / "design-review-report.md").read_text(), "Grades")
        self.assertNotIn("half a letter", grades)
        self.assertIn("a high finding subtracts 1 point and a medium 0.5", grades)
        self.assertIn("rounded down to a whole letter", grades)
        self.assertIn("accessibility is graded but is not part of the weighted design score", grades)
        skill = (REPO / "hermes/skills/gstack-design-review/SKILL.md").read_text()
        self.assertNotIn("half a letter", skill)

    def test_report_separates_static_findings_from_unverified_browser_evidence(self):
        out = self.render(self.a)
        rep = (out / "design-review-report.md").read_text()
        self.assertIn("SYNTHETIC fixture data", rep)
        self.assertIn("This review changed no source", rep)
        nv = section(rep, "Not verified")
        for what in ("Browser render and screenshots: not run", "Assistive technology: not run", "Usability test: not run"):
            self.assertIn(what, nv)
        grades = section(rep, "Grades")
        for cat in ("hierarchy", "performance"):
            self.assertRegex(grades, rf"{cat}[^\n]*not graded")
        self.assertIn("static evidence only", grades)
        self.assertRegex(grades, r"typography: not graded \(not assessed")  # nothing looked at it: silence is not an A
        self.assertRegex(grades, r"color-contrast[^\n]*\bB\b")  # starts at A; one high finding drops one letter
        for f in self.a["findings"]:
            self.assertIn(f"`{f['id']}`", rep)
        static = section(rep, "Static checks")
        self.assertRegex(static, r"`css-contrast`[^\n]*fail")
        self.assertRegex(static, r"`doc-title`[^\n]*pass")
        self.assertRegex(static, r"`external-resource`[^\n]*waived")
        self.assertIn("Quick wins", rep)
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any("no browser" in x.lower() for x in result["limitations"]))
        checks = cr.load_json(out / "static-checks.json")
        self.assertEqual(checks["artifacts"][PAGE]["digest"], self.a["reviewed_artifacts"][0]["digest"])
        index = cr.load_json(out / "artifacts.json")
        self.assertEqual(cr.verify_artifact_contents(index, out), [])
        rep_art = next(x for x in index["artifacts"] if x["location"] == "design-review-report.md")
        self.assertEqual(rep_art["kind"], "design-review-report")
        self.assertIn({"artifact_id": PAGE, "digest": self.a["reviewed_artifacts"][0]["digest"]}, rep_art["source_refs"])

    def test_findings_must_cite_failing_checks_or_verbatim_excerpts(self):
        bad = mutate(self.a, lambda b: finding(b, "f-structure")["evidence"].append(
            {"kind": "static-check", "artifact_id": PAGE, "check_id": "doc-title"}))
        self.assert_error(self.errs_of(bad), r"f-structure cites doc-title, which passed")
        bad = mutate(self.a, lambda b: finding(b, "f-structure")["evidence"].append(
            {"kind": "static-check", "artifact_id": PAGE, "check_id": "target-size"}))
        self.assert_error(self.errs_of(bad), r"f-structure cites target-size, which static checks cannot evaluate")
        bad = mutate(self.a, lambda b: finding(b, "f-copy")["evidence"].append(
            {"kind": "excerpt", "artifact_id": PAGE, "text": "Something went wrong, please retry"}))
        self.assert_error(self.errs_of(bad), r"f-copy quotes text that is not in")
        bad = mutate(self.a, lambda b: finding(b, "f-copy")["evidence"].append(
            {"kind": "static-check", "artifact_id": "other.page", "check_id": "copy-slop"}))
        self.assert_error(self.errs_of(bad), r"f-copy cites other.page, which is not a reviewed artifact")
        bad = mutate(self.a, lambda b: finding(b, "f-zoom").update(evidence=[{"kind": "statement", "statement_id": "q-scope"}]))
        self.assert_error(self.errs_of(bad), r"f-zoom is static but cites no failing check or excerpt")

    def test_every_failing_check_is_covered_or_waived(self):
        bad = mutate(self.a, lambda b: b.update(findings=[f for f in b["findings"] if f["id"] != "f-motion"]))
        self.assert_error(self.errs_of(bad), r"reduced-motion failed on t-queue-review.current-queue-page.*no finding")
        bad = mutate(self.a, lambda b: b.update(waived_checks=[]))
        self.assert_error(self.errs_of(bad), r"external-resource failed")
        bad = mutate(self.a, lambda b: b["waived_checks"].append({"artifact_id": PAGE, "check_id": "doc-title", "reason": "x"}))
        self.assert_error(self.errs_of(bad), r"waives doc-title, which did not fail")

    def test_no_invented_screenshot_accessibility_or_usability_results(self):
        bad = mutate(self.a, lambda b: finding(b, "f-contrast").update(verification="browser"))
        self.assert_error(self.errs_of(bad), r"f-contrast claims browser verification but cites no browser capture")
        bad = mutate(self.a, lambda b: finding(b, "f-links").update(category="hierarchy"))
        self.assert_error(self.errs_of(bad), r"f-links: hierarchy needs browser evidence")
        bad = mutate(self.a, lambda b: finding(b, "f-copy").update(
            because="In the usability session, agents could not find the reload action."))
        self.assert_error(self.errs_of(bad), r"f-copy reads as a usability, screen-reader or screenshot result")
        bad = mutate(self.a, lambda b: finding(b, "f-names").update(because="VoiceOver announced the button as unlabelled."))
        self.assert_error(self.errs_of(bad), r"f-names reads as")
        bad = mutate(self.a, lambda b: b.update(first_impression={"communicates": "a busy queue", "notice": "a gradient button",
                                                                 "eye_path": ["button", "badge", "heading"], "one_word": "loud",
                                                                 "source_id": "Q1"}))
        self.assert_error(self.errs_of(bad), r"first impression needs a supplied browser screenshot")
        bad = mutate(self.a, lambda b: b["evidence_coverage"].update(browser_render={"status": "supplied", "source_ids": ["Q1"]}))
        self.assert_error(self.errs_of(bad), r"Q1 is synthetic.*not browser evidence")
        bad = mutate(self.a, lambda b: b["evidence_coverage"].update(usability_test={"status": "not-run", "source_ids": ["Q1"]}))
        self.assert_error(self.errs_of(bad), r"usability_test is not-run but lists sources")

    def test_review_has_no_write_or_patch_path(self):
        page = EVALS / self.scenario / "inputs" / "current-queue-page.html"
        before = hashlib.sha256(page.read_bytes()).hexdigest()
        mtime = page.stat().st_mtime_ns
        out = self.render(self.a)
        self.assertEqual(hashlib.sha256(page.read_bytes()).hexdigest(), before)
        self.assertEqual(page.stat().st_mtime_ns, mtime)
        allowed = {"design-review-report.md", "static-checks.json", "analysis.json", "evidence-pack.json", "artifacts.json", "result.json"}
        self.assertEqual([p.name for p in out.iterdir() if p.name not in allowed and not p.name.startswith("input-")], [])
        for field in ("fixes", "patch", "apply"):
            self.assert_error(self.errs_of(mutate(self.a, lambda b, f=field: b.update({f: []}))), r"Additional properties")
        bad = mutate(self.a, lambda b: finding(b, "f-zoom").update(recommendation="--- a/page.html\n+++ b/page.html\n@@ -1 +1 @@"))
        self.assert_error(self.errs_of(bad), r"f-zoom recommendation is a patch")
        cap = next(c for c in REG["capabilities"] if c["id"] == "gstack.design-review")
        self.assertEqual(cap["execution"]["writer"], "none")
        self.assertNotIn("source-modification", cap["side_effects"])

    def test_changed_or_foreign_reviewed_artifact_is_refused(self):
        bad = mutate(self.a, lambda b: b["reviewed_artifacts"][0].update(digest="0" * 64))
        self.assert_error(self.errs_of(bad), r"reviewed artifact .*digest differs")
        store = copy.deepcopy(self.store)
        store.index["artifacts"][0]["company_id"] = "example-a"
        self.assert_error(errors(self.a, self.pack, store), r"belongs to another company/project")
        bad = mutate(self.a, lambda b: b.update(reviewed_artifacts=[]))
        self.assert_error(self.errs_of(bad), r"schema: reviewed_artifacts")

    def test_render_opens_no_network_connection(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")):
            self.render(self.a)

    def test_cli_validates_and_refuses_an_unsupported_finding(self):
        d = EVALS / self.scenario
        args = ["--evidence", d / "evidence-pack.json", "--artifacts", d / "inputs/artifacts.json"]
        p = self.run_cli(REPO / "scripts/capability_workflow.py", "validate", "--analysis", d / "analysis-audit.json", *args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        bad = mutate(self.a, lambda b: finding(b, "f-zoom").update(verification="usability-test"))
        (self.tmp / "bad.json").write_text(json.dumps(bad))
        p = self.run_cli(REPO / "scripts/capability_workflow.py", "render", "--analysis", self.tmp / "bad.json", *args,
                         "--out", self.tmp / "never")
        self.assertEqual(p.returncode, 1)
        self.assertIn("usability test verification but cites no usability capture", p.stdout)
        self.assertFalse((self.tmp / "never").exists())

    def errs_of(self, a):
        return errors(a, self.pack, self.store)


class DesignPlannerSeam(TempDirCase):
    def test_consultation_reuses_the_accepted_prd_and_review_follows_it_read_only(self):
        from test_capability_plan import artifact, index, plan, selection, stage, stage_ids
        prd = artifact("a-prd", "lean-prd", "prd body")
        p = plan(selection("gstack.design-review", "gstack.design-consultation"), arts=index(prd))
        self.assertEqual(stage_ids(p), ["gstack.design-consultation", "gstack.design-review"])
        product = next(i for i in stage(p, "gstack.design-consultation")["inputs"] if i["name"] == "product")
        self.assertEqual(product["satisfied_by"]["artifact_id"], "a-prd")
        self.assertEqual(stage(p, "gstack.design-review")["after"], ["gstack.design-consultation"])
        for cid in ("gstack.design-consultation", "gstack.design-review"):
            self.assertEqual(stage(p, cid)["execution"]["writer"], "none")
            self.assertNotIn("source-modification", stage(p, cid)["side_effects"])


class ConsultationOutputReview(DiscoveryCase):
    scenario, analysis = "trial-guide-review", "analysis-audit"

    def test_static_checks_of_the_rendered_preview_and_tokens_pass(self):
        res = wf.design_static_checks(self.a, ctx(self.pack, self.store))
        prev = next(k for k in res if k.endswith("design-preview"))
        toks = next(k for k in res if k.endswith("design-tokens"))
        self.assertEqual([k for k, v in res[prev].items() if v["status"] == "fail"], [])
        self.assertEqual([k for k, v in res[toks].items() if v["status"] == "fail"], [])
        self.assertIn("contrast:light:text-muted-on-surface", res[toks])
        out = self.render(self.a)
        rep = (out / "design-review-report.md").read_text()
        self.assertRegex(section(rep, "Static checks"), r"0 failed")
        self.assertIn("Browser render and screenshots: not run", rep)

    def test_tampered_preview_is_detected_when_reviewed(self):
        d = self.tmp / "inputs"
        src = EVALS / self.scenario / "inputs"
        d.mkdir()
        for p in src.iterdir():
            (d / p.name).write_bytes(p.read_bytes())
        prev = next(p for p in d.iterdir() if p.name.endswith("design-preview.html"))
        prev.write_text(prev.read_text().replace("</body>", '<script src="https://x.example/a.js"></script></body>'))
        store, errs = wf.load_artifacts(d / "artifacts.json", d)
        self.assertIsNone(store)
        self.assert_error(errs, r"content digest differs")


class SuppliedBrowserCapture(TempDirCase):
    """A public (non-synthetic) task with a supplied screenshot file: accepted as coordinator-supplied
    capture evidence (digest and image type verified), never as something this tool rendered."""

    def setUp(self):
        super().setUp()
        base, pack, store = load("queue-page-review", "analysis-audit")
        page = (EVALS / "queue-page-review" / "inputs" / "current-queue-page.html").read_bytes()
        d = self.tmp / "case"
        (d / "inputs").mkdir(parents=True)
        (d / "inputs" / "page.html").write_bytes(page)
        (d / "shot.png").write_bytes(tiny_png())
        b = {"task_id": "t-public-review", "company_id": "example-b", "project_id": "example-ops", "data_classification": "public"}
        idx = copy.deepcopy(store.index)
        art = idx["artifacts"][0]
        art.update(artifact_id="t-public-review.page", task_id="t-public-review", data_classification="public", location="page.html")
        (d / "inputs" / "artifacts.json").write_text(json.dumps(idx))
        pk = copy.deepcopy(pack.doc)
        pk.update(pack_id="pack-public-review", binding=b, not_available=[])
        pk["items"] = [{"source_id": "S1", "kind": "artifact", "classification": "real", "title": "Desktop screenshot of the page",
                        "ref": "capture:public-review/desktop-1440", "topics": ["screenshot"], "excerpt": "Screenshot at 1440 by 900.",
                        "captured_file": "shot.png", "digest": hashlib.sha256(tiny_png()).hexdigest(),
                        "retrieved_at": "2026-11-04T12:00:00Z"},
                       {"source_id": "S2", "kind": "document", "classification": "real", "title": "Review request",
                        "ref": "doc:public-review/request", "topics": ["review-scope"], "excerpt": "Audit the queue page."}]
        (d / "evidence-pack.json").write_text(json.dumps(pk))
        self.pack, errs = wf.load_pack(d / "evidence-pack.json")
        self.assertEqual(errs, [])
        self.store, errs = wf.load_artifacts(d / "inputs" / "artifacts.json", d / "inputs")
        self.assertEqual(errs, [])
        a = copy.deepcopy(base)
        a["binding"] = b
        a["evidence"] = {"pack_id": "pack-public-review", "digest": self.pack.digest}
        a["reviewed_artifacts"] = [{"artifact_id": "t-public-review.page", "kind": "ui-evidence", "digest": art["content_digest"]}]
        for f in a["findings"]:
            for e in f["evidence"]:
                if "artifact_id" in e:
                    e["artifact_id"] = "t-public-review.page"
        a["waived_checks"][0]["artifact_id"] = "t-public-review.page"
        a["statements"] = [{"id": "q-scope", "text": "The requester asked for an audit of the queue page.", "class": "observed", "source_ids": ["S2"]}]
        a["evidence_coverage"]["browser_render"] = {"status": "supplied", "source_ids": ["S1"]}
        a["first_impression"] = {"communicates": "a busy ticket list", "notice": "the gradient button outweighs the tickets",
                                 "eye_path": ["gradient button", "late badge", "page heading"], "one_word": "loud", "source_id": "S1"}
        a["findings"].append({"id": "f-hier", "title": "The promotional button outweighs the ticket list", "category": "hierarchy",
                              "impact": "medium", "notice": "In the screenshot, the gradient button is the first thing the eye reaches.",
                              "because": "Agents come to the page for the oldest ticket.", "recommendation": "Demote the button below the list.",
                              "verification": "browser", "evidence": [{"kind": "capture", "source_id": "S1"}]})
        self.a = a

    def test_supplied_capture_backs_browser_findings_and_is_labeled_as_supplied(self):
        self.assertEqual(errors(self.a, self.pack, self.store), [])
        c = ctx(self.pack, self.store)
        c.extra["pack_raw"] = (self.tmp / "case" / "evidence-pack.json").read_bytes()
        files, errs = wf.render_analysis(self.a, c)
        self.assertEqual(errs, [])
        rep = files["design-review-report.md"]
        self.assertIn("Browser render and screenshots: supplied by the coordinator (S1", rep)
        self.assertIn("not rendered or viewed by this tool", rep)
        self.assertRegex(section(rep, "Grades"), r"hierarchy[^\n]*\bB\b")
        self.assertIn("## First impression", rep)

    def test_capture_must_be_an_image_file_with_the_right_topic(self):
        (self.tmp / "case" / "shot.png").unlink()
        (self.tmp / "case" / "shot.png").write_bytes(b"not an image")
        pk = json.loads((self.tmp / "case" / "evidence-pack.json").read_text())
        pk["items"][0]["digest"] = hashlib.sha256(b"not an image").hexdigest()
        (self.tmp / "case" / "evidence-pack.json").write_text(json.dumps(pk))
        pack, errs = wf.load_pack(self.tmp / "case" / "evidence-pack.json")
        self.assertEqual(errs, [])
        a = mutate(self.a, lambda b: b["evidence"].update(digest=pack.digest))
        self.assert_error(errors(a, pack, self.store), r"S1 is not a PNG or JPEG image")
        a = mutate(self.a, lambda b: b["evidence_coverage"].update(assistive_tech={"status": "supplied", "source_ids": ["S1"]}))
        self.assert_error(errors(a, self.pack, self.store), r"S1 is not tagged accessibility-scan")

    def _with_capture(self, sid, topic, body=b"VoiceOver: button, Promote. Usability notes."):
        d = self.tmp / "case"
        (d / f"{sid}.txt").write_bytes(body)
        pk = json.loads((d / "evidence-pack.json").read_text())
        pk["items"].append({"source_id": sid, "kind": "artifact", "classification": "real", "title": f"{topic} capture",
                            "ref": f"capture:public-review/{sid}", "topics": [topic], "excerpt": "Supplied capture.",
                            "captured_file": f"{sid}.txt", "digest": hashlib.sha256(body).hexdigest(),
                            "retrieved_at": "2026-11-04T12:00:00Z"})
        (d / "evidence-pack.json").write_text(json.dumps(pk))
        pack, errs = wf.load_pack(d / "evidence-pack.json")
        self.assertEqual(errs, [])
        return pack

    def test_screenshot_does_not_back_a_screen_reader_or_usability_result(self):
        # review 2026-11-05 (wor3): any supplied capture satisfied the research-claim gate
        claims = {"VoiceOver announced the button as unlabelled.": r"assistive-technology result \('VoiceOver announced'\)",
                  "In the screenshot, TalkBack reads the button twice.": r"assistive-technology result \('TalkBack reads'\)",
                  "Lighthouse flagged the button.": r"assistive-technology result",
                  "In the usability session, agents could not find the oldest ticket.": r"usability or user-observation result",
                  "Users told us the button is confusing.": r"usability or user-observation result"}
        for text, pat in claims.items():
            errs = errors(mutate(self.a, lambda b, t=text: finding(b, "f-hier").update(because=t)), self.pack, self.store)
            self.assert_error(errs, rf"f-hier reads as an? {pat}.* cites no supplied (assistive-technology|usability) capture")
        # a screenshot claim stays backed by the screenshot
        self.assertEqual(errors(mutate(self.a, lambda b: finding(b, "f-hier").update(
            because="In the screenshot, the button sits above the list.")), self.pack, self.store), [])

    def test_matching_capture_backs_the_claim(self):
        pack = self._with_capture("S3", "accessibility-scan")
        pack = self._with_capture("S4", "usability-session")

        def fn(b):
            b["evidence"]["digest"] = pack.digest
            b["evidence_coverage"]["assistive_tech"] = {"status": "supplied", "source_ids": ["S3"]}
            b["evidence_coverage"]["usability_test"] = {"status": "supplied", "source_ids": ["S4"]}
        base = mutate(self.a, fn)
        at = mutate(base, lambda b: finding(b, "f-hier").update(because="VoiceOver announced the button as unlabelled."))
        self.assert_error(errors(at, pack, self.store), r"assistive-technology result .* cites no supplied assistive-technology capture")
        at = mutate(at, lambda b: finding(b, "f-hier")["evidence"].append({"kind": "capture", "source_id": "S3"}))
        self.assertEqual(errors(at, pack, self.store), [])
        us = mutate(base, lambda b: finding(b, "f-hier").update(because="In the usability session, agents could not find it."))
        us = mutate(us, lambda b: finding(b, "f-hier")["evidence"].append({"kind": "capture", "source_id": "S3"}))
        self.assert_error(errors(us, pack, self.store), r"usability or user-observation result .* cites no supplied usability capture")
        us = mutate(us, lambda b: finding(b, "f-hier")["evidence"].append({"kind": "capture", "source_id": "S4"}))
        self.assertEqual(errors(us, pack, self.store), [])


class StaticCheckFalsePassTest(TempDirCase):
    """Review 2026-11-05: inline style attributes, later srcset candidates and a no-preference
    motion query slipped past the static checks as 'pass'."""

    HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>t</title>'
            '<meta name="viewport" content="width=device-width"><style>{css}</style></head>'
            '<body><a href="#m">Skip</a><main id="m"><h1>x</h1>{body}</main></body></html>')

    def audit(self, body="", css=""):
        return wf.cd.audit_html(self.HEAD.format(css=css, body=body))

    def test_inline_style_remote_url_and_removed_outline_fail(self):
        r = self.audit('<div style="background:url(https://evil.example/a.png)"></div><button style="outline:none">Go</button>')
        self.assertEqual(r["external-resource"]["status"], "fail")
        self.assertEqual(r["focus-visible"]["status"], "fail")

    def test_every_srcset_candidate_is_checked(self):
        r = self.audit('<img alt="a" src="a.png" srcset="a.png 1x, https://x.example/b.png 2x">')
        self.assertEqual(r["external-resource"]["status"], "fail")

    def test_no_preference_query_is_not_a_reduced_motion_rule(self):
        css = ".x { transition: opacity 200ms; } @media (prefers-reduced-motion: no-preference) { .x { transition: opacity 400ms; } }"
        self.assertEqual(self.audit(css=css)["reduced-motion"]["status"], "fail")
        ok = ".x { transition: opacity 200ms; } @media (prefers-reduced-motion: reduce) { .x { transition: none; } }"
        self.assertEqual(self.audit(css=ok)["reduced-motion"]["status"], "pass")


class MalformedTokensTest(TempDirCase):
    """Review 2026-11-05 (wor8): a malformed design-tokens.v1 file crashed audit_tokens with TypeError/AttributeError."""

    BAD = [{"spacing_px": [4, "8"]}, {"spacing_px": "4,8"}, {"spacing_px": [4, True, 8]}, {"themes": ["light"]},
           {"themes": {"light": ["#fff"]}}, {"radius_px": [4, 8]}, {"radius_px": {"small": "4", "medium": 8, "large": 12}},
           {"type_scale_px": "16"}, {"type_scale_px": [16, "20", 24, 30, 36, 44]}, {"motion": [100]},
           {"type": ["roles"]}, {"type": {"roles": "display"}}, {"type": {"roles": ["display", 3, None]}}]

    def test_malformed_tokens_report_failures_instead_of_raising(self):
        for extra in self.BAD:
            with self.subTest(doc=extra):
                out = wf.cd.audit_tokens({"schema": "design-tokens.v1", **extra})
                self.assertIsInstance(out, dict)
                self.assertTrue(any(r["status"] == "fail" for r in out.values()), out)

    def test_cli_reports_a_malformed_reviewed_tokens_file_without_a_traceback(self):
        base, pack, store = load("queue-page-review", "analysis-audit")
        d = self.tmp / "case"
        (d / "inputs").mkdir(parents=True)
        src = EVALS / "queue-page-review"
        (d / "inputs" / "current-queue-page.html").write_bytes((src / "inputs" / "current-queue-page.html").read_bytes())
        tok = json.dumps({"schema": "design-tokens.v1", "spacing_px": [4, "8"], "themes": ["light"]}).encode()
        (d / "inputs" / "tokens.json").write_bytes(tok)
        idx = copy.deepcopy(store.index)
        t = copy.deepcopy(idx["artifacts"][0])
        t.update(artifact_id="t-queue-review.tokens", location="tokens.json", content_digest=hashlib.sha256(tok).hexdigest())
        idx["artifacts"].append(t)
        (d / "inputs" / "artifacts.json").write_text(json.dumps(idx))
        a = copy.deepcopy(base)
        a["reviewed_artifacts"].append({"artifact_id": "t-queue-review.tokens", "kind": "ui-evidence", "digest": t["content_digest"]})
        (d / "a.json").write_text(json.dumps(a))
        p = self.run_cli(REPO / "scripts/capability_workflow.py", "validate", "--analysis", d / "a.json",
                         "--evidence", src / "evidence-pack.json", "--artifacts", d / "inputs" / "artifacts.json")
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn(p.returncode, (1, 2), p.stdout + p.stderr)
        self.assertIn("spacing-scale", p.stdout)  # the failed check surfaces as an uncited-failure error, not a crash


if __name__ == "__main__":
    import unittest
    unittest.main()
