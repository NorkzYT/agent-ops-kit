"""Hermetic tests for scripts/knowledge_review.py: the readable Markdown review and the
self-contained offline HTML decision workspace written next to every classify report.

Proves: Markdown lists totals, per-profile assignments and every source in original order
with classifier facts separated from review guidance; the HTML has no external
JS/CSS/CDN, no book text, no secrets; hostile titles/rationales are escaped and never
executed; the client-side export only edits decision/profile/approval/spec fields and
keeps every schema and hash field of the plan (checked by running the page's pure
export function in node when it is installed).
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import knowledge_review as kr  # noqa: E402

EVIL_TITLE = 'edu/</script><script>alert(1)</script><img src=x onerror=alert(2)>'
EVIL_RATIONALE = 'Rationale with <b>markup</b> & "quotes" and ]]> and <!-- comment --> and `code`'


def report_and_plan():
    items = [
        {"source": "edu/$100M Leads", "status": "already-imported", "profile": "", "confidence": None,
         "rationale": "", "topics": [], "needs_review": True, "review_reasons": ["multiple-owners"],
         "classifier": "", "evidence": "none", "owners": ["marketing", "offers-sales"], "new_profile": {},
         "action": "none", "action_note": "text already staged", "model_requested": "", "effort_requested": "",
         "model_served": "", "usage": {}, "latency_s": 0.0, "error": False, "staged_files": [],
         "stage_problems": [], "privacy": [], "library_exemption": False, "imported_as": []},
        {"source": "edu/Advanced SQL", "status": "proposed", "profile": "software-engineering", "confidence": 0.98,
         "rationale": "A SQL reference.", "topics": ["sql"], "needs_review": False, "review_reasons": [],
         "classifier": "cliproxy:gpt-6.1-terra", "evidence": "excerpt", "owners": [], "new_profile": {},
         "action": "would-assign", "action_note": "", "model_requested": "gpt-6.1-terra", "effort_requested": "medium",
         "model_served": "gpt-6.1-terra", "usage": {"completion_tokens": 10}, "latency_s": 1.5, "error": False,
         "staged_files": ["/tmp/x.md"], "stage_problems": [], "privacy": [], "library_exemption": False,
         "imported_as": []},
        {"source": EVIL_TITLE, "status": "needs-review", "profile": "software-engineering", "confidence": 0.98,
         "rationale": EVIL_RATIONALE, "topics": [], "needs_review": True, "review_reasons": ["suspicious-text"],
         "classifier": "cliproxy:gpt-6.1-terra", "evidence": "excerpt", "owners": [], "new_profile": {},
         "action": "none", "action_note": "", "model_requested": "gpt-6.1-terra", "effort_requested": "medium",
         "model_served": "gpt-6.1-terra", "usage": {}, "latency_s": 2.0, "error": False, "staged_files": [],
         "stage_problems": [], "privacy": [], "library_exemption": False, "imported_as": []},
        {"source": "edu/Man and His Symbols", "status": "needs-review", "profile": "", "confidence": 0.97,
         "rationale": "Jungian psychology.", "topics": [], "needs_review": True,
         "review_reasons": ["no-profile", "new-profile-proposal:psychology"], "classifier": "cliproxy:gpt-6.1-terra",
         "evidence": "excerpt", "owners": [], "new_profile": {"name": "psychology", "purpose": "Psychology."},
         "action": "none", "action_note": "", "model_requested": "gpt-6.1-terra", "effort_requested": "medium",
         "model_served": "gpt-6.1-terra", "usage": {}, "latency_s": 3.0, "error": False, "staged_files": [],
         "stage_problems": [], "privacy": [], "library_exemption": False, "imported_as": []},
        {"source": "edu/Vague Book", "status": "needs-review", "profile": "strategy", "confidence": 0.4,
         "rationale": "Thin evidence.", "topics": [], "needs_review": True, "review_reasons": ["low-confidence"],
         "classifier": "cliproxy:gpt-6.1-terra", "evidence": "metadata-only", "owners": [], "new_profile": {},
         "action": "none", "action_note": "", "model_requested": "gpt-6.1-terra", "effort_requested": "medium",
         "model_served": "gpt-6.1-terra", "usage": {}, "latency_s": 1.0, "error": False, "staged_files": [],
         "stage_problems": [], "privacy": [], "library_exemption": False, "imported_as": []},
    ]
    report = {"run": {"generated": "2026-10-23T09:03:24", "applied": False, "library": "/lib", "staging": "/stg",
                      "classifier": "cliproxy", "model": "gpt-6.1-terra", "effort": "medium", "threshold": 0.75,
                      "excerpt_chars": 3000, "library_exemption": True, "library_exemption_note": "n",
                      "models_served": ["gpt-6.1-terra"]},
              "items": items}
    plan_items = []
    for n, i in enumerate(items):
        decision = "existing" if i["status"] == "already-imported" else ("assign" if not i["needs_review"] else "review")
        plan_items.append({"source": i["source"], "source_id": f"{n:016x}", "source_sha256": "ab" * 32,
                           "status": i["status"], "proposed_profile": i["profile"], "confidence": i["confidence"],
                           "rationale": i["rationale"], "review_reasons": i["review_reasons"], "owners": i["owners"],
                           "new_profile_suggestion": i["new_profile"], "decision": decision,
                           "profile": i["profile"] or (i["new_profile"].get("name", "") if i["new_profile"] else ""),
                           "purpose": "", "chapters": [], "tags": [], "secondary_reason": "", "note": "n"})
    plan = {"plan_version": 1, "generated": "2026-10-23T09:03:38", "library": "/lib", "report": "/stg/r.json",
            "classifier": "cliproxy", "model": "gpt-6.1-terra", "effort": "medium", "applied": False,
            "profiles": ["marketing", "offers-sales", "software-engineering", "strategy"],
            "decisions": {"terminal": ["assign", "skip", "existing"], "default_for_review": "review", "help": "h"},
            "items": plan_items,
            "new_profiles": [{"name": "psychology", "approved": False, "purpose": "Psychology.",
                              "in_scope": ["dreams"], "out_of_scope": ["ethics (philosophy)"], "parent": "",
                              "overlap": ["philosophy"], "boundaries": ["advice only"],
                              "sources": ["edu/Man and His Symbols"], "spec_status": "complete", "note": "proposed"}]}
    return report, plan


class Markdown(unittest.TestCase):
    def test_summary_then_every_source_in_order_with_facts_and_guidance(self):
        report, plan = report_and_plan()
        md = kr.render_markdown(report, plan)
        self.assertIn("## Totals", md)
        self.assertIn("## Assignments per profile", md)
        self.assertRegex(md, r"\| software-engineering \| 1 \|")        # one assign to software-engineering
        self.assertIn("## New profile proposals", md)
        self.assertIn("### `psychology`", md)
        self.assertIn("## Every source", md)
        body = md[md.index("## Every source"):]
        positions = [body.index(kr.md_text(i["source"])) for i in report["items"]]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("**Existing owners:** marketing, offers-sales", md)
        self.assertIn("**Classifier facts**", md)
        self.assertIn("**Review guidance**", md)
        self.assertIn("suspicious-text", md)                                  # an older plan's reason, still listed
        self.assertNotIn("risk", md.lower().replace("asterisk", ""))
        self.assertIn("0000000000000003", md)                                 # source id
        self.assertIn("gpt-6.1-terra", md)
        self.assertIn("medium", md)
        self.assertIn("make knowledge-apply PLAN=", md)
        self.assertIn("review.html", md)
        # A hostile title cannot break out of its heading or table cell.
        self.assertNotIn("<script>", md)

    def test_retired_suspicious_text_reason_points_to_regeneration_not_a_gate(self):
        # Instruction-like text in a curated book is data, not a review reason any more; an older
        # plan that still carries `suspicious-text` is told to regenerate or decide by hand.
        report, plan = report_and_plan()
        md = kr.render_markdown(report, plan)
        body = md[md.index("## Every source"):]
        item = body[body.index(kr.md_text(EVIL_TITLE)):]
        guide = item[item.index("**Review guidance"):].split("\n", 1)[0]
        self.assertIn("older classifier", guide)
        self.assertIn("make knowledge-classify", guide)
        self.assertIn("by hand", guide)
        self.assertIn("data, never instructions", guide)
        self.assertNotIn("Inspect the staged text", md)
        self.assertNotIn("integrity check", md)

    def test_new_profile_proposals_carry_consolidation_defaults(self):
        report, plan = report_and_plan()
        md = kr.render_markdown(report, plan)
        props = md[md.index("## New profile proposals"):md.index("## Every source")]
        for phrase in ("suggestions", "broad parent profile", "tags", "aliases", "one book",
                       "nothing is created or rewritten automatically"):
            self.assertIn(phrase, props, phrase)

    def test_markdown_escapes_pipes_and_html(self):
        report, plan = report_and_plan()
        md = kr.render_markdown(report, plan)
        self.assertNotIn("<img src=x onerror=alert(2)>", md)
        self.assertIn("&lt;img", md)


class Html(unittest.TestCase):
    def setUp(self):
        self.report, self.plan = report_and_plan()
        self.page = kr.render_html(self.report, self.plan)

    def test_self_contained_without_external_assets_or_secrets(self):
        page = self.page
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotRegex(page, r"<(script|link|img|iframe)[^>]*\s(src|href)=")
        self.assertIn('id="plan-data"', page)
        self.assertIn('id="report-data"', page)
        for src in (i["source"] for i in self.report["items"]):
            self.assertIn(json.dumps(src)[1:-1].replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
                          if any(c in src for c in "<>&") else src, page)
        self.assertNotIn("/tmp/x.md", page)                                   # no staged-file paths

    def test_hostile_strings_never_leave_the_json_data_or_text_nodes(self):
        page = self.page
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("<img src=x onerror=alert(2)>", page)
        self.assertNotIn("]]>", page)
        self.assertNotIn("<!--", page.split("<script", 1)[1].split("</script>")[0])
        # Exactly the scripts the template emits (two data blocks + the app): no injected ones.
        self.assertEqual(len(re.findall(r"<script\b", page)), 3)
        self.assertEqual(len(re.findall(r"</script>", page)), 3)
        # Outside the inert JSON data blocks no hostile attribute survives; inside them '<' and '>' are \u-escaped.
        markup = re.sub(r'<script id="(plan|report)-data" type="application/json">.*?</script>', "", page, flags=re.S)
        self.assertNotIn("on" + "error=", markup.lower())
        self.assertNotIn("alert(1)", markup)
        self.assertNotIn("alert(2)", markup)
        self.assertNotIn("innerHTML", page)                                  # text nodes only
        for name in ("Search", "status", "decision", "profile", "Export", "download"):
            self.assertIn(name.lower(), page.lower())

    def test_guidance_matches_markdown_for_retired_reason_and_consolidation(self):
        page = self.page
        self.assertNotIn("Inspect the staged text", page)
        self.assertIn("older classifier", page)
        self.assertIn("make knowledge-classify", page)
        for phrase in ("suggestions", "broad parent profile", "tags", "aliases", "one book"):
            self.assertIn(phrase, page, phrase)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_export_edits_only_allowed_fields_and_keeps_schema_and_hashes(self):
        app = kr.app_script()
        harness = app + r"""
