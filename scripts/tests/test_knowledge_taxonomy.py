"""Offline unit tests for scripts/knowledge_taxonomy.py: the deterministic
post-parse consolidation of overlapping new-profile proposals into one broad
family per run. No model, no library, no real profiles."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import knowledge_taxonomy as kt  # noqa: E402


def load_classifier():
    spec = importlib.util.spec_from_file_location("kc_for_taxonomy", REPO / "scripts" / "knowledge-classify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


kc = load_classifier()
EXISTING = ("philosophy", "business", "red-team", "software-engineering", "health-fitness")


def item(source, name="", purpose="p", profile="", **spec):
    it = kc.Item(source, status="needs-review", profile=profile, confidence=0.8, rationale="r", topics=["t"])
    if name:
        it.new_profile = {"name": name, "purpose": purpose, **spec}
        it.review_reasons = ["no-profile", f"new-profile-proposal:{name}"]
    elif profile:
        it.review_reasons = ["low-confidence"]
    it.needs_review = True
    return it


class DefaultTaxonomyFile(unittest.TestCase):
    def test_committed_default_covers_the_known_families(self):
        fams = kt.load_taxonomy(kt.DEFAULT_TAXONOMY_FILE)
        self.assertEqual(fams["psychology"].name, "psychology")
        for alias in ("depth-psychology", "psychology-mental-health", "psychology-personality", "psychology-psychotherapy"):
            self.assertIn(alias, fams["psychology"].aliases, alias)
        for alias in ("blue-team-incident-response", "blue-team-security-operations"):
            self.assertIn(alias, fams["blue-team-security"].aliases, alias)
        for f in fams.values():
            self.assertTrue(kt.NAME_RE.match(f.name), f.name)
            self.assertTrue(all(kt.NAME_RE.match(a) for a in f.aliases), f.name)
            self.assertNotIn(f.name, f.aliases)

    def test_missing_or_invalid_file_is_an_explicit_error_never_a_silent_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(kt.load_taxonomy(Path(d) / "absent.json", missing_ok=True), {})
            with self.assertRaises(kt.TaxonomyError):
                kt.load_taxonomy(Path(d) / "absent.json")
            bad = Path(d) / "bad.json"
            bad.write_text(json.dumps({"families": {"Bad Name": {"aliases": ["x"]}}}))
            with self.assertRaises(kt.TaxonomyError):
                kt.load_taxonomy(bad)
            dup = Path(d) / "dup.json"
            dup.write_text(json.dumps({"families": {"a": {"aliases": ["z"]}, "b": {"aliases": ["z"]}}}))
            with self.assertRaises(kt.TaxonomyError):
                kt.load_taxonomy(dup)

    def test_user_file_is_explicit_configuration(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            p.write_text(json.dumps({"families": {"gardening": {"aliases": ["permaculture", "gardening-vegetables"],
                                                                "tags": ["soil"], "parent": "health-fitness"}}}))
            fams = kt.load_taxonomy(p)
            self.assertEqual(fams["gardening"].aliases, ("gardening-vegetables", "permaculture"))
            self.assertEqual(fams["gardening"].tags, ("soil",))
            self.assertEqual(fams["gardening"].parent, "health-fitness")


class CanonicalFor(unittest.TestCase):
    def setUp(self):
        self.fams = kt.load_taxonomy(kt.DEFAULT_TAXONOMY_FILE)

    def test_family_map_aliases_fold_into_the_canonical_family(self):
        for alias in ("depth-psychology", "psychology-mental-health", "psychology-personality", "psychology-psychotherapy"):
            self.assertEqual(kt.canonical_for(alias, self.fams, EXISTING, ()), ("psychology", "family-alias"))
        for alias in ("blue-team-incident-response", "blue-team-security-operations"):
            self.assertEqual(kt.canonical_for(alias, self.fams, EXISTING, ()), ("blue-team-security", "family-alias"))

    def test_family_prefix_folds_even_when_the_alias_is_not_listed(self):
        self.assertEqual(kt.canonical_for("psychology-cognitive", self.fams, EXISTING, ()), ("psychology", "family-prefix"))

    def test_existing_broad_profile_wins_over_a_narrow_child_name(self):
        self.assertEqual(kt.canonical_for("red-team-web-apps", self.fams, EXISTING, ()), ("red-team", "existing-profile"))
        self.assertEqual(kt.canonical_for("business-finance", self.fams, EXISTING, ()), ("business", "existing-profile"))

    def test_sibling_proposals_in_one_run_fold_into_their_shortest_root(self):
        proposed = ("machine-learning-ai", "machine-learning", "machine-learning-ai-vision")
        self.assertEqual(kt.canonical_for("machine-learning-ai", self.fams, EXISTING, proposed), ("machine-learning", "sibling-prefix"))
        self.assertEqual(kt.canonical_for("machine-learning-ai-vision", self.fams, EXISTING, proposed), ("machine-learning", "sibling-prefix"))
        self.assertEqual(kt.canonical_for("machine-learning", self.fams, EXISTING, proposed), ("machine-learning", ""))

    def test_unknown_family_is_preserved_verbatim(self):
        self.assertEqual(kt.canonical_for("biotechnology-genetics", self.fams, EXISTING, ("biotechnology-genetics",)),
                         ("biotechnology-genetics", ""))
        self.assertEqual(kt.canonical_for("cloud-security", self.fams, EXISTING, ("cloud-security",)), ("cloud-security", ""))

    def test_never_folds_into_a_template_or_a_word_prefix(self):
        # "business" must not capture "businesslike-writing": only a dash-separated child folds.
        self.assertEqual(kt.canonical_for("businesslike-writing", self.fams, EXISTING, ()), ("businesslike-writing", ""))
        self.assertEqual(kt.canonical_for("_template-x", self.fams, EXISTING + ("_template",), ()), ("_template-x", ""))


class Consolidate(unittest.TestCase):
    def setUp(self):
        self.fams = kt.load_taxonomy(kt.DEFAULT_TAXONOMY_FILE)
        self.items = [
            item("edu/Depth", "depth-psychology", "Jungian depth psychology"),
            item("edu/Therapy", "psychology-psychotherapy", "Psychotherapy practice", parent="health-fitness",
                 in_scope=["therapy"], out_of_scope=["medicine"], boundaries=["advice only"]),
            item("edu/Mind", "psychology-mental-health", "Mental health"),
            item("edu/Blue", "blue-team-incident-response", "Incident response"),
            item("edu/Blue2", "blue-team-security", "Defensive security"),
            item("edu/Genes", "biotechnology-genetics", "Genetics"),
            item("edu/Web", "red-team-web-apps", "Web app pentesting"),
            item("edu/Strategy", profile="business"),
            item("edu/Plain"),
        ]

    def test_one_proposal_per_family_and_sources_remapped(self):
        res = kt.consolidate(self.items, EXISTING, self.fams)
        names = {i.new_profile.get("name") for i in self.items if i.new_profile}
        self.assertEqual(names, {"psychology", "blue-team-security", "biotechnology-genetics"})
        self.assertEqual(sorted(res.families), ["blue-team-security", "psychology", "red-team"])
        psy = res.families["psychology"]
        self.assertEqual(psy.aliases, ("depth-psychology", "psychology-mental-health", "psychology-psychotherapy"))
        self.assertEqual(psy.sources, ("edu/Depth", "edu/Mind", "edu/Therapy"))
        self.assertFalse(psy.existing)
        self.assertEqual(res.families["blue-team-security"].aliases, ("blue-team-incident-response",))
        self.assertEqual(res.families["blue-team-security"].sources, ("edu/Blue", "edu/Blue2"))
        self.assertTrue(res.applied)

    def test_remapped_item_keeps_its_spec_records_the_alias_and_tags(self):
        kt.consolidate(self.items, EXISTING, self.fams)
        therapy = self.items[1]
        self.assertEqual(therapy.new_profile["name"], "psychology")
        self.assertEqual(therapy.new_profile["in_scope"], ["therapy"])             # spec fields survive
        self.assertEqual(therapy.new_profile["aliases"], ["psychology-psychotherapy"])
        self.assertEqual(therapy.taxonomy, {"proposed": "psychology-psychotherapy", "canonical": "psychology",
                                            "rule": "family-alias"})
        self.assertIn("psychotherapy", therapy.tags)
        self.assertIn("depth-psychology", self.items[0].tags)
        self.assertEqual(therapy.review_reasons,
                         ["no-profile", "new-profile-proposal:psychology", "taxonomy-consolidated:psychology-psychotherapy->psychology"])
        self.assertEqual(therapy.profile, "", "a proposal never becomes an assignment")
        self.assertEqual(therapy.status, "needs-review")
        # A canonical name that was proposed as such is untouched.
        blue2 = self.items[4]
        self.assertEqual(blue2.new_profile["name"], "blue-team-security")
        self.assertEqual(blue2.taxonomy, {})
        self.assertEqual(blue2.review_reasons, ["no-profile", "new-profile-proposal:blue-team-security"])

    def test_existing_broad_profile_is_assigned_for_review_not_proposed_again(self):
        res = kt.consolidate(self.items, EXISTING, self.fams)
        web = self.items[6]
        self.assertEqual(web.profile, "red-team")
        self.assertEqual(web.new_profile, {})
        self.assertEqual(web.status, "needs-review", "a deterministic remap is reviewed, never auto-assigned")
        self.assertTrue(web.needs_review)
        self.assertEqual(web.review_reasons, ["taxonomy-existing-profile:red-team-web-apps->red-team"])
        self.assertIn("web-apps", web.tags)
        self.assertTrue(res.families["red-team"].existing)
        self.assertEqual(res.families["red-team"].sources, ("edu/Web",))

    def test_canonical_family_that_already_exists_is_assigned_not_proposed(self):
        res = kt.consolidate(self.items, EXISTING + ("psychology",), self.fams)
        for i in self.items[:3]:
            self.assertEqual((i.profile, i.new_profile), ("psychology", {}), i.source)
            self.assertEqual(i.status, "needs-review")
        self.assertTrue(res.families["psychology"].existing)
        self.assertEqual(res.families["psychology"].aliases, ("depth-psychology", "psychology-mental-health", "psychology-psychotherapy"))

    def test_unknown_family_and_plain_items_are_untouched(self):
        before = [asdict(i) for i in self.items[5:]]
        res = kt.consolidate(self.items, EXISTING, self.fams)
        after = [asdict(i) for i in self.items[5:]]
        self.assertEqual(before[0], after[0])      # biotechnology-genetics: no sibling, no family, kept verbatim
        self.assertEqual(before[2:], after[2:])    # assigned and empty items
        self.assertNotIn("biotechnology-genetics", res.families)

    def test_idempotent(self):
        kt.consolidate(self.items, EXISTING, self.fams)
        snap = [asdict(i) for i in self.items]
        res2 = kt.consolidate(self.items, EXISTING, self.fams)
        self.assertEqual(snap, [asdict(i) for i in self.items])
        self.assertFalse(res2.applied)
        self.assertEqual(res2.families, {})

    def test_no_proposals_means_nothing_applied(self):
        res = kt.consolidate(self.items[7:], EXISTING, self.fams)
        self.assertFalse(res.applied)
        self.assertEqual(res.summary()["applied"], False)

    def test_summary_is_plain_json_metadata(self):
        res = kt.consolidate(self.items, EXISTING, self.fams)
        s = res.summary()
        json.dumps(s)
        self.assertTrue(s["applied"])
        self.assertEqual(s["families"]["psychology"]["aliases"],
                         ["depth-psychology", "psychology-mental-health", "psychology-psychotherapy"])
        self.assertEqual(s["families"]["psychology"]["existing"], False)
        self.assertEqual(s["families"]["red-team"]["existing"], True)
        self.assertIn("policy", s)
        for word in ("risk", "high-risk", "suspicious"):
            self.assertNotIn(word, json.dumps(s).lower())

    def test_spec_enrichment_adds_parent_tags_and_aliases_without_overwriting(self):
        res = kt.consolidate(self.items, EXISTING, self.fams)
        specs = {"psychology": {"name": "psychology", "purpose": "Psychology", "parent": "health-fitness"},
                 "blue-team-security": {"name": "blue-team-security", "purpose": "Blue team"},
                 "biotechnology-genetics": {"name": "biotechnology-genetics", "purpose": "G"}}
        out = kt.enrich_specs(specs, res, EXISTING, self.fams)
        self.assertEqual(out["psychology"]["parent"], "health-fitness", "a parent the model gave is kept")
        self.assertEqual(out["psychology"]["aliases"], ["depth-psychology", "psychology-mental-health", "psychology-psychotherapy"])
        self.assertIn("psychotherapy", out["psychology"]["tags"])
        self.assertIn("mental-health", out["psychology"]["tags"])
        self.assertIn("incident-response", out["blue-team-security"]["tags"])
        self.assertEqual(out["blue-team-security"]["parent"], "none")   # family default when the model gave none
        self.assertEqual(out["biotechnology-genetics"].get("aliases", []), [])
        self.assertNotIn("red-team", out, "an existing profile is never a new-profile spec")


class PromptPolicy(unittest.TestCase):
    def test_prompt_prefers_existing_broad_profiles_and_forbids_narrow_siblings(self):
        p = kc.SYSTEM_PROMPT
        self.assertIn("existing broad profile", p)
        self.assertIn("reasonably own the book", p)
        self.assertIn("broad parent-level", p)
        self.assertIn("not narrow subcategories", p)
        self.assertIn("tags", p.lower())
        self.assertIn("never propose", p.lower())
        self.assertIn("overlapping", p)
        self.assertRegex(p, r"\"tags\": \[")
        self.assertNotIn("risk", p.lower())

    def test_tags_in_the_answer_are_kept_bounded_and_sanitized(self):
        v = kc.parse_verdict(json.dumps({"profile": "none", "confidence": 0.8, "rationale": "x",
                                         "topics": ["t"], "tags": ["Mental Health", "x" * 200, 5, "[a](http://e)"],
                                         "new_profile": {"name": "psychology", "purpose": "p",
                                                         "tags": ["therapy", "therapy", 1]}}), ["alpha"])
        self.assertEqual(v.tags[:1], ["Mental Health"])
        self.assertTrue(all(len(t) <= 60 for t in v.tags))
        self.assertNotIn("http://", "".join(v.tags))
        self.assertEqual(v.new_profile["tags"], ["therapy"])


if __name__ == "__main__":
    unittest.main()
