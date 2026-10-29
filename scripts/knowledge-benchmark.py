#!/usr/bin/env python3
"""knowledge-benchmark.py — compare CLIProxyAPI models and reasoning efforts on the
book-classification prompt, against a versioned rubric built from an operator-reviewed plan.

    knowledge-benchmark.py catalog --rubric R --out catalog.json
        precompute the exact production evidence (scripts/knowledge-classify.py:
        build_evidence + load_profiles) for every rubric source once; the library is
        only read and books are copy-converted into the normal quarantine staging area.
    knowledge-benchmark.py run --rubric R --out DIR [--catalog FILE] [--models a,b]
        [--efforts medium,high] [--repeats 2] [--concurrency 4] [--timeout 120]
        [--sources N|stratified]
        send the identical catalog/prompt to every model x effort cell (only the
        request's model and top-level reasoning_effort differ), record every answer,
        failure and timeout (never invented), score and report.
    knowledge-benchmark.py score --rubric R --results results.json --out DIR
        re-score an existing results file.

Scoring per cell: exact and lenient profile fit, new-profile-family fit, none-answer
correctness, malformed / refused / unknown-profile answers, confidence calibration
(mean confidence on right vs wrong answers, Brier score), latency p50/p90/max, token
usage when the proxy returns it, requested vs served model, repeat consistency and
failure rate; the recommendation ranks cells by lenient fit, then exact fit, then
failure rate, then latency, and states the margin to the runner-up and to the
baseline cell (the classifier's current default model/effort).

Safety: this script has no code path that writes a plan, a book list, a profile or
data/knowledge. Everything it writes goes under --out. results.json carries request
metadata and sanitized answers only (never excerpts or keys); the catalog carries the
bounded excerpts and therefore belongs in the gitignored staging area, not in git.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as _dt
import hashlib
import importlib.util
import json
import math
import os
import re
import statistics
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge_plan as kp  # noqa: E402  (content-hash source identity)

RUBRIC_VERSION = 1
CATALOG_VERSION = 1
DEFAULT_MODELS = ("gpt-5.6-luna", "gpt-6.1-terra", "gpt-5.6-sol")
DEFAULT_EFFORTS = ("medium", "high")
BENCH_EFFORTS = ("medium", "high")            # no low effort in a benchmark
BASELINE_CELL = "gpt-6.1-terra/medium"      # the classifier default (DEFAULT_MODEL/DEFAULT_EFFORT)
KINDS = ("existing", "new", "either")
UNKNOWN_RE = re.compile(r"not one of the listed profiles")


class BenchmarkError(Exception):
    pass


def load_classifier():
    """The production classifier module (prompt, evidence builders, parser, backend)."""
    path = ROOT / "scripts" / "knowledge-classify.py"
    spec = importlib.util.spec_from_file_location("knowledge_classify_for_benchmark", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# --------------------------------------------------------------------------- #
# Rubric
# --------------------------------------------------------------------------- #
def family_of(name: str, families: Dict[str, Sequence[str]]) -> str:
    """The rubric family a proposed profile slug belongs to ('' when none matches).
    Keywords match whole dash-separated parts or prefixes of them (psych -> psychology)."""
    if not name:
        return ""
    parts = [p for p in re.split(r"[^a-z0-9]+", name.lower()) if p]
    for fam, keys in families.items():
        for k in keys:
            k = k.lower()
            if any(p == k or (len(k) >= 4 and p.startswith(k)) for p in parts):
                return fam
    return ""


def load_rubric(path: Path) -> Dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"cannot read rubric {path}: {exc}") from None
    if not isinstance(data, dict) or data.get("rubric_version") != RUBRIC_VERSION:
        raise BenchmarkError(f"rubric {path}: rubric_version must be {RUBRIC_VERSION}")
    families = data.get("families")
    profiles = data.get("profiles")
    sources = data.get("sources")
    if not isinstance(families, dict) or not all(isinstance(v, list) for v in families.values()):
        raise BenchmarkError("rubric: families must map family -> [keywords]")
    if not isinstance(profiles, list) or not all(isinstance(p, str) for p in profiles):
        raise BenchmarkError("rubric: profiles must be a list of candidate profile names")
    if not isinstance(sources, list) or not sources:
        raise BenchmarkError("rubric: sources must be a non-empty list")
    seen = set()
    for n, s in enumerate(sources, 1):
        where = f"rubric source {n}"
        if not isinstance(s, dict) or not isinstance(s.get("source"), str) or not s["source"]:
            raise BenchmarkError(f"{where}: needs a source path")
        if s["source"] in seen:
            raise BenchmarkError(f"{where}: {s['source']!r} is listed twice")
        seen.add(s["source"])
        e = s.get("expected")
        if not isinstance(e, dict) or e.get("kind") not in KINDS:
            raise BenchmarkError(f"{where}: expected.kind must be one of {', '.join(KINDS)}")
        if e["kind"] in ("existing", "either"):
            if e.get("primary") not in profiles:
                raise BenchmarkError(f"{where}: expected.primary {e.get('primary')!r} is not a candidate profile")
        if e["kind"] in ("new", "either"):
            if e.get("family") not in families:
                raise BenchmarkError(f"{where}: expected.family {e.get('family')!r} is not a rubric family")
        for key in ("acceptable",):
            for p in e.get(key) or []:
                if p not in profiles:
                    raise BenchmarkError(f"{where}: {key} names unknown profile {p!r}")
        for f in e.get("acceptable_families") or []:
            if f not in families:
                raise BenchmarkError(f"{where}: acceptable_families names unknown family {f!r}")
        s.setdefault("stratum", "unlabelled")
    return data


def select_sources(rubric: Dict[str, object], spec: str) -> List[Dict[str, object]]:
    sources = list(rubric["sources"])
    if not spec or spec == "all":
        return sources
    if spec == "stratified":
        picked = [s for s in sources if s.get("sample") is True]
        if not picked:
            raise BenchmarkError("--sources stratified: no rubric source carries \"sample\": true")
        return picked
    try:
        n = int(spec)
    except ValueError:
        raise BenchmarkError("--sources must be all, stratified or a number") from None
    if n < 1:
        raise BenchmarkError("--sources must be at least 1")
    return sources[:n]


# --------------------------------------------------------------------------- #
# Catalog: the exact production evidence, computed once
# --------------------------------------------------------------------------- #
def build_catalog(rubric: Dict[str, object], kc) -> Dict[str, object]:
    args = kc.build_parser().parse_args([])
    s = kc.settings_from(args)
    kc.check_roots(s)
    profiles = kc.load_profiles(s)
    names = sorted(profiles)
    if sorted(rubric["profiles"]) != names:
        raise BenchmarkError("the rubric's candidate profiles differ from the book-backed profiles the classifier "
                             f"would list now: rubric {sorted(rubric['profiles'])} vs live {names}. Regenerate "
                             "the rubric or use the matching checkout.")
    # Rubric sources are matched to library paths by content hash (source_id), never by title:
    # titles in the rubric are sanitized and the library may carry watermarked folder names.
    by_id: Dict[str, str] = {}
    for rel in kc.uncategorized(s):
        try:
            by_id[kp.source_identity(s.books, rel).source_id] = rel
        except kp.PlanError:
            continue
    out_sources = []
    for n, entry in enumerate(rubric["sources"], 1):
        sid = str(entry.get("source_id") or "")
        rel = by_id.get(sid)
        if rel is None:
            raise BenchmarkError(f"rubric source {entry['source']!r} (id {sid}) is not an uncategorized library source "
                                 "with that content hash; the library changed or the book was assigned since the rubric")
        print(f"[knowledge-benchmark] evidence {n}/{len(rubric['sources'])}: {entry['source']}", file=sys.stderr)
        src = kc.safe_rel(s, rel)
        staged, meta, excerpt = kc.build_evidence(s, rel, src)
        meta = {k: v for k, v in meta.items() if k != "has_text"}
        out_sources.append({"source": entry["source"], "source_id": sid, "library_path": rel,
                            "evidence": {"metadata": meta, "excerpt": excerpt},
                            "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest()[:16],
                            "stage_problems": staged.problems})
    return {"catalog_version": CATALOG_VERSION, "generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "library": str(s.books), "excerpt_chars": s.excerpt_chars,
            "profiles": [{"name": p.name, "purpose": p.purpose, "entries": p.entries, "staged": p.staged,
                          "titles": list(p.titles)} for p in profiles.values()],
            "sources": out_sources}


def load_catalog(path: Path) -> Dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"cannot read catalog {path}: {exc}") from None
    if not isinstance(data, dict) or data.get("catalog_version") != CATALOG_VERSION:
        raise BenchmarkError(f"catalog {path}: catalog_version must be {CATALOG_VERSION}")
    for key in ("profiles", "sources"):
        if not isinstance(data.get(key), list):
            raise BenchmarkError(f"catalog {path}: {key} must be a list")
    return data


def catalog_digest(catalog: Dict[str, object]) -> str:
    body = json.dumps({"profiles": catalog["profiles"], "sources": catalog["sources"]}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def parse_cells(models: str, efforts: str) -> List[Tuple[str, str]]:
    ms = [m.strip() for m in models.split(",") if m.strip()]
    es = [e.strip().lower() for e in efforts.split(",") if e.strip()]
    if not ms or not es:
        raise BenchmarkError("--models and --efforts must not be empty")
    bad = [e for e in es if e not in BENCH_EFFORTS]
    if bad:
        raise BenchmarkError(f"--efforts: {', '.join(bad)} not allowed; the benchmark runs {', '.join(BENCH_EFFORTS)} only")
    return [(m, e) for m in ms for e in es]


def run_cells(kc, catalog: Dict[str, object], sources: Sequence[str], cells: Sequence[Tuple[str, str]],
              repeats: int, concurrency: int, timeout: float, base_url: str, key: str,
              progress=None) -> List[Dict[str, object]]:
    profiles = {p["name"]: kc.Profile(p["name"], p.get("purpose", ""), Path("/nonexistent"), int(p.get("entries", 0)),
                                      int(p.get("staged", 0)), list(p.get("titles") or []))
                for p in catalog["profiles"]}
    allowed = list(profiles)
    by_source = {s["source"]: s for s in catalog["sources"]}
    missing = [s for s in sources if s not in by_source]
    if missing:
        raise BenchmarkError(f"catalog lacks evidence for: {', '.join(missing[:5])}" + (" ..." if len(missing) > 5 else ""))
    messages_for = {s: kc.build_messages(profiles, kc.Evidence(by_source[s]["evidence"]["metadata"],
                                                                by_source[s]["evidence"]["excerpt"]))
                    for s in sources}
    backends = {cell: kc.Backend("cliproxy", base_url, cell[0], key=key, timeout=timeout, effort=cell[1])
                for cell in cells}
    tasks = [(r, src, cell) for r in range(1, repeats + 1) for src in sources for cell in cells]
    results: List[Dict[str, object]] = []
    lock = threading.Lock()

    def one(task):
        r, src, cell = task
        b = backends[cell]
        row: Dict[str, object] = {"source": src, "source_id": by_source[src].get("source_id", ""),
                                  "model": cell[0], "effort": cell[1], "repeat": r,
                                  "requested_at": _dt.datetime.now().isoformat(timespec="seconds")}
        try:
            done = b.complete_full(messages_for[src])
        except kc.ClassifierError as exc:
            row.update(status="error", error=str(exc), latency_s=None, profile="", confidence=None,
                       new_profile_name="", model_served="", usage={}, rationale="")
            return row
        v = kc.parse_verdict(done.content, allowed)
        row.update(status=v.status, error="", latency_s=done.latency_s, profile=v.profile,
                   confidence=v.confidence if v.status == "classified" else None,
                   new_profile_name=str(v.new_profile.get("name", "")) if v.new_profile else "",
                   model_served=done.model_served, usage=done.usage, rationale=v.rationale,
                   topics=list(v.topics), answer_chars=len(done.content))
        return row

    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(one, t) for t in tasks]
        for n, fut in enumerate(cf.as_completed(futures), 1):
            row = fut.result()
            with lock:
                results.append(row)
            if progress:
                progress(n, len(tasks), row)
    results.sort(key=lambda r: (r["repeat"], sources.index(r["source"]), cells.index((r["model"], r["effort"]))))
    return results


# --------------------------------------------------------------------------- #
# Scorer
# --------------------------------------------------------------------------- #
def _pct(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    vs = sorted(values)
    k = max(1, math.ceil(p * len(vs)))
    return vs[k - 1]


def _mean(values: List[float]) -> Optional[float]:
    return round(statistics.fmean(values), 4) if values else None


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def judge(entry: Dict[str, object], row: Dict[str, object], families: Dict[str, Sequence[str]]) -> Dict[str, object]:
    """exact / lenient / family / none bookkeeping for one classified answer."""
    e = entry["expected"]
    kind = e["kind"]
    profile = row.get("profile") or ""
    none = profile == ""
    fam = family_of(row.get("new_profile_name") or "", families)
    acceptable = set(e.get("acceptable") or [])
    acc_fams = set(e.get("acceptable_families") or [])
    exact = lenient = False
    if kind in ("existing", "either"):
        exact = exact or profile == e["primary"]
        lenient = lenient or exact or profile in acceptable or (none and fam in acc_fams)
    if kind in ("new", "either"):
        fam_ok = none and fam == e["family"]
        exact = exact or fam_ok
        lenient = lenient or fam_ok or (none and fam in acc_fams) or profile in acceptable
    false_none = none and kind == "existing" and fam not in acc_fams
    family_fit = kind in ("new", "either") and none and fam == e["family"]
    return {"exact": exact, "lenient": lenient, "none": none, "family": fam, "false_none": false_none,
            "family_fit": family_fit, "wants_family": kind in ("new", "either")}


def score_results(rubric: Dict[str, object], results: Sequence[Dict[str, object]]) -> Dict[str, object]:
    families = rubric["families"]
    by_source = {s["source"]: s for s in rubric["sources"]}
    cells: Dict[str, Dict[str, object]] = {}
    per_cell_rows: Dict[str, List[Dict[str, object]]] = {}
    for r in results:
        if r.get("source") not in by_source:
            continue
        per_cell_rows.setdefault(f"{r['model']}/{r['effort']}", []).append(r)
    for cell, rows in per_cell_rows.items():
        c: Dict[str, object] = {"requests": len(rows), "failures": 0, "malformed": 0, "refused": 0, "unknown_profile": 0,
                                "classified": 0, "scored": 0, "exact_n": 0, "lenient_n": 0, "none_answers": 0,
                                "false_none": 0, "served_model_mismatch": 0}
        lat: List[float] = []
        conf_right: List[float] = []
        conf_wrong: List[float] = []
        brier: List[float] = []
        comp_tok: List[float] = []
        reas_tok: List[float] = []
        fam_total = fam_fit = 0
        strata: Dict[str, Dict[str, int]] = {}
        answers: Dict[str, List[Tuple[str, str, str]]] = {}
        errors: Dict[str, int] = {}
        for r in rows:
            entry = by_source[r["source"]]
            st = strata.setdefault(str(entry.get("stratum")), {"scored": 0, "exact": 0, "lenient": 0, "failures": 0})
            if r.get("status") == "error":
                c["failures"] += 1
                st["failures"] += 1
                errors[str(r.get("error"))[:80]] = errors.get(str(r.get("error"))[:80], 0) + 1
                continue
            if isinstance(r.get("latency_s"), (int, float)):
                lat.append(float(r["latency_s"]))
            if r.get("model_served") and r["model_served"] != r["model"]:
                c["served_model_mismatch"] += 1
            c["scored"] += 1
            st["scored"] += 1
            if r.get("status") != "classified":
                c["malformed" if r.get("status") == "malformed" else "refused"] += 1
                if UNKNOWN_RE.search(str(r.get("rationale") or "")):
                    c["unknown_profile"] += 1
                answers.setdefault(r["source"], []).append(("<" + str(r.get("status")) + ">", "", ""))
                continue
            c["classified"] += 1
            u = r.get("usage") or {}
            if isinstance(u.get("completion_tokens"), int):
                comp_tok.append(u["completion_tokens"])
            if isinstance(u.get("reasoning_tokens"), int):
                reas_tok.append(u["reasoning_tokens"])
            j = judge(entry, r, families)
            answers.setdefault(r["source"], []).append((r.get("profile") or "", j["family"], ""))
            c["exact_n"] += j["exact"]
            c["lenient_n"] += j["lenient"]
            st["exact"] += j["exact"]
            st["lenient"] += j["lenient"]
            c["none_answers"] += j["none"]
            c["false_none"] += j["false_none"]
            if j["wants_family"]:
                fam_total += 1
                fam_fit += j["family_fit"]
            conf = r.get("confidence")
            if isinstance(conf, (int, float)):
                (conf_right if j["lenient"] else conf_wrong).append(float(conf))
                brier.append((float(conf) - (1.0 if j["lenient"] else 0.0)) ** 2)
        scored = c["scored"]
        c["exact"] = round(c["exact_n"] / scored, 4) if scored else None
        c["lenient"] = round(c["lenient_n"] / scored, 4) if scored else None
        c["lenient_ci95"] = wilson(c["lenient_n"], scored)
        c["exact_ci95"] = wilson(c["exact_n"], scored)
        c["failure_rate"] = round(c["failures"] / len(rows), 4) if rows else None
        c["new_family_fit"] = round(fam_fit / fam_total, 4) if fam_total else None
        c["new_family_n"] = fam_total
        c["latency_p50"] = _pct(lat, 0.5)
        c["latency_p90"] = _pct(lat, 0.9)
        c["latency_max"] = max(lat) if lat else None
        c["latency_mean"] = _mean(lat)
        c["tokens"] = {"completion_mean": _mean(comp_tok), "reasoning_mean": _mean(reas_tok), "with_usage": len(comp_tok)}
        c["confidence_right_mean"] = _mean(conf_right)
        c["confidence_wrong_mean"] = _mean(conf_wrong)
        c["brier"] = _mean(brier)
        pairs = [v for v in answers.values() if len(v) >= 2]
        c["consistency"] = round(sum(1 for v in pairs if len(set(v)) == 1) / len(pairs), 4) if pairs else None
        c["consistency_n"] = len(pairs)
        c["errors"] = errors
        c["strata"] = {k: {"scored": v["scored"], "failures": v["failures"],
                           "exact": round(v["exact"] / v["scored"], 4) if v["scored"] else None,
                           "lenient": round(v["lenient"] / v["scored"], 4) if v["scored"] else None}
                       for k, v in sorted(strata.items())}
        cells[cell] = c
    ordered = sorted(cells.items(), key=lambda kv: (-(kv[1]["lenient"] or 0), -(kv[1]["exact"] or 0),
                                                    kv[1]["failure_rate"] or 0, kv[1]["latency_p50"] or 0,
                                                    kv[1]["latency_mean"] or 0, kv[0]))
    summary = [{"cell": k, "lenient": v["lenient"], "exact": v["exact"], "new_family_fit": v["new_family_fit"],
                "false_none": v["false_none"], "malformed": v["malformed"] + v["refused"], "failures": v["failures"],
                "latency_p50": v["latency_p50"], "latency_p90": v["latency_p90"], "latency_max": v["latency_max"],
                "consistency": v["consistency"], "brier": v["brier"], "served_model_mismatch": v["served_model_mismatch"],
                "requests": v["requests"]} for k, v in ordered]
    rec: Dict[str, object] = {"cell": "", "reason": "no scored cell"}
    if ordered:
        best, bv = ordered[0]
        runner = ordered[1] if len(ordered) > 1 else None
        base = cells.get(BASELINE_CELL)
        rec = {"cell": best, "lenient": bv["lenient"], "exact": bv["exact"], "failure_rate": bv["failure_rate"],
               "latency_p50": bv["latency_p50"],
               "margin": round((bv["lenient"] or 0) - (runner[1]["lenient"] or 0), 4) if runner else None,
               "runner_up": runner[0] if runner else "",
               "baseline": BASELINE_CELL if base else "",
               "margin_vs_baseline": round((bv["lenient"] or 0) - (base["lenient"] or 0), 4) if base else None,
               "ci_overlaps_runner_up": bool(runner and bv["lenient_ci95"][0] <= runner[1]["lenient_ci95"][1]),
               "ranking": "lenient fit, then exact fit, then failure rate, then latency p50 and mean"}
        rec["reason"] = (f"{best} ranks first by {rec['ranking']}"
                         + (f"; margin over {runner[0]}: {rec['margin']:+.3f} lenient" if runner else "")
                         + (" (95% intervals overlap: the difference is not statistically separable at this sample size)"
                            if rec["ci_overlaps_runner_up"] else ""))
    return {"scored_at": _dt.datetime.now().isoformat(timespec="seconds"), "rubric_version": rubric.get("rubric_version"),
            "rubric_sources": len(rubric["sources"]), "cells": dict(ordered), "summary_table": summary,
            "recommendation": rec}


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def fmt(v, kind="f"):
    if v is None:
        return "-"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "s":
        return f"{v:.1f}s"
    if kind == "f":
        return f"{v:.3f}" if isinstance(v, float) else str(v)
    return str(v)


def render_report(scores: Dict[str, object], meta: Dict[str, object]) -> str:
    L: List[str] = []
    L.append(f"# Classifier model/effort benchmark — {meta.get('run_id', '')}")
    L.append("")
    L.append(f"Rubric version {scores.get('rubric_version')} ({scores.get('rubric_sources')} sources; "
             f"{meta.get('sources_run', '?')} run); catalog digest `{meta.get('catalog_digest', '')}`; "
             f"cells {', '.join(meta.get('cells') or [])}; repeats {meta.get('repeats')}; "
             f"concurrency {meta.get('concurrency')}; timeout {meta.get('timeout')}s; "
             f"{meta.get('total_requests', '?')} requests; started {meta.get('started', '')}, finished {meta.get('finished', '')}.")
    L.append("")
    L.append("Identical system prompt, profile catalog and bounded evidence in every cell (the production classifier's "
             "builders); only the request's `model` and top-level `reasoning_effort` differ. Scores are against the "
             "rubric's expected profiles; `lenient` also accepts the rubric's acceptable profiles / families. "
             "Malformed or refused answers count as wrong; transport failures (timeouts, HTTP errors) are excluded "
             "from fit scores and reported separately.")
    L.append("")
    L.append("## Cells (ranked)")
    L.append("")
    L.append("| cell | lenient | exact | 95% CI (lenient) | new-family fit | false none | malformed | failures | "
             "p50 | p90 | max | consistency | Brier | served≠requested | requests |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in scores["summary_table"]:
        c = scores["cells"][row["cell"]]
        ci = c.get("lenient_ci95") or (0, 0)
        L.append(f"| {row['cell']} | {fmt(row['lenient'], 'pct')} | {fmt(row['exact'], 'pct')} | "
                 f"{ci[0] * 100:.0f}–{ci[1] * 100:.0f}% | {fmt(row['new_family_fit'], 'pct')} | {row['false_none']} | "
                 f"{row['malformed']} | {row['failures']} | {fmt(row['latency_p50'], 's')} | {fmt(row['latency_p90'], 's')} | "
                 f"{fmt(row['latency_max'], 's')} | {fmt(row['consistency'], 'pct')} | {fmt(row['brier'])} | "
                 f"{row['served_model_mismatch']} | {row['requests']} |")
    L.append("")
    L.append("## Tokens and calibration")
    L.append("")
    L.append("| cell | completion tokens (mean) | reasoning tokens (mean) | answers with usage | confidence when right | confidence when wrong |")
    L.append("|---|---|---|---|---|---|")
    for cell, c in scores["cells"].items():
        t = c["tokens"]
        L.append(f"| {cell} | {fmt(t['completion_mean'])} | {fmt(t['reasoning_mean'])} | {t['with_usage']} | "
                 f"{fmt(c['confidence_right_mean'])} | {fmt(c['confidence_wrong_mean'])} |")
    L.append("")
    strata = sorted({s for c in scores["cells"].values() for s in c["strata"]})
    if strata:
        L.append("## Lenient fit per stratum")
        L.append("")
        L.append("| cell | " + " | ".join(strata) + " |")
        L.append("|---|" + "---|" * len(strata))
        for cell, c in scores["cells"].items():
            L.append(f"| {cell} | " + " | ".join(
                f"{fmt(c['strata'][s]['lenient'], 'pct')} ({c['strata'][s]['scored']})" if s in c["strata"] else "-"
                for s in strata) + " |")
        L.append("")
    errs = {cell: c["errors"] for cell, c in scores["cells"].items() if c["errors"]}
    if errs:
        L.append("## Failures")
        L.append("")
        for cell, e in errs.items():
            L.append(f"- {cell}: " + "; ".join(f"{k} ×{v}" for k, v in e.items()))
        L.append("")
    rec = scores["recommendation"]
    L.append("## Recommendation")
    L.append("")
    L.append(f"**{rec.get('cell') or 'none'}** — {rec.get('reason', '')}.")
    if rec.get("baseline"):
        L.append(f"Margin versus the baseline {rec['baseline']}: {rec['margin_vs_baseline']:+.3f} lenient.")
    L.append("")
    L.append("Caveats: the rubric encodes the operator's review (subjective choices are documented in it); the sample "
             "is one library; confidence is self-reported; latency depends on proxy load at run time. Change the "
             "default only when the margin is outside the overlapping intervals or the cheaper cell is not worse.")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_out() -> Path:
    staging = Path(os.environ.get("KNOWLEDGE_STAGING", str(ROOT / "data" / "knowledge-staging")))
    return staging / "benchmarks" / _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def cmd_catalog(args) -> int:
    rubric = load_rubric(Path(args.rubric))
    kc = load_classifier()
    catalog = build_catalog(rubric, kc)
    write_json(Path(args.out), catalog)
    print(f"catalog: {args.out} ({len(catalog['sources'])} sources, {len(catalog['profiles'])} profiles, "
          f"digest {catalog_digest(catalog)})")
    return 0


def cmd_score(args) -> int:
    rubric = load_rubric(Path(args.rubric))
    try:
        data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"cannot read results {args.results}: {exc}") from None
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        raise BenchmarkError("results must be a list or {results: [...]}")
    out = Path(args.out)
    scores = score_results(rubric, results)
    write_json(out / "scores.json", scores)
    meta = data.get("run", {}) if isinstance(data, dict) else {}
    (out / "report.md").write_text(render_report(scores, meta), encoding="utf-8")
    print(f"scores: {out / 'scores.json'}\nreport: {out / 'report.md'}")
    print(f"recommendation: {scores['recommendation'].get('cell')} — {scores['recommendation'].get('reason')}")
    return 0


def cmd_run(args) -> int:
    rubric = load_rubric(Path(args.rubric))
    cells = parse_cells(args.models, args.efforts)
    if args.repeats < 1 or args.concurrency < 1 or args.timeout <= 0:
        raise BenchmarkError("--repeats and --concurrency must be at least 1 and --timeout positive")
    kc = load_classifier()
    out = Path(args.out) if args.out else default_out()
    out.mkdir(parents=True, exist_ok=True)
    catalog_path = Path(args.catalog) if args.catalog and args.catalog != "build" else out / "catalog.json"
    if args.catalog == "build" or not catalog_path.is_file():
        print(f"[knowledge-benchmark] building the evidence catalog from the library into {catalog_path}", file=sys.stderr)
        catalog = build_catalog(rubric, kc)
        write_json(catalog_path, catalog)
    else:
        catalog = load_catalog(catalog_path)
    chosen = select_sources(rubric, args.sources)
    sources = [s["source"] for s in chosen]
    port = kc.env_value("CLIPROXY_PORT", "8317")
    base_url = os.environ.get("KNOWLEDGE_CLASSIFY_BASE_URL", f"http://127.0.0.1:{port}/v1")
    key = kc.env_value("CLIPROXY_API_KEY")
    started = _dt.datetime.now().isoformat(timespec="seconds")
    total = len(sources) * len(cells) * args.repeats
    print(f"[knowledge-benchmark] {len(sources)} sources x {len(cells)} cells x {args.repeats} repeats = {total} "
          f"requests to {base_url} (concurrency {args.concurrency}, timeout {args.timeout}s); output {out}", file=sys.stderr)
    meta = {"run_id": out.name, "started": started, "rubric": str(args.rubric), "rubric_version": rubric.get("rubric_version"),
            "catalog": str(catalog_path), "catalog_digest": catalog_digest(catalog), "base_url": base_url,
            "cells": [f"{m}/{e}" for m, e in cells], "repeats": args.repeats, "concurrency": args.concurrency,
            "timeout": args.timeout, "sources_run": len(sources), "sources_spec": args.sources, "total_requests": total}
    partial: List[Dict[str, object]] = []

    def progress(n, total_n, row):
        partial.append(row)
        tag = row["status"] if row["status"] != "error" else f"error: {row['error']}"
        print(f"[knowledge-benchmark] {n}/{total_n} {row['model']}/{row['effort']} r{row['repeat']} "
              f"{row['source'][:60]} -> {tag} {row.get('profile') or row.get('new_profile_name') or ''} "
              f"{'' if row.get('latency_s') is None else str(row['latency_s']) + 's'}", file=sys.stderr)
        if n % 10 == 0:
            write_json(out / "results.json", {"run": dict(meta, partial=True), "results": list(partial)})

    results = run_cells(kc, catalog, sources, cells, args.repeats, args.concurrency, args.timeout, base_url, key, progress)
    meta["finished"] = _dt.datetime.now().isoformat(timespec="seconds")
    write_json(out / "results.json", {"run": meta, "results": results})
    scores = score_results(rubric, results)
    write_json(out / "scores.json", scores)
    (out / "report.md").write_text(render_report(scores, meta), encoding="utf-8")
    print(f"results: {out / 'results.json'}\nscores: {out / 'scores.json'}\nreport: {out / 'report.md'}")
    for row in scores["summary_table"]:
        print(f"  {row['cell']:<24} lenient {fmt(row['lenient'], 'pct'):>6}  exact {fmt(row['exact'], 'pct'):>6}  "
              f"failures {row['failures']}  p50 {fmt(row['latency_p50'], 's')}")
    print(f"recommendation: {scores['recommendation'].get('cell')} — {scores['recommendation'].get('reason')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="knowledge-benchmark", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("catalog", help="precompute the production evidence for every rubric source")
    c.add_argument("--rubric", required=True)
    c.add_argument("--out", required=True, help="catalog JSON to write (keep it out of git: it carries excerpts)")
    c.set_defaults(func=cmd_catalog)
    r = sub.add_parser("run", help="run every model x effort cell and score it")
    r.add_argument("--rubric", required=True)
    r.add_argument("--out", help="output folder (default: <staging>/benchmarks/<timestamp>)")
    r.add_argument("--catalog", help="evidence catalog JSON, or 'build' to recompute it (default: <out>/catalog.json, built when missing)")
    r.add_argument("--models", default=",".join(DEFAULT_MODELS))
    r.add_argument("--efforts", default=",".join(DEFAULT_EFFORTS))
    r.add_argument("--repeats", type=int, default=2)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--timeout", type=float, default=120.0, help="per-request client timeout in seconds (allows thinking)")
    r.add_argument("--sources", default="all", help="all | stratified (rubric entries with sample: true) | <n> first entries")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("score", help="re-score an existing results.json")
    s.add_argument("--rubric", required=True)
    s.add_argument("--results", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_score)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BenchmarkError as exc:
        print(f"[knowledge-benchmark][error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
