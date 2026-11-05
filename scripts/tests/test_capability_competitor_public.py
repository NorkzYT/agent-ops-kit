"""RES-01 on real public sources: Notion, Trello and Asana pricing pages retrieved 2026-11-04.

The repository holds only derived, attributed excerpts (verbatim fragments in page order plus the
URL, retrieval time and SHA-256 of the full retrieved text); the full snapshots are not shipped.
The analysis is coordinator-authored. These tests check what the validator can check offline: the
quotes against the excerpts, the price wording, hosts, freshness and labels, and the snapshot
re-check mechanics on stand-in snapshots. The re-check against the real snapshots is a recorded
coordinator run (capability_workflow.py --snapshot-dir), not part of this offline suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_competitor_research import ResearchCase, by_id, errors  # noqa: E402
from test_capability_discovery import section  # noqa: E402
from workflow_helpers import EVALS, REG, REPO, cr, mutate, wf  # noqa: E402

PUBLIC = "public-work-management-pricing"
URLS = {"public-1": "https://www.notion.com/pricing", "public-2": "https://trello.com/pricing", "public-3": "https://asana.com/pricing"}


class PublicPricingSample(ResearchCase):
    scenario = PUBLIC

    def test_sample_is_real_public_primary_web_evidence(self):
        self.assertEqual(self.pack.doc["binding"]["data_classification"], "public")
        self.assertEqual(self.a["authored_by"]["kind"], "coordinator-analysis")
        for sid, url in URLS.items():
            it = self.pack.items[sid]
            self.assertEqual((it["kind"], it["classification"], it["ref"]), ("primary-web", "real", url))
            self.assertEqual(it["retrieved_at"], "2026-11-04T23:26:52.879113+00:00")
            self.assertRegex(it["snapshot_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.errs(), [])
        self.assertEqual(self.errs(today="2026-11-05"), [])

    def test_report_states_prices_cadence_and_ambiguity_as_captured(self):
        out = self.render_out()
        rep = (out / "competitor-report.md").read_text()
        self.assertNotIn("SYNTHETIC", rep)
        src = section(rep, "Sources")
        for sid, url in URLS.items():
            self.assertIn(url, src)
            self.assertIn(self.pack.items[sid]["snapshot_digest"][:16], src)
        price = section(rep, "Pricing")
        self.assertRegex(price, r"Notion \| Plus \| \$10 USD \| per-seat \| \*\*unstated\*\*")
        self.assertRegex(price, r"Notion \| Business \| \$20 USD \| per-seat \| \*\*unstated\*\*")
        self.assertRegex(price, r"Trello \| Standard \| \$5 USD \| per-user \| billed annually")
        self.assertRegex(price, r"Trello \| Standard \| \$6 USD \| per-user \| billed monthly")
        self.assertRegex(price, r"Trello \| Enterprise \| \$17\.50 USD \| per-user \| billed annually")
        self.assertRegex(price, r"Asana \| Starter \| \$10\.99 USD \| per-user \| billed annually")
        self.assertRegex(price, r"Asana \| Starter \| \$13\.49 USD \| per-user \| billed monthly")
        self.assertRegex(price, r"Asana \| Enterprise \| contact sales \(no public price\)")
        self.assertRegex(price, r"Asana \| Enterprise\+ \| \*\*unknown\*\*")
        self.assertIn("retrieved 2026-11-04", price)
        comp = section(rep, "Computed comparisons")
        self.assertRegex(comp, r"c-asana-starter-saving[^\n]*= 18\.53 percent")
        self.assertRegex(comp, r"c-trello-enterprise-year[^\n]*= 210 USD")
        self.assertIn("U-notion-cadence", section(rep, "Unknowns"))
        self.assertIn("Re-verify by 2027-02-02", rep)
        self.assertIn("NOT re-checked against the full snapshots", section(rep, "Limits"))
        self.assertIn("vendor's own page", section(rep, "Limits"))
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "completed")
        self.assertEqual({s["kind"] for s in result["sources"]}, {"primary-web"})
        self.assertTrue(all(s["retrieved_at"] and s["digest"] for s in result["sources"]))
        self.assertTrue(any("not independent product testing" in x for x in result["limitations"]))
        ledger = cr.load_json(out / "evidence-ledger.json")
        e = next(x for x in ledger["entries"] if x["id"] == "p-notion-plus")
        self.assertEqual((e["classification"], e["cadence"]), ("vendor-statement", "unstated"))
        self.assertEqual(e["sources"][0]["snapshot_digest"], self.pack.items["public-1"]["snapshot_digest"])

    def test_repository_ships_derived_excerpts_not_pages(self):
        snapshot_digests = {it["snapshot_digest"] for it in self.pack.items.values()}
        for it in self.pack.items.values():
            text = (EVALS / PUBLIC / it["captured_file"]).read_text()
            self.assertLess(len(text.encode()), 2500, it["captured_file"])
            self.assertIn("Derived excerpt", text)
            self.assertIn(f"URL: {it['ref']}", text)
            self.assertIn(f"Retrieved: {it['retrieved_at']}", text)
            self.assertIn(f"Full snapshot sha256: {it['snapshot_digest']}", text)
            self.assertIn("not redistributed", text)
            quoted = [ln[2:] for ln in text.splitlines() if ln.startswith("> ")]
            self.assertLessEqual(len(quoted), 30)
            self.assertLessEqual(sum(len(q) for q in quoted), 1600)
        found = []
        for root, _, files in os.walk(REPO):
            if "/.git" in root:
                continue
            for name in files:
                p = Path(root) / name
                if p.is_file() and not p.is_symlink() and p.stat().st_size < 200_000:
                    if hashlib.sha256(p.read_bytes()).hexdigest() in snapshot_digests:
                        found.append(str(p))
        self.assertEqual(found, [], "a full retrieved page is in the repository")

    def test_vendor_statements_keep_their_wording_and_labels(self):
        self.assert_error(self.errs(lambda b: by_id(b, "price_points", "p-notion-plus").update(cadence="billed-monthly")),
                          r"p-notion-plus: cadence billed-monthly but the quoted text does not say it is billed monthly")
        self.assert_error(self.errs(lambda b: by_id(b, "price_points", "p-asana-enterprise").update(
            status="stated", amount=30.49, currency="USD", unit="per-user", cadence="billed-monthly")),
            r"p-asana-enterprise: amount 30.49 is not in the quoted text")
        self.assert_error(self.errs(lambda b: by_id(b, "price_points", "p-trello-premium-annual").update(plan="Standard")),
                          r"p-trello-premium-annual: plan 'Standard' is not next to the quoted price")
        self.assert_error(self.errs(lambda b: by_id(b, "statements", "O-five-users").update(source_ids=[])),
                          r"O-five-users: observed statement cites no source")
        self.assert_error(self.errs(lambda b: by_id(b, "statements", "O-notion-billing").update(
            text="Notion has 100 million users and leads the market.")), r"number 100 is not in its cited sources")
        self.assert_error(self.errs(lambda b: by_id(b, "recommendations", "R-sso").update(
            text="Asana is the market leader, so copy its tiers.")), r"R-sso: market size/share/leadership claim")
        self.assert_error(self.errs(lambda b: by_id(b, "findings", "F-sso-bundle").update(
            text="Trello reviewers complain that SSO costs extra.")), r"F-sso-bundle reads as customer research")
        self.assert_error(self.errs(lambda b: by_id(b, "competitors", "asana").update(domain="asana.example")),
                          r"asana cites public-3 \(asana\.com\), which is not on asana\.example")
        self.assert_error(self.errs(today="2027-02-03"), r"today 2027-02-03 is past the re-verify date 2027-02-02")


class SnapshotRecheck(ResearchCase):
    """Mechanics of --snapshot-dir on stand-in snapshots (the real ones stay outside the repository)."""
    scenario = PUBLIC

    def standin(self, sid, transform=lambda s: s):
        """A stand-in 'full page' made of the excerpt's quoted lines with filler between them, bound by digest."""
        it = self.pack.items[sid]
        lines = [ln[2:] for ln in (EVALS / PUBLIC / it["captured_file"]).read_text().splitlines() if ln.startswith("> ")]
        text = transform("\n\nfiller paragraph\n\n".join(lines) + "\n")
        digest = hashlib.sha256(text.encode()).hexdigest()
        a, pack, errs = self.variant(lambda p: next(x for x in p["items"] if x["source_id"] == sid).update(snapshot_digest=digest))
        self.assertEqual(errs, [])
        return a, pack, {digest: text}

    def test_matching_snapshot_rechecks_lines_and_placement(self):
        a, pack, snaps = self.standin("public-2")
        c_errs = errors(a, pack, snapshots=snaps)
        self.assertEqual([e for e in c_errs if "public-2" in e], [])
        self.assertTrue(any("no file in the snapshot directory" in e for e in c_errs))  # the other two were not supplied

    def test_snapshot_missing_a_quoted_line_or_misplacing_a_plan_is_refused(self):
        a, pack, snaps = self.standin("public-2", lambda s: s.replace("Trello your way.", "Trello, your way!"))
        self.assert_error(errors(a, pack, snapshots=snaps), r"public-2: quoted line not in the retrieved snapshot: '# Trello your way\.'")
        a, pack, snaps = self.standin("public-2", lambda s: s.replace("## Premium\n\nfiller paragraph\n\n$10USD", "$10USD\n\nfiller paragraph\n\n## Premium"))
        self.assert_error(errors(a, pack, snapshots=snaps),
                          r"p-trello-premium-annual: in the full snapshot of public-2 plan 'Premium' is not next to the quoted price")

    def test_cli_snapshot_dir_requires_the_bound_snapshots(self):
        empty = self.tmp / "snapshots"
        empty.mkdir()
        (empty / "unrelated.txt").write_text("not a snapshot\n")
        d = EVALS / PUBLIC
        r = self.run_cli("scripts/capability_workflow.py", "validate", "--analysis", d / "analysis-scan.json",
                         "--evidence", d / "evidence-pack.json", "--snapshot-dir", empty)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        errs = json.loads(r.stdout)["errors"]
        self.assertEqual(sorted(re.findall(r"^(public-\d): no file in the snapshot directory", e)[0] for e in errs),
                         ["public-1", "public-2", "public-3"])
        link = self.tmp / "snaplink"
        link.symlink_to(empty)
        r = self.run_cli("scripts/capability_workflow.py", "validate", "--analysis", d / "analysis-scan.json",
                         "--evidence", d / "evidence-pack.json", "--snapshot-dir", link)
        self.assertEqual(r.returncode, 2)
        self.assertIn("symlinked", r.stderr)


if __name__ == "__main__":
    import unittest
    unittest.main()
