"""Offline tests for the reviewed-plan workflow: scripts/knowledge_plan.py and
scripts/knowledge-apply.py (one command: validate -> create approved profiles ->
assign -> canonicalize once -> reference -> install/sync -> learn -> report).

A synthetic read-only library, temp profile seeds (PROFILES_DIR), temp
KNOWLEDGE_ROOT / KNOWLEDGE_CANONICAL, a temp HERMES_HOME with a stub ``hermes``
(profile create + learn) and a stub /learn prompt, a stub converter and a temp
ENV_FILE. Nothing touches /mnt/books, hermes/profiles, ~/.hermes or a model.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APPLY = REPO / "scripts" / "knowledge-apply.py"
LEARN = REPO / "scripts" / "knowledge-learn.py"
STAGE = REPO / "scripts" / "knowledge-stage.sh"
TEMPLATE_SOUL = REPO / "hermes" / "profiles" / "_template" / "SOUL.md"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


kp = load("knowledge_plan_under_test", REPO / "scripts" / "knowledge_plan.py")

STUB_DOC2MD = r'''
import json, os, sys
a = sys.argv[1:]
src, dest, rels = a[a.index("--source") + 1], a[a.index("--dest") + 1], a[a.index("--") + 1:]
res = []
for rel in rels:
    t = os.path.join(src, rel)
    paths = [t] if os.path.isfile(t) else [os.path.join(d, f) for d, _, fs in os.walk(t) for f in fs]
    for p in paths:
        if p.endswith((".epub", ".pdf")):
            out = os.path.splitext(os.path.join(dest, os.path.relpath(p, src)))[0] + ".md"
            os.makedirs(os.path.dirname(out), exist_ok=True)
            open(out, "w").write("---\ntitle: \"Converted\"\n---\n\n# Converted\n\n" + open(p).read())
            res.append({"source": os.path.relpath(p, src), "status": "converted", "output": out})
print(json.dumps({"results": res}))
'''

# One stub for both `hermes profile create <name> --clone` and the learn turn
# `hermes -p <p> chat --query-file ...` (same contract as test_knowledge_learn).
STUB_HERMES = r'''#!/usr/bin/env python3
import json, os, re, sys
a = sys.argv[1:]
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": a}) + "\n")
if a[:2] == ["profile", "create"]:
    os.makedirs(os.path.join(os.environ["HERMES_HOME"], "profiles", a[2]), exist_ok=True)
    sys.exit(0)
if a[0] == "-p" and "config" in a:
    sys.exit(0)
prof = a[a.index("-p") + 1]
prompt = open(a[a.index("--query-file") + 1], encoding="utf-8").read()
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": a, "prompt": prompt}) + "\n")
if os.environ.get("STUB_MODE") == "fail":
    print("provider error"); sys.exit(3)
src = re.search(r"from ONE source file:\n\n(.+)\n", prompt).group(1)
slug = re.search(r"`references/([a-z0-9-]+)-<chapter", prompt).group(1)
assert os.path.isfile(src), src
home = os.path.join(os.environ["HERMES_HOME"], "profiles", prof)
skill = os.path.join(home, "skills", "knowledge", prof + "-knowledge")
os.makedirs(os.path.join(skill, "references"), exist_ok=True)
md = os.path.join(skill, "SKILL.md")
body = open(md).read() if os.path.exists(md) else "---\nname: %s-knowledge\n---\n# KB\n## Sources\n" % prof
open(md, "w").write(body + "- %s\n" % slug)
open(os.path.join(skill, "references", slug + "-ch01.md"), "w").write("notes from " + src + "\n")
print("saved")
'''

STUB_LEARN_PROMPT = '''
def build_learn_prompt(user_request):
    return "[/learn] stub official prompt\\n\\nTHE REQUEST:\\n" + user_request + "\\nEND\\n"
'''

STRATEGY_TEXT = "# Strategy\n\n" + ("Competitive advantage, positioning and moats. " * 60) + "\n"


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        h.update(f"{p.relative_to(root)} {st.st_size} {st.st_mtime_ns} {stat.S_IFMT(st.st_mode)}".encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
    return h.hexdigest()


def new_profile_spec(name="amazon-selling", approved=True, **over):
    spec = {"name": name, "approved": approved,
            "purpose": "Amazon marketplace selling: listings, FBA logistics, PPC and account health.",
            "in_scope": ["Amazon seller central operations", "FBA and listings"],
            "out_of_scope": ["general business strategy (business profile)", "tax or legal advice"],
            "parent": "business", "overlap": ["business", "offers-sales"],
            "boundaries": ["no purchases, no account actions without an explicit ask"]}
    spec.update(over)
    return spec


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ka-test-"))
        t = self.tmp
        self.books, self.kroot, self.canon = t / "books", t / "knowledge", t / "canonical"
        self.seeds, self.home = t / "profiles", t / "hermes"
        b = self.books / "edu"
        books = {
            "Strategy Book/strategy.md": STRATEGY_TEXT,
            "Copy Of Strategy/strategy.md": STRATEGY_TEXT,              # same bytes, other path
            "Amazon Selling Guide/guide.epub": "Selling on Amazon FBA.\n",
            "Cloud Security/cloud.txt": "Learning cloud security basics.\n",
            "Two Files/a.md": "# A\n\nfirst file\n",
            "Two Files/b.txt": "second file\n",
        }
        for rel, text in books.items():
            (b / rel).parent.mkdir(parents=True, exist_ok=True)
            (b / rel).write_text(text)
        for name in ("alpha", "beta"):
            (self.seeds / name).mkdir(parents=True)
            (self.seeds / name / "SOUL.md").write_text(f"# SOUL.md\n\nYou are the {name} specialist.\n")
            (self.seeds / name / "knowledge-sources.txt").write_text(f"# {name} books\n")
        (self.seeds / "_template").mkdir()
        shutil.copy(TEMPLATE_SOUL, self.seeds / "_template" / "SOUL.md")
        # alpha is installed already (knowledge-sync path); beta is not (hermes-profile path)
        (self.home / "profiles" / "alpha" / "knowledge").mkdir(parents=True)
        bindir = t / "bin"
        bindir.mkdir()
        self.hermes = bindir / "hermes"
        self.hermes.write_text(STUB_HERMES)
        self.hermes.chmod(0o755)
        agent = t / "hermes-agent" / "agent"
        agent.mkdir(parents=True)
        (agent / "__init__.py").write_text("")
        (agent / "learn_prompt.py").write_text(STUB_LEARN_PROMPT)
        (t / "doc2md.py").write_text(STUB_DOC2MD)
        (t / "env").write_text(f"CLIPROXY_API_KEY=test-key\nHERMES_HOME={self.home}\n")
        self.calls = t / "calls.jsonl"
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("APPROVE", "PLAN", "DRY_RUN", "STUB_MODE", "MAKEFLAGS", "MFLAGS")}
        self.env.update(BOOKS_ROOT=str(self.books), KNOWLEDGE_ROOT=str(self.kroot),
                        KNOWLEDGE_CANONICAL=str(self.canon), PROFILES_DIR=str(self.seeds),
                        HERMES_HOME=str(self.home), HERMES_BIN=str(self.hermes),
                        HERMES_AGENT_DIR=str(t / "hermes-agent"), HERMES_PYTHON=sys.executable,
                        STUB_CALLS=str(self.calls), DOC2MD=str(t / "doc2md.py"), ENV_FILE=str(t / "env"),
                        PATH=f"{bindir}:{os.environ.get('PATH', '')}", TMPDIR=str(t),
                        KNOWLEDGE_LEARN_TIMEOUT="60")
        self.lib_before = tree_hash(self.books)
        self._chmod(self.books, writable=False)

    def tearDown(self):
        self._chmod(self.books, writable=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _chmod(self, root, writable):
        for p in [root, *root.rglob("*")]:
            if p.is_symlink():
                continue
            mode = p.stat().st_mode
            p.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    # helpers --------------------------------------------------------------
    def item(self, source, decision="assign", profile="alpha", status="proposed", **over):
        ident = kp.source_identity(self.books, source)
        d = {"source": source, "source_id": ident.source_id, "source_sha256": ident.sha256,
             "status": status, "proposed_profile": profile if status == "proposed" else "",
             "confidence": 0.9, "rationale": "fits", "risk": [], "review_reasons": [],
             "decision": decision, "profile": profile, "purpose": "", "chapters": [], "tags": [], "note": ""}
        d.update(over)
        return d

    def plan(self, items, new_profiles=(), name="plan"):
        data = {"plan_version": kp.PLAN_VERSION, "generated": "2026-10-22T00:00:00",
                "library": str(self.books), "report": "", "items": items, "new_profiles": list(new_profiles)}
        path = self.tmp / f"{name}.plan.json"
        path.write_text(json.dumps(data, indent=2) + "\n")
        return path

    def run_apply(self, plan, *args, **env):
        p = subprocess.run([sys.executable, str(APPLY), "--plan", str(plan), *args],
                           capture_output=True, text=True, env=dict(self.env, **env), timeout=180)
        return p

    def calls_for(self, kind):
        if not self.calls.exists():
            return []
        out = []
        for line in self.calls.read_text().splitlines():
            c = json.loads(line)
            if kind == "learn" and "prompt" in c:
                out.append(c)
            elif kind == "create" and c["argv"][:2] == ["profile", "create"]:
                out.append(c)
        return out

    def manifest(self, name):
        return (self.seeds / name / "knowledge-sources.txt").read_text()

    def assignments(self, name):
        return json.loads((self.seeds / name / "knowledge-assignments.json").read_text())

    def staged_full_copies(self, name):
        root = self.kroot / name
        if not root.is_dir():
            return []
        return [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".ref.md")]

    def assert_library_unchanged(self):
        self._chmod(self.books, writable=True)
        try:
            self.assertEqual(self.lib_before, tree_hash(self.books), "book library was modified")
        finally:
            self._chmod(self.books, writable=False)

    def assert_nothing_written(self):
        self.assertEqual(self.manifest("alpha"), "# alpha books\n")
        self.assertFalse((self.seeds / "alpha" / "knowledge-assignments.json").exists())
        self.assertFalse(self.kroot.exists())
        self.assertFalse((self.canon / "sources").exists())
        self.assertEqual(self.calls_for("learn"), [])
        self.assert_library_unchanged()

    # plan validation (fail closed) ------------------------------------------
    def test_refuses_without_approval(self):
        p = self.run_apply(self.plan([self.item("edu/Strategy Book")]))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("APPROVE=1", p.stderr)
        self.assert_nothing_written()

    def test_refuses_unresolved_review_item(self):
        plan = self.plan([self.item("edu/Strategy Book"),
                          self.item("edu/Cloud Security", decision="review", profile="", status="needs-review")])
        p = self.run_apply(plan, "--approve")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("edu/Cloud Security", p.stderr)
        self.assertIn("review", p.stderr)
        self.assert_nothing_written()

    def test_refuses_stale_source_hash(self):
        plan = self.plan([self.item("edu/Strategy Book", source_sha256="0" * 64)])
        p = self.run_apply(plan, "--approve")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("stale", p.stderr.lower())
        self.assert_nothing_written()

    def test_refuses_bad_decisions_profiles_and_versions(self):
        cases = {
            "unknown decision": [self.item("edu/Strategy Book", decision="yolo")],
            "unknown profile": [self.item("edu/Strategy Book", profile="ghost")],
            "unsafe profile name": [self.item("edu/Strategy Book", profile="../etc")],
            "underscore profile": [self.item("edu/Strategy Book", profile="_template")],
            "assign without profile": [self.item("edu/Strategy Book", profile="")],
            "unsafe source": [dict(self.item("edu/Strategy Book"), source="../outside")],
            "missing source": [dict(self.item("edu/Strategy Book"), source="edu/Not There")],
            "unknown action": [self.item("edu/Strategy Book", actions=["delete-source"])],
        }
        for label, items in cases.items():
            with self.subTest(label):
                p = self.run_apply(self.plan(items, name=label.replace(" ", "-")), "--approve")
                self.assertEqual(p.returncode, 1, label + ": " + p.stdout + p.stderr)
        bad_version = self.plan([self.item("edu/Strategy Book")], name="v")
        bad_version.write_text(json.dumps({"plan_version": 99, "items": [], "new_profiles": []}))
        self.assertEqual(self.run_apply(bad_version, "--approve").returncode, 1)
        malformed = self.tmp / "bad.plan.json"
        malformed.write_text("{not json")
        self.assertEqual(self.run_apply(malformed, "--approve").returncode, 1)
        self.assert_nothing_written()

    def test_refuses_new_profile_without_approved_full_spec(self):
        src = "edu/Amazon Selling Guide"
        for label, spec in (
            ("not approved", new_profile_spec(approved=False)),
            ("missing scope", new_profile_spec(in_scope=[])),
            ("placeholder purpose", new_profile_spec(purpose="<fill in>")),
            ("bad name", new_profile_spec(name="Amazon Selling")),
            ("existing name", new_profile_spec(name="alpha")),
        ):
            with self.subTest(label):
                plan = self.plan([self.item(src, profile=spec["name"])], [spec], name=label.replace(" ", "-"))
                p = self.run_apply(plan, "--approve")
                self.assertEqual(p.returncode, 1, label + ": " + p.stdout + p.stderr)
        with self.subTest("assignment to an unlisted new profile"):
            plan = self.plan([self.item(src, profile="amazon-selling")], [], name="unlisted")
            self.assertEqual(self.run_apply(plan, "--approve").returncode, 1)
        self.assertFalse((self.seeds / "amazon-selling").exists())
        self.assert_nothing_written()

    def test_dry_run_validates_and_writes_nothing(self):
        plan = self.plan([self.item("edu/Strategy Book"), self.item("edu/Cloud Security", decision="skip", profile="")])
        p = self.run_apply(plan, "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("DRY-RUN", p.stdout)
        self.assertIn("edu/Strategy Book", p.stdout)
        self.assertIn("alpha", p.stdout)
        self.assert_nothing_written()
        self.assertFalse((self.canon / "apply").exists())

    # one command, end to end --------------------------------------------------
    def test_apply_assigns_canonicalizes_references_installs_and_learns(self):
        plan = self.plan([self.item("edu/Strategy Book"),
                          self.item("edu/Amazon Selling Guide", profile="beta", purpose="FBA playbook",
                                    chapters=["FBA"], tags=["amazon"]),
                          self.item("edu/Cloud Security", decision="skip", profile="", status="needs-review")])
        p = self.run_apply(plan, "--approve")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # assignments: book list + metadata, skip touches nothing
        self.assertEqual(self.manifest("alpha"), "# alpha books\nedu/Strategy Book\n")
        self.assertEqual(self.manifest("beta"), "# beta books\nedu/Amazon Selling Guide\n")
        a = self.assignments("beta")["assignments"]
        self.assertEqual(len(a), 1)
        self.assertEqual((a[0]["source"], a[0]["purpose"], a[0]["chapters"], a[0]["tags"]),
                         ("edu/Amazon Selling Guide", "FBA playbook", ["FBA"], ["amazon"]))
        self.assertTrue(a[0]["source_id"])
        self.assertNotIn("edu/Cloud Security", self.manifest("alpha") + self.manifest("beta"))
        # canonical store: one converted copy per source, outside every profile folder
        strategy = kp.source_identity(self.books, "edu/Strategy Book")
        guide = kp.source_identity(self.books, "edu/Amazon Selling Guide")
        self.assertEqual((self.canon / "sources" / strategy.source_id / "strategy.md").read_text(), STRATEGY_TEXT)
        converted = self.canon / "sources" / guide.source_id / "guide.md"
        self.assertIn("Selling on Amazon FBA", converted.read_text())
        self.assertTrue(json.loads((self.canon / "sources" / guide.source_id / "source.json").read_text())["complete"])
        # profiles hold small references only, no duplicate book Markdown
        self.assertEqual(self.staged_full_copies("alpha"), [])
        self.assertEqual(self.staged_full_copies("beta"), [])
        refs_a = list((self.kroot / "alpha" / "refs").glob("*.ref.md"))
        self.assertEqual(len(refs_a), 1, refs_a)
        ref = kp.parse_ref(refs_a[0])
        self.assertEqual((ref["source"], ref["source_id"], ref["sha256"]),
                         ("edu/Strategy Book", strategy.source_id, hashlib.sha256(STRATEGY_TEXT.encode()).hexdigest()))
        self.assertEqual(ref["canonical_rel"], f"sources/{strategy.source_id}/strategy.md")
        self.assertNotIn("Competitive advantage", refs_a[0].read_text())
        # installed profiles: alpha synced, beta created through hermes-profile.sh (stub hermes)
        self.assertTrue((self.home / "profiles" / "alpha" / "knowledge" / "refs" / refs_a[0].name).is_file())
        self.assertEqual(len(self.calls_for("create")), 1)
        self.assertEqual(self.calls_for("create")[0]["argv"][2], "beta")
        self.assertTrue(list((self.home / "profiles" / "beta" / "knowledge" / "refs").glob("*.ref.md")))
        self.assertTrue((self.home / "profiles" / "beta" / "SOUL.md").is_file())
        # learning ran once per reference, on the canonical file
        learned = self.calls_for("learn")
        self.assertEqual(sorted(c["argv"][c["argv"].index("-p") + 1] for c in learned), ["alpha", "beta"])
        self.assertTrue(any(str(converted) in c["prompt"] for c in learned), [c["prompt"] for c in learned])
        state = json.loads((self.home / "profiles" / "alpha" / "knowledge-learn" / "state.json").read_text())
        self.assertEqual([u["status"] for u in state["units"].values()], ["learned"])
        # durable report + checkpoint
        reports = list((self.canon / "apply").glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        rep = json.loads(reports[0].read_text())
        self.assertEqual(rep["outcome"], "complete")
        self.assertEqual(sorted(rep["profiles"]), ["alpha", "beta"])
        self.assertIn("report:", p.stdout)
        self.assert_library_unchanged()

    def test_duplicate_source_gets_one_canonical_copy_and_many_references(self):
        plan = self.plan([self.item("edu/Strategy Book", profile="alpha"),
                          self.item("edu/Copy Of Strategy", profile="beta"),
                          self.item("edu/Strategy Book", profile="beta", secondary_reason="beta reads it for weekly planning reviews")])
        p = self.run_apply(plan, "--approve", "--no-learn")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        a, b = kp.source_identity(self.books, "edu/Strategy Book"), kp.source_identity(self.books, "edu/Copy Of Strategy")
        self.assertEqual(a.source_id, b.source_id, "identical bytes must share one canonical identity")
        self.assertEqual([d.name for d in (self.canon / "sources").iterdir()], [a.source_id])
        self.assertEqual(len(list((self.kroot / "alpha" / "refs").glob("*.ref.md"))), 1)
        self.assertEqual(len(list((self.kroot / "beta" / "refs").glob("*.ref.md"))), 2)
        self.assertEqual(self.manifest("beta"), "# beta books\nedu/Copy Of Strategy\nedu/Strategy Book\n")
        self.assertEqual(self.staged_full_copies("alpha") + self.staged_full_copies("beta"), [])
        self.assertEqual(self.calls_for("learn"), [])
        self.assertEqual(self.assignments("beta")["assignments"][1]["secondary_reason"],
                         "beta reads it for weekly planning reviews")

    def test_approved_new_profile_is_created_from_the_template_and_becomes_a_candidate(self):
        spec = new_profile_spec()
        plan = self.plan([self.item("edu/Amazon Selling Guide", profile="amazon-selling", status="needs-review",
                                    proposed_profile="", review_reasons=["no-profile"])], [spec])
        p = self.run_apply(plan, "--approve", "--no-learn")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pdir = self.seeds / "amazon-selling"
        soul = (pdir / "SOUL.md").read_text()
        self.assertIn("amazon-selling", soul)
        self.assertIn(spec["purpose"], soul)
        self.assertIn("Amazon seller central operations", soul)
        self.assertIn("tax or legal advice", soul)
        self.assertNotIn("<Name>", soul)
        self.assertNotIn("<domain>", soul)
        self.assertEqual(self.manifest("amazon-selling").splitlines()[-1], "edu/Amazon Selling Guide")
        self.assertEqual(self.assignments("amazon-selling")["profile_spec"]["parent"], "business")
        kc = load("kc_under_apply_test", REPO / "scripts" / "knowledge-classify.py")
        env = dict(self.env)
        s = kc.Settings(books=self.books, kroot=self.kroot, staging=self.tmp / "staging", profiles_dir=self.seeds,
                        ignore_file=self.tmp / "ignore", private_file=self.tmp / "private",
                        doc2md=self.tmp / "doc2md.py")
        self.assertIn("amazon-selling", kc.load_profiles(s))
        self.assertTrue((self.home / "profiles" / "amazon-selling" / "SOUL.md").is_file())
        self.assertEqual(self.calls_for("create")[0]["argv"][2], "amazon-selling")
        # the same plan again: the profile this plan created is not "already existing"
        p2 = self.run_apply(plan, "--approve", "--no-learn")
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertEqual(self.manifest("amazon-selling").count("edu/Amazon Selling Guide"), 1)

    def test_rerun_is_idempotent_and_a_failed_step_resumes(self):
        plan = self.plan([self.item("edu/Strategy Book")])
        p = self.run_apply(plan, "--approve", STUB_MODE="fail")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("Resume:", p.stdout + p.stderr)
        self.assertIn("APPROVE=1", p.stdout + p.stderr)
        ck = json.loads(next((self.canon / "apply").glob("*/checkpoint.json")).read_text())
        self.assertEqual(ck["failed_step"], "learn")
        self.assertTrue(ck["steps"]["references"]["done"])
        self.assertEqual(self.manifest("alpha"), "# alpha books\nedu/Strategy Book\n")
        n_fail = len(self.calls_for("learn"))
        self.assertGreaterEqual(n_fail, 1)
        canon_before = tree_hash(self.canon / "sources")
        p2 = self.run_apply(plan, "--approve")
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertEqual(self.manifest("alpha"), "# alpha books\nedu/Strategy Book\n")
        self.assertEqual(tree_hash(self.canon / "sources"), canon_before, "canonical copy was rewritten")
        self.assertEqual(len(self.calls_for("learn")), n_fail + 1)
        p3 = self.run_apply(plan, "--approve")
        self.assertEqual(p3.returncode, 0, p3.stdout + p3.stderr)
        self.assertEqual(len(self.calls_for("learn")), n_fail + 1, "a learned file was learned again")
        self.assertEqual(len(list((self.kroot / "alpha" / "refs").glob("*.ref.md"))), 1)
        self.assertEqual(len(list((self.canon / "apply").glob("*/report.json"))), 1)
        self.assert_library_unchanged()

    def test_removing_an_assignment_never_deletes_the_canonical_copy(self):
        plan = self.plan([self.item("edu/Strategy Book")])
        self.assertEqual(self.run_apply(plan, "--approve", "--no-learn").returncode, 0)
        ident = kp.source_identity(self.books, "edu/Strategy Book")
        (self.seeds / "alpha" / "knowledge-sources.txt").write_text("# alpha books\n")
        (self.seeds / "alpha" / "knowledge-assignments.json").write_text(json.dumps({"version": 1, "assignments": []}))
        plan2 = self.plan([self.item("edu/Cloud Security", profile="beta")], name="later")
        self.assertEqual(self.run_apply(plan2, "--approve", "--no-learn").returncode, 0)
        self.assertTrue((self.canon / "sources" / ident.source_id / "strategy.md").is_file())

    # compatibility -------------------------------------------------------------
    def test_legacy_profile_local_corpus_still_learns_and_duplicates_are_only_reported(self):
        legacy = self.kroot / "alpha" / "Old Book" / "old.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("# Old\n\nlegacy copied text\n")
        dup = self.kroot / "alpha" / "Strategy Book" / "strategy.md"      # a legacy full copy of a canonical book
        dup.parent.mkdir(parents=True)
        dup.write_text(STRATEGY_TEXT)
        plan = self.plan([self.item("edu/Strategy Book")])
        p = self.run_apply(plan, "--approve")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(legacy.is_file() and dup.is_file(), "legacy copies must not be deleted or moved")
        learned = sorted(Path(c["argv"][c["argv"].index("--query-file") + 1]).stem for c in self.calls_for("learn"))
        self.assertEqual(len(learned), 3, learned)        # old.md, legacy strategy.md, canonical reference
        rep = json.loads(next((self.canon / "apply").glob("*/report.json")).read_text())
        self.assertEqual([d["staged"] for d in rep["legacy_duplicates"]], ["alpha/Strategy Book/strategy.md"])
        self.assertIn("not migrated", json.dumps(rep["legacy_duplicates"]).lower() + rep.get("legacy_note", "").lower())
        p = self.run_apply(plan, "--legacy-report")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("alpha/Strategy Book/strategy.md", p.stdout)
        self.assertTrue(dup.is_file())

    def test_knowledge_stage_skips_canonical_assignments(self):
        plan = self.plan([self.item("edu/Strategy Book"), self.item("edu/Two Files", profile="alpha")])
        self.assertEqual(self.run_apply(plan, "--approve", "--no-learn").returncode, 0)
        with open(self.seeds / "alpha" / "knowledge-sources.txt", "a") as fh:
            fh.write("edu/Cloud Security\n")                              # a legacy, non-canonical line
        env = dict(self.env, KNOWLEDGE_MANIFEST=str(self.seeds / "alpha" / "knowledge-sources.txt"))
        p = subprocess.run(["bash", str(STAGE), "alpha"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([x.name for x in self.staged_full_copies("alpha")], ["cloud.txt"])
        self.assertIn("canonical", (p.stdout + p.stderr).lower())
        self.assertEqual(len(list((self.kroot / "alpha" / "refs").glob("*.ref.md"))), 3)

    def test_references_are_tiny_and_learn_resolves_them_through_validation(self):
        plan = self.plan([self.item("edu/Two Files")])
        self.assertEqual(self.run_apply(plan, "--approve", "--no-learn").returncode, 0)
        refs = sorted((self.kroot / "alpha" / "refs").glob("*.ref.md"))
        self.assertEqual(len(refs), 2)
        self.assertTrue(all(r.stat().st_size < 2048 for r in refs))
        kl = load("kl_under_apply_test", LEARN)
        saved = dict(os.environ)
        os.environ.update({k: self.env[k] for k in ("HERMES_HOME", "KNOWLEDGE_CANONICAL")})
        try:
            units = kl.units(kl.Profile("alpha"))
            self.assertEqual(sorted(u["class"] for u in units), ["pending", "pending"])
            self.assertTrue(all(str(u["path"]).startswith(str(self.canon)) for u in units))
            self.assertEqual(sorted(u["rel"] for u in units), sorted("refs/" + r.name for r in refs))
            # tamper with one canonical file: its reference no longer validates and is never learned
            ident = kp.source_identity(self.books, "edu/Two Files")
            (self.canon / "sources" / ident.source_id / "b.txt").write_text("changed\n")
            classes = {u["rel"]: u["class"] for u in kl.units(kl.Profile("alpha"))}
            self.assertEqual(sorted(classes.values()), ["invalid-ref", "pending"])
            r = kl.readiness("alpha")
            self.assertEqual((r["invalid_ref"], r["references"], r["pending"]), (1, 2, 1))
            self.assertIn("do not validate", " ".join(r["notes"]))
            # a reference that names another root is refused even when the file there matches its hash
            elsewhere = self.tmp / "elsewhere"
            good = next(u for u in kl.units(kl.Profile("alpha")) if u["class"] == "pending")
            ref = kp.parse_ref(self.kroot / "alpha" / good["rel"])
            (elsewhere / ref["canonical_rel"]).parent.mkdir(parents=True)
            (elsewhere / ref["canonical_rel"]).write_bytes(Path(good["path"]).read_bytes())
            os.environ.pop("KNOWLEDGE_CANONICAL")
            ref["canonical_root"] = str(elsewhere)
            path, problem = kp.resolve_ref(ref, root=self.canon)
            self.assertIsNone(path)
            self.assertIn("canonical_root", problem)
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
