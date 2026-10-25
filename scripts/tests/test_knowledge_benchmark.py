"""Hermetic tests for scripts/knowledge-benchmark.py (the classifier model/effort benchmark).

A synthetic rubric, a synthetic evidence catalog and the same stub chat-completions
server the classify tests use. Proves: the rubric is validated; the runner sends the
production classifier prompt with only model/effort varying, records failures instead
of inventing results, and never writes outside --out; the scorer computes exact /
lenient / new-profile-family / none correctness, malformed and unknown-profile counts,
calibration, latency percentiles, token usage, served-model agreement, repeat
consistency and failure rate per cell; the Make target is safe by construction.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "knowledge-benchmark.py"


def load_module():
    spec = importlib.util.spec_from_file_location("knowledge_benchmark_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


kb = load_module()

RUBRIC = {
    "rubric_version": 1,
    "families": {"psychology": ["psych"], "security": ["secur"], "ai-engineering": ["ai", "ml", "machine-learning"]},
    "profiles": ["philosophy", "red-team", "software-engineering", "strategy"],
    "sources": [
        {"source": "edu/SQL", "source_id": "a" * 16, "stratum": "clear-existing",
         "expected": {"kind": "existing", "primary": "software-engineering", "acceptable": []}},
        {"source": "edu/Jung", "source_id": "b" * 16, "stratum": "new-profile",
         "expected": {"kind": "new", "family": "psychology", "acceptable_families": [], "acceptable": ["philosophy"]}},
        {"source": "edu/AI Sec", "source_id": "c" * 16, "stratum": "security",
         "expected": {"kind": "new", "family": "security", "acceptable_families": ["ai-engineering"],
                      "acceptable": ["red-team"]}},
        {"source": "edu/Ambiguous", "source_id": "d" * 16, "stratum": "ambiguous-existing",
         "expected": {"kind": "existing", "primary": "strategy", "acceptable": ["software-engineering"]}},
    ],
}


def result(source, model="m", effort="medium", repeat=1, status="classified", profile="", conf=0.9,
           new_profile="", latency=1.0, served=None, usage=None, error=""):
    return {"source": source, "model": model, "effort": effort, "repeat": repeat, "status": status,
            "profile": profile, "confidence": conf, "new_profile_name": new_profile, "latency_s": latency,
            "model_served": model if served is None else served, "usage": usage or {}, "error": error,
            "rationale": "r"}


class Rubric(unittest.TestCase):
    def test_valid_rubric_loads_and_invalid_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = tmp / "r.json"
            p.write_text(json.dumps(RUBRIC))
            r = kb.load_rubric(p)
            self.assertEqual(len(r["sources"]), 4)
            bad = json.loads(json.dumps(RUBRIC))
            bad["sources"][0]["expected"]["primary"] = "ghost"
            p.write_text(json.dumps(bad))
            with self.assertRaises(kb.BenchmarkError):
                kb.load_rubric(p)
            bad = json.loads(json.dumps(RUBRIC))
            bad["sources"][1]["expected"]["family"] = "nope"
            p.write_text(json.dumps(bad))
            with self.assertRaises(kb.BenchmarkError):
                kb.load_rubric(p)
            bad = json.loads(json.dumps(RUBRIC))
            bad["sources"].append(dict(RUBRIC["sources"][0]))
            p.write_text(json.dumps(bad))
            with self.assertRaises(kb.BenchmarkError):
                kb.load_rubric(p)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_family_of_a_proposed_name(self):
        fam = RUBRIC["families"]
        self.assertEqual(kb.family_of("analytical-psychology", fam), "psychology")
        self.assertEqual(kb.family_of("cloud-application-security", fam), "security")
        self.assertEqual(kb.family_of("machine-learning-ai", fam), "ai-engineering")
        self.assertEqual(kb.family_of("biotech-science", fam), "")
        self.assertEqual(kb.family_of("", fam), "")


class Scorer(unittest.TestCase):
    def test_per_cell_metrics(self):
        rows = [
            result("edu/SQL", profile="software-engineering", conf=0.95, latency=1.0, usage={"completion_tokens": 10, "reasoning_tokens": 4}),
            result("edu/SQL", repeat=2, profile="software-engineering", conf=0.9, latency=3.0),
            result("edu/Jung", profile="", new_profile="analytical-psychology", conf=0.8, latency=2.0),
            result("edu/Jung", repeat=2, profile="philosophy", conf=0.6, latency=2.0),          # lenient only
            result("edu/AI Sec", profile="", new_profile="machine-learning-ai", conf=0.7, latency=9.0),  # lenient (family)
            result("edu/AI Sec", repeat=2, status="malformed", profile="", conf=0.0, latency=1.0,
                   served="other-model"),
            result("edu/Ambiguous", profile="philosophy", conf=0.99, latency=1.0),           # wrong, confident
            result("edu/Ambiguous", repeat=2, status="error", error="HTTP 500", latency=120.0),
        ]
        rows[5]["rationale"] = "profile 'ghost' is not one of the listed profiles"
        s = kb.score_results(RUBRIC, rows)
        self.assertEqual(list(s["cells"]), ["m/medium"])
        c = s["cells"]["m/medium"]
        self.assertEqual(c["requests"], 8)
        self.assertEqual(c["failures"], 1)
        self.assertEqual(c["malformed"], 1)
        self.assertEqual(c["unknown_profile"], 1)
        self.assertEqual(c["classified"], 6)
        self.assertEqual(c["scored"], 7)                                   # malformed counts as wrong, error excluded
        self.assertAlmostEqual(c["exact"], 3 / 7, places=3)
        self.assertAlmostEqual(c["lenient"], 5 / 7, places=3)
        self.assertAlmostEqual(c["new_family_fit"], 1 / 3, places=3)       # of 3 classified answers for new-profile sources, only Jung#1 answered none with the expected family
        self.assertEqual(c["none_answers"], 2)
        self.assertEqual(c["false_none"], 0)
        self.assertAlmostEqual(c["latency_p50"], 2.0)
        self.assertAlmostEqual(c["latency_max"], 9.0)                       # failures excluded from latency
        self.assertEqual(c["served_model_mismatch"], 1)
        self.assertEqual(c["tokens"]["completion_mean"], 10.0)
        self.assertEqual(c["tokens"]["reasoning_mean"], 4.0)
        self.assertEqual(c["tokens"]["with_usage"], 1)
        self.assertAlmostEqual(c["consistency"], 1 / 3, places=3)           # SQL same; Jung differs; AI Sec classified vs malformed differs; Ambiguous has one non-error answer (excluded)
        self.assertGreater(c["confidence_wrong_mean"], c["confidence_right_mean"] - 1)
        self.assertIn("brier", c)
        self.assertEqual(s["cells"]["m/medium"]["strata"]["clear-existing"]["lenient"], 1.0)
        self.assertIn("summary_table", s)
        self.assertIn("recommendation", s)

    def test_baseline_is_the_classifier_default(self):
        kc = kb.load_classifier()
        self.assertEqual(kb.BASELINE_CELL, f"{kc.DEFAULT_MODEL}/{kc.DEFAULT_EFFORT}")
        self.assertTrue((REPO / "docs/benchmarks/knowledge-classify/results-v1-2026-10-23.md").is_file())

    def test_recommendation_prefers_accuracy_then_failures_then_latency(self):
        rows = []
        for model, acc_profile in (("a", "software-engineering"), ("b", "philosophy")):
            for eff in ("medium", "high"):
                rows.append(result("edu/SQL", model=model, effort=eff, profile=acc_profile, latency=1.0 if eff == "medium" else 5.0))
                rows.append(result("edu/Ambiguous", model=model, effort=eff, profile="strategy"))
        s = kb.score_results(RUBRIC, rows)
        self.assertEqual(s["recommendation"]["cell"], "a/medium")
        self.assertIn("margin", s["recommendation"])


class Stub:
    def __init__(self):
        self.requests, self.fail_model = [], ""
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                stub.requests.append(body)
                if body["model"] == stub.fail_model:
                    self.send_response(503)
                    self.end_headers()
                    return
                user = body["messages"][-1]["content"]
                m = re.search(r'"source": "([^"]*)"', user)
                src = m.group(1) if m else ""
                content = {"edu/SQL": json.dumps({"profile": "software-engineering", "confidence": 0.9, "rationale": "sql", "topics": []}),
                           "edu/Jung": json.dumps({"profile": "none", "confidence": 0.8, "rationale": "jung", "topics": [],
                                                   "new_profile": {"name": "psychology", "purpose": "p"}})}.get(src, "nope")
                out = json.dumps({"model": body["model"] + "-served", "usage": {"completion_tokens": 7},
                                  "choices": [{"message": {"content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Catalog(unittest.TestCase):
    """The catalog resolves rubric sources by content hash and carries the production evidence."""

    def test_catalog_from_a_synthetic_library(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import knowledge_plan as kp
        tmp = Path(tempfile.mkdtemp())
        try:
            books = tmp / "books"
            (books / "edu" / "Deep Book (for Someone)").mkdir(parents=True)
            (books / "edu" / "Deep Book (for Someone)" / "b.md").write_text("# Deep\n\n" + "Chapter text about strategy. " * 200)
            (books / "edu" / "Other Book").mkdir()
            (books / "edu" / "Other Book" / "o.txt").write_text("Other text.\n")
            seeds = tmp / "profiles"
            for name in ("alpha", "beta"):
                (seeds / name).mkdir(parents=True)
                (seeds / name / "SOUL.md").write_text(f"You are the {name} specialist.\n")
                (seeds / name / "knowledge-sources.txt").write_text(f"# {name}\n")
            ident = kp.source_identity(books, "edu/Deep Book (for Someone)").source_id
            rubric = {"rubric_version": 1, "families": {"x": ["x"]}, "profiles": ["alpha", "beta"],
                      "sources": [{"source": "Deep Book", "source_id": ident, "stratum": "s",
                                   "expected": {"kind": "existing", "primary": "alpha"}}]}
            (tmp / "r.json").write_text(json.dumps(rubric))
            env = dict(os.environ, BOOKS_ROOT=str(books), KNOWLEDGE_ROOT=str(tmp / "knowledge"),
                       KNOWLEDGE_STAGING=str(tmp / "staging"), PROFILES_DIR=str(seeds),
                       KNOWLEDGE_IGNORE=str(tmp / "ignore.txt"), KNOWLEDGE_PRIVATE=str(tmp / "private.txt"),
                       HERMES_HOME=str(tmp / "hermes"), KNOWLEDGE_CLASSIFY_NO_DOTENV="1",
                       KNOWLEDGE_LIBRARY_EXEMPT_ROOT="off")
            (tmp / "ignore.txt").write_text("")
            p = subprocess.run([sys.executable, str(SCRIPT), "catalog", "--rubric", str(tmp / "r.json"),
                                "--out", str(tmp / "cat.json")], capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            cat = json.loads((tmp / "cat.json").read_text())
            self.assertEqual([x["name"] for x in cat["profiles"]], ["alpha", "beta"])
            src = cat["sources"][0]
            self.assertEqual((src["source"], src["library_path"]), ("Deep Book", "edu/Deep Book (for Someone)"))
            self.assertEqual(src["evidence"]["metadata"]["source"], "edu/Deep Book (for Someone)")
            self.assertIn("Chapter text about strategy", src["evidence"]["excerpt"])
            self.assertLess(len(src["evidence"]["excerpt"]), 3100)
            self.assertNotIn("has_text", src["evidence"]["metadata"])
            # A rubric id that no uncategorized source has is an error, not a guess.
            rubric["sources"][0]["source_id"] = "0" * 16
            (tmp / "r.json").write_text(json.dumps(rubric))
            p = subprocess.run([sys.executable, str(SCRIPT), "catalog", "--rubric", str(tmp / "r.json"),
                                "--out", str(tmp / "cat2.json")], capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(p.returncode, 1)
            self.assertIn("content hash", p.stderr)
            self.assertFalse((seeds / "alpha" / "knowledge-sources.txt").read_text().strip() != "# alpha")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stub = Stub()
        self.rubric = self.tmp / "rubric.json"
        self.rubric.write_text(json.dumps(RUBRIC))
        catalog = {"catalog_version": 1, "profiles": [{"name": n, "purpose": f"You are the {n} specialist.",
                                                       "entries": 1, "staged": 1, "titles": ["T"]}
                                                      for n in RUBRIC["profiles"]],
                   "sources": [{"source": s["source"], "source_id": s["source_id"],
                                "evidence": {"metadata": {"source": s["source"], "name": s["source"].split("/")[1]},
                                             "excerpt": "Outline:\n# One\n\nSample:\nBody " + s["source"]}}
                               for s in RUBRIC["sources"]]}
        self.catalog = self.tmp / "catalog.json"
        self.catalog.write_text(json.dumps(catalog))
        self.out = self.tmp / "out"
        self.env = dict(os.environ, KNOWLEDGE_CLASSIFY_BASE_URL=self.stub.url, CLIPROXY_API_KEY="test-key",
                        KNOWLEDGE_CLASSIFY_NO_DOTENV="1")

    def tearDown(self):
        self.stub.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_kb(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              env=self.env, timeout=120)

    def test_run_sends_the_production_prompt_per_cell_and_records_failures(self):
        self.stub.fail_model = "gpt-5.6-sol"
        p = self.run_kb("run", "--rubric", str(self.rubric), "--catalog", str(self.catalog), "--out", str(self.out),
                        "--models", "gpt-5.6-luna,gpt-5.6-sol", "--efforts", "medium,high", "--repeats", "2",
                        "--concurrency", "3", "--timeout", "30")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.stub.requests), 4 * 2 * 2 * 2)
        cells = {(r["model"], r["reasoning_effort"]) for r in self.stub.requests}
        self.assertEqual(cells, {("gpt-5.6-luna", "medium"), ("gpt-5.6-luna", "high"),
                                 ("gpt-5.6-sol", "medium"), ("gpt-5.6-sol", "high")})
        sysmsgs = {r["messages"][0]["content"] for r in self.stub.requests}
        self.assertEqual(len(sysmsgs), 1)
        kc_spec = importlib.util.spec_from_file_location("kc_for_bench", REPO / "scripts" / "knowledge-classify.py")
        kc = importlib.util.module_from_spec(kc_spec)
        sys.modules[kc_spec.name] = kc
        kc_spec.loader.exec_module(kc)
        self.assertEqual(sysmsgs.pop(), kc.SYSTEM_PROMPT)
        users = {r["messages"][1]["content"] for r in self.stub.requests if '"source": "edu/SQL"' in r["messages"][1]["content"]}
        self.assertEqual(len(users), 1, "identical catalog/prompt across cells")
        self.assertIn("- software-engineering: You are the software-engineering specialist.", users.pop())
        results = json.loads((self.out / "results.json").read_text())
        self.assertEqual(len(results["results"]), 32)
        errs = [r for r in results["results"] if r["status"] == "error"]
        self.assertEqual(len(errs), 16)
        self.assertTrue(all("503" in r["error"] for r in errs))
        ok = [r for r in results["results"] if r["model"] == "gpt-5.6-luna" and r["source"] == "edu/SQL"]
        self.assertEqual({r["profile"] for r in ok}, {"software-engineering"})
        self.assertEqual({r["model_served"] for r in ok}, {"gpt-5.6-luna-served"})
        self.assertEqual({r["usage"]["completion_tokens"] for r in ok}, {7})
        self.assertTrue(all("excerpt" not in r for r in results["results"]))
        self.assertNotIn("test-key", (self.out / "results.json").read_text())
        self.assertTrue((self.out / "scores.json").is_file())
        self.assertTrue((self.out / "report.md").is_file())
        self.assertIn("gpt-5.6-luna/medium", (self.out / "report.md").read_text())
        self.assertIn("recommend", (self.out / "report.md").read_text().lower())
        self.assertEqual(sorted(x.name for x in self.tmp.iterdir()), ["catalog.json", "out", "rubric.json"])

    def test_score_only_and_safety(self):
        p = self.run_kb("run", "--rubric", str(self.rubric), "--catalog", str(self.catalog), "--out", str(self.out),
                        "--models", "gpt-5.6-luna", "--efforts", "medium", "--repeats", "1", "--timeout", "30")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_kb("score", "--rubric", str(self.rubric), "--results", str(self.out / "results.json"),
                        "--out", str(self.out / "rescored"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.out / "rescored" / "scores.json").is_file())
        p = self.run_kb("run", "--rubric", str(self.rubric), "--catalog", str(self.catalog), "--out", str(self.out),
                        "--efforts", "low")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("effort", p.stderr)
        src = SCRIPT.read_text()
        for forbidden in ("knowledge-sources.txt", "append_manifest", "apply_item", "knowledge-stage.sh"):
            self.assertNotIn(forbidden, src, f"benchmark must never touch manifests: {forbidden}")

    def test_make_target_passes_arguments(self):
        p = subprocess.run(["make", "-n", "-C", str(REPO), "knowledge-benchmark", "RUBRIC=r.json", "OUT=o",
                            "CATALOG=c.json", "MODELS=gpt-5.6-luna", "EFFORTS=high", "REPEATS=1"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        for flag in ("--rubric", "--out", "--catalog", "--models", "--efforts", "--repeats"):
            self.assertIn(flag, p.stdout)
        p = subprocess.run(["make", "-n", "-C", str(REPO), "knowledge-benchmark"], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("--rubric", p.stdout)                                  # a default rubric is wired in


if __name__ == "__main__":
    unittest.main()