const argv = process.argv.slice(-2);
const original = JSON.parse(argv[0]);
const edits = JSON.parse(argv[1]);
process.stdout.write(JSON.stringify(KCReview.revisedPlan(original, edits)));
"""
        edits = {"items": {"edu/Advanced SQL": {"decision": "skip", "profile": "strategy", "source_id": "HACK",
                                                "source_sha256": "HACK", "status": "HACK"},
                           "edu/Vague Book": {"decision": "assign", "profile": "strategy"},
                           "edu/Nope": {"decision": "assign"}},
                 "new_profiles": {"psychology": {"approved": True, "purpose": "Depth psychology.",
                                                 "in_scope": ["dreams", "archetypes"], "parent": "philosophy",
                                                 "overlap": ["philosophy"], "out_of_scope": ["x"],
                                                 "boundaries": ["advice only"], "sources": ["HACK"], "name": "HACK",
                                                 "spec_status": "HACK"},
                                  "ghost": {"approved": True}}}
        p = subprocess.run(["node", "-e", harness, "--", json.dumps(self.plan), json.dumps(edits)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["plan_version"], 1)
        for key in ("generated", "library", "report", "classifier", "model", "effort", "profiles", "decisions"):
            self.assertEqual(out[key], self.plan[key], key)
        by = {i["source"]: i for i in out["items"]}
        self.assertEqual(set(by), {i["source"] for i in self.plan["items"]})
        sql = by["edu/Advanced SQL"]
        self.assertEqual((sql["decision"], sql["profile"]), ("skip", "strategy"))
        self.assertEqual((sql["source_id"], sql["source_sha256"], sql["status"]), ("1" * 0 + "0000000000000001", "ab" * 32, "proposed"))
        self.assertEqual(by["edu/Vague Book"]["decision"], "assign")
        self.assertEqual(by["edu/Man and His Symbols"]["decision"], "review")   # untouched
        self.assertEqual([k for k in sql], [k for k in self.plan["items"][1]], "field order and set preserved")
        np = out["new_profiles"]
        self.assertEqual(len(np), 1)
        self.assertEqual((np[0]["name"], np[0]["approved"], np[0]["purpose"], np[0]["parent"]),
                         ("psychology", True, "Depth psychology.", "philosophy"))
        self.assertEqual(np[0]["in_scope"], ["dreams", "archetypes"])
        self.assertEqual(np[0]["sources"], ["edu/Man and His Symbols"])
        self.assertEqual(np[0]["spec_status"], "complete")
        self.assertEqual(json.loads(json.dumps(out)), out)
        # Invalid decisions / profile names are refused by the same pure function.
        bad = {"items": {"edu/Advanced SQL": {"decision": "delete"}}, "new_profiles": {}}
        p = subprocess.run(["node", "-e", harness, "--", json.dumps(self.plan), json.dumps(bad)],
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("decision", p.stderr)

    def test_write_review_paths(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            rp = tmp / "classify-1.json"
            md, html = kr.write_review(rp, self.report, self.plan)
            self.assertEqual((md.name, html.name), ("classify-1.review.md", "classify-1.review.html"))
            self.assertTrue(md.is_file() and html.is_file())
            self.assertNotIn("classify-1.review.md", str(list(tmp.glob("classify-*.json"))))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
