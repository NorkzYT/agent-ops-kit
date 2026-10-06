"""Offline tests for content-aware classification and reconciliation.

scripts/knowledge_match.py (fingerprints), scripts/knowledge-classify.py (an
EPUB/PDF/text source whose text is already staged under another path is
reported `already-imported` and never classified, quarantined, assigned or
re-ingested) and scripts/knowledge-reconcile.py (proposal by default, append
only on explicit approval with exact/strong evidence).

Synthetic library, temp profile seeds, the stub converter and stub classifier
server from test_knowledge_classify. Nothing touches /mnt/books, the real
profiles, data/knowledge, the real .env or the network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_knowledge_classify import ANSWERS, STUB_DOC2MD, Stub, answer, tree_hash

REPO = Path(__file__).resolve().parents[2]
CLASSIFY = REPO / "scripts" / "knowledge-classify.py"
RECONCILE = REPO / "scripts" / "knowledge-reconcile.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


km = load(REPO / "scripts" / "knowledge_match.py", "knowledge_match_under_test")

VOCAB = [a + b for a in ("ka", "lo", "mi", "nu", "pe", "ra", "si", "tu", "vo", "ze")
         for b in ("ran", "tel", "dor", "mix", "pul", "sen", "gar", "fol", "bim", "hux")]


def words(seed: int, n: int = 1500) -> list:
    rng = random.Random(seed)
    return [rng.choice(VOCAB) for _ in range(n)]


def prose(ws: list, per_par: int = 60) -> str:
    """Sentences and paragraphs, as an EPUB converter would emit them."""
    pars = []
    for i in range(0, len(ws), per_par):
        chunk = ws[i:i + per_par]
        pars.append(" ".join(chunk[:1]).capitalize() + " " + " ".join(chunk[1:]) + ".")
    return "\n\n".join(pars)


def books_in_text(ws: list) -> str:
    """The same words as a plain-text dump: title page, copyright line, hard wraps, CRLF."""
    body, line, out = " ".join(ws), "", []
    for w in body.split(" "):
        if len(line) + len(w) > 58:
            out.append(line)
            line = ""
        line = (line + " " + w).strip()
    out.append(line)
    return "Converted Title\r\n\r\n\r\nCopyright © 2023\r\n\r\n" + "\r\n".join(out) + "\r\n"


A, B, C, D, E = (words(s) for s in (1, 2, 3, 4, 5))
VOL2 = A[:450] + words(6, 1050)            # same series: shares a 30% preface with A
REVISED = A[:1100] + words(7, 400)         # revised edition: 73% the same as A


class Fingerprints(unittest.TestCase):
    def test_converter_headers_and_whitespace_are_tolerated(self):
        staged = km.fingerprint(books_in_text(A))
        converted = km.fingerprint('---\nsource: "edu/x.epub"\nconverter: "doc2md 1.0.0"\n---\n\n'
                                   "# Converted Title\n\n" + prose(A))
        ev, score = km.compare(converted, staged)
        self.assertEqual(ev, "strong")
        self.assertGreater(score, 0.95)
        plain = km.fingerprint(prose(A))
        self.assertEqual(km.compare(plain, km.fingerprint("---\nk: v\n---\n" + prose(A).upper().replace(" ", "\n\t ")))[0],
                         "exact")

    def test_near_collisions_do_not_match(self):
        a = km.fingerprint(prose(A))
        for other in (VOL2, REVISED, B):
            ev, score = km.compare(a, km.fingerprint(prose(other)))
            self.assertEqual(ev, "", score)
            self.assertLess(score, km.STRONG)

    def test_short_texts_never_match(self):
        self.assertIsNone(km.fingerprint("Text.\n"))
        self.assertEqual(km.compare(km.fingerprint("a b c"), km.fingerprint("a b c")), ("", 0.0))

    def test_fingerprint_is_bounded(self):
        self.assertEqual(len(km.normalize("word " * (km.MAX_TOKENS + 50))), km.MAX_TOKENS)

    def test_stage_paths(self):
        self.assertEqual(km.stage_output("edu/Book/book.epub"), "Book/book.md")
        self.assertEqual(km.stage_output("books-in-text/alex/A.txt"), "alex/A.txt")
        self.assertTrue(km.produces("books-in-text/alex", "alex/A.txt"))
        self.assertTrue(km.produces("edu/Book/book.pdf", "Book/book.md"))
        self.assertFalse(km.produces("edu/Book", "Booklet/x.md"))


class ContentAwareE2E(unittest.TestCase):
    """Library with EPUB/PDF/text copies of text already staged under other paths."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        t = self.tmp
        self.books, self.kroot, self.staging, self.seeds = t / "books", t / "knowledge", t / "staging", t / "profiles"
        lib = {
            "books-in-text/alex/A.txt": books_in_text(A),            # listed sources (offers, marketing)
            "books-in-text/alex/B.txt": books_in_text(B),
            "edu/Leads Book/leads.epub": prose(A),                     # EPUB copy of A
            "edu/Models Book/models.pdf": prose(B),                    # PDF copy of B (two owners)
            "edu/Plain Copy/plain.txt": books_in_text(C).replace("\r\n", "\n\t"),   # text copy of C
            "text/Legacy/C.md": "---\nsource: \"old\"\n---\n\n" + prose(C),          # second candidate for C
            "edu/Series Vol 2/vol2.epub": prose(VOL2),                 # near collision with A
            "edu/Revised Leads/leads2.epub": prose(REVISED),           # near collision with A
            "edu/Same Name/A.txt": books_in_text(D),                   # same file name as staged A, other text
            "edu/Fresh Book/fresh.md": "# Fresh\n\n" + prose(E),
        }
        for rel, text in lib.items():
            (self.books / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.books / rel).write_text(text)
        staged = {
            "offers/alex/A.txt": books_in_text(A),
            "offers/alex/B.txt": books_in_text(B),
            "marketing/alex/B.txt": books_in_text(B),
            "strategy/Legacy/C.md": '---\nsource: "Legacy/c.epub"\nconverter: "doc2md 1.0.0"\n---\n\n# C\n\n' + prose(C),
        }
        for rel, text in staged.items():
            (self.kroot / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.kroot / rel).write_text(text)
        seeds = {"offers": ("sales offers", "books-in-text/alex\n"),
                 "marketing": ("marketing", "books-in-text/alex/B.txt\n"),
                 "strategy": ("business strategy", "")}
        for name, (purpose, entries) in seeds.items():
            (self.seeds / name).mkdir(parents=True)
            (self.seeds / name / "SOUL.md").write_text(f"# SOUL.md\n\nYou are the {purpose} specialist.\n")
            (self.seeds / name / "knowledge-sources.txt").write_text(f"# {name}\n{entries}")
        (t / "ignore.txt").write_text("")
        (t / "private.txt").write_text("")
        (t / "doc2md.py").write_text(STUB_DOC2MD)
        (t / "tmp").mkdir()
        self.cloud = Stub("cloud-stub")
        ANSWERS.update({"edu/Fresh Book": answer("strategy", 0.9, "a strategy text"),
                        "edu/Series Vol 2": answer("offers", 0.9), "edu/Revised Leads": answer("offers", 0.9),
                        "edu/Same Name": answer("marketing", 0.9)})
        self.env = dict(os.environ, BOOKS_ROOT=str(self.books), KNOWLEDGE_ROOT=str(self.kroot),
                        KNOWLEDGE_STAGING=str(self.staging), PROFILES_DIR=str(self.seeds),
                        KNOWLEDGE_IGNORE=str(t / "ignore.txt"), KNOWLEDGE_PRIVATE=str(t / "private.txt"),
                        DOC2MD=str(t / "doc2md.py"), HERMES_HOME=str(t / "hermes"), TMPDIR=str(t / "tmp"),
                        KNOWLEDGE_CLASSIFY_NO_DOTENV="1", KNOWLEDGE_CLASSIFY_BASE_URL=self.cloud.url,
                        CLIPROXY_API_KEY="test-key", OLLAMA_BASE_URL="", OLLAMA_CHAT_MODEL="",
                        MODEL_ROUTER_PRIVATE_TERMS="", MODEL_ROUTER_PRIVATE_PATHS="", KNOWLEDGE_CLASSIFY_LIMIT="50")
        for k in ("APPLY", "APPROVE", "APPROVALS", "SOURCE", "SCOPE", "CLASSIFIER", "LIMIT", "KNOWLEDGE_CLASSIFIER",
                  "KNOWLEDGE_HIGH_RISK_PROFILES", "MAKEFLAGS", "MFLAGS"):
            self.env.pop(k, None)
        self.lib0, self.k0, self.m0 = tree_hash(self.books), tree_hash(self.kroot), self.manifests()
        self._chmod(self.books, writable=False)

    def tearDown(self):
        self.cloud.close()
        self._chmod(self.books, writable=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _chmod(self, root, writable):
        for p in [root, *root.rglob("*")]:
            mode = p.stat().st_mode
            p.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    def manifests(self):
        return {d.name: (d / "knowledge-sources.txt").read_text() for d in sorted(self.seeds.iterdir())}

    def sent_sources(self):
        return sorted(json.loads(json.loads(r["body"])["messages"][-1]["content"]
                                 .split("<<<EVIDENCE\n", 1)[1].rsplit("\nEVIDENCE>>>", 1)[0])["metadata"]["source"]
                      for r in self.cloud.requests)

    def run_py(self, script, *args, **env):
        rpath = self.tmp / f"report-{len(list(self.tmp.glob('report-*.json')))}.json"
        p = subprocess.run([sys.executable, str(script), *args, "--report", str(rpath)], capture_output=True,
                           text=True, env=dict(self.env, **env), timeout=180)
        return p, (json.loads(rpath.read_text()) if rpath.exists() else None)

    def make(self, target, *args):
        return subprocess.run(["make", "-s", "-C", str(REPO), target, *args], capture_output=True, text=True,
                              env=self.env, timeout=180)

    def assert_untouched(self, manifests=True):
        self._chmod(self.books, writable=True)
        try:
            self.assertEqual(self.lib0, tree_hash(self.books), "the source library was modified")
        finally:
            self._chmod(self.books, writable=False)
        self.assertEqual(self.k0, tree_hash(self.kroot), "data/knowledge was modified")
        if manifests:
            self.assertEqual(self.m0, self.manifests(), "a book list was modified")
        self.assertEqual(list((self.tmp / "tmp").glob("knowledge-match.*")), [], "temp text left behind")

    # ------------------------------------------------------------------ #
    def test_default_make_classify_reports_owners_and_never_reprocesses(self):
        p = self.make("knowledge-classify")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        report = sorted((self.staging / "reports").glob("classify-*.json"))[-1]
        items = {i["source"]: i for i in json.loads(report.read_text())["items"]}
        expect = {"edu/Leads Book": ["offers"], "edu/Models Book": ["marketing", "offers"],
                  "edu/Plain Copy": ["strategy"]}
        for rel, owners in expect.items():
            it = items[rel]
            self.assertEqual((it["status"], it["owners"], it["profile"], it["action"], it["classifier"]),
                             ("already-imported", owners, "", "none", ""), rel)
            self.assertEqual(it["staged_files"], [], rel)
        self.assertEqual(items["edu/Models Book"]["review_reasons"], ["multiple-owners"])
        self.assertTrue(items["edu/Models Book"]["needs_review"])
        self.assertEqual({m["profile"] for m in items["edu/Models Book"]["imported_as"]}, {"marketing", "offers"})
        self.assertEqual(items["edu/Plain Copy"]["imported_as"][0]["linked_by"], [])
        self.assertEqual(items["edu/Leads Book"]["imported_as"][0]["linked_by"], ["books-in-text/alex"])
        self.assertIn("owners: marketing, offers   (several: reconciliation review, none chosen)", p.stdout)
        # The model saw only the genuinely new books; nothing already staged was quarantined.
        self.assertEqual(self.sent_sources(), ["edu/Fresh Book", "edu/Revised Leads", "edu/Same Name",
                                               "edu/Series Vol 2"])
        quarantined = {q.name for q in (self.staging / "sources" / "edu").iterdir()}
        self.assertEqual(quarantined, {"Fresh Book", "Revised Leads", "Same Name", "Series Vol 2"})
        self.assert_untouched()

    def test_near_collision_and_same_file_name_are_classified_normally(self):
        p, rep = self.run_py(CLASSIFY, "edu/Series Vol 2", "edu/Revised Leads", "edu/Same Name")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for it in rep["items"]:
            self.assertEqual((it["status"], it["owners"]), ("proposed", []), it["source"])
        self.assertEqual(len(self.cloud.requests), 3)

    def test_apply_never_assigns_an_existing_import(self):
        p, rep = self.run_py(CLASSIFY, "--apply")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        items = {i["source"]: i for i in rep["items"]}
        self.assertEqual(items["edu/Fresh Book"]["action"], "assigned")      # new books still classify + assign
        text = "".join(self.manifests().values())
        for rel in ("edu/Leads Book", "edu/Models Book", "edu/Plain Copy"):
            self.assertEqual(items[rel]["action"], "none")
            self.assertNotIn(rel, text)
        self.assertNotIn("Leads", self.sent_sources()[0])
        for f in ("offers/alex/A.txt", "marketing/alex/B.txt", "strategy/Legacy/C.md"):
            self.assertTrue((self.kroot / f).is_file(), f)
        self.assertFalse((self.kroot / "offers" / "Leads Book").exists(), "re-ingested an existing import")

    def test_unchecked_text_is_never_auto_assigned(self):
        p, rep = self.run_py(CLASSIFY, "--apply", "--no-convert", "edu/Leads Book")
        it = rep["items"][0]
        self.assertEqual(it["status"], "needs-review")
        self.assertIn("content-unchecked", it["review_reasons"])
        self.assertEqual(it["action"], "none")
        self.assert_untouched()

    def test_approval_into_an_owner_profile_is_refused(self):
        p, rep = self.run_py(CLASSIFY, "--apply", "--approved-only", "--approve", "offers=edu/Leads Book")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        it = rep["items"][0]
        self.assertEqual((it["status"], it["action"], it["owners"]), ("already-imported", "refused", ["offers"]))
        self.assertEqual(self.cloud.requests, [])
        self.assert_untouched()

    def test_new_books_get_profile_purpose_and_corpus_context(self):
        p, rep = self.run_py(CLASSIFY, "edu/Fresh Book")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        user = json.loads(self.cloud.requests[0]["body"])["messages"][-1]["content"]
        self.assertIn("- offers: You are the sales offers specialist.", user)
        self.assertIn("corpus: 1 book-list entries, 2 staged files; books: alex", user)
        self.assertIn("- strategy: You are the business strategy specialist.\n    corpus: 0 book-list entries, 1 staged files",
                      user)
        self.assertEqual(rep["items"][0]["profile"], "strategy")

    def test_reconcile_default_is_a_proposal(self):
        p = self.make("knowledge-reconcile")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout
        self.assertIn("PROPOSAL (nothing was changed)", out)
        self.assertIn("Staged without a manifest source (1)", out)
        self.assertIn("- strategy: Legacy/C.md", out)
        self.assertIn("candidate: edu/Plain Copy/plain.txt  (strong", out)
        self.assertIn("candidate: text/Legacy/C.md  (", out)
        self.assertIn("refresh updates this same file", out)
        self.assertIn("refresh would ALSO stage Plain Copy/plain.txt (second copy)", out)
        self.assertIn("several candidates: pick one canonical source yourself (none is chosen)", out)
        self.assertIn("approve: make knowledge-reconcile APPLY=1 APPROVE='strategy=text/Legacy/C.md'", out)
        self.assertIn("Already imported, source not linked (4)", out)
        self.assertIn("reconciliation review: multiple-owners", out)
        for near in ("Series Vol 2", "Revised Leads", "Same Name"):
            self.assertNotIn(f"edu/{near}   [", out)
        self.assertEqual(self.cloud.requests, [])
        self.assert_untouched()

    def test_reconcile_apply_is_explicit_and_needs_evidence(self):
        p, _ = self.run_py(RECONCILE, "--apply")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("explicit approvals", p.stderr)
        p, rep = self.run_py(RECONCILE, "--approve", "strategy=text/Legacy/C.md")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(rep["decisions"][0]["action"], "would-link")
        self.assert_untouched()
        refused = (["strategy=edu/Series Vol 2"],                 # near collision, no evidence
                   ["offers=edu/Leads Book"],                     # offers' copy already has a source
                   ["strategy=edu/Fresh Book"])                   # unrelated text
        for approve in refused:
            p, rep = self.run_py(RECONCILE, "--apply", "--approve", approve[0])
            self.assertEqual(p.returncode, 3, f"{approve}: {p.stdout}{p.stderr}")
            self.assertEqual(rep["decisions"][0]["action"], "refused", approve)
        for bad in ("strategy=../x", "nope=text/Legacy/C.md", "strategy=/etc/passwd"):
            p, _ = self.run_py(RECONCILE, "--apply", "--approve", bad)
            self.assertEqual(p.returncode, 1, bad)
        self.assert_untouched()
        p = self.make("knowledge-reconcile", "APPLY=1", "APPROVE=strategy=text/Legacy/C.md")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        m = self.manifests()
        self.assertEqual(m["strategy"], self.m0["strategy"] + "text/Legacy/C.md\n")
        self.assertEqual({k: v for k, v in m.items() if k != "strategy"},
                         {k: v for k, v in self.m0.items() if k != "strategy"})
        self.assertIn("Staged without a manifest source (0)", p.stdout)
        self.assert_untouched(manifests=False)            # nothing staged, deleted or written to the library
        p, rep = self.run_py(RECONCILE, "--apply", "--approve", "strategy=text/Legacy/C.md")
        self.assertEqual(rep["decisions"][0]["action"], "already-listed")
        self.assertEqual(self.manifests()["strategy"].count("text/Legacy/C.md"), 1)

    def test_inventory_separates_new_imported_and_missing(self):
        with open(self.seeds / "offers" / "knowledge-sources.txt", "a") as fh:
            fh.write("edu/Gone Book\n")
        p = subprocess.run(["bash", str(REPO / "scripts" / "knowledge-refresh.sh"), "--inventory"],
                           capture_output=True, text=True, env=self.env, timeout=180)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout
        self.assertIn("Missing manifest sources (1)", out)
        self.assertIn("Staged without a manifest source (1)", out)
        self.assertRegex(out, r"Already imported, source not linked \(4\)")
        self.assertRegex(out, r"Uncategorized \(4\)")
        imported = out.split("Already imported", 1)[1].split("Uncategorized", 1)[0]
        new = out.split("Uncategorized (4)", 1)[1]
        for rel in ("edu/Leads Book", "edu/Models Book", "edu/Plain Copy", "text/Legacy"):
            self.assertIn(f"  - {rel}\n", imported)
            self.assertNotIn(f"  - {rel}\n", new)
        for rel in ("edu/Fresh Book", "edu/Series Vol 2", "edu/Revised Leads", "edu/Same Name"):
            self.assertIn(f"  - {rel}\n", new)
        self.assertEqual(self.cloud.requests, [])
        self.assertEqual(self.k0, tree_hash(self.kroot))


if __name__ == "__main__":
    unittest.main()
