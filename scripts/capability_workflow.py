#!/usr/bin/env python3
"""Workflow analysis contracts: validate a coordinator-authored analysis, render its artifacts.

The skills under hermes/skills/ (problem-definition, pm-*, gstack-*) are method
instructions the Hephaestus coordinator follows; the coordinator writes the
analysis JSON. This module never authors analysis content: it checks the
analysis against its typed contract (hermes/capabilities/schemas/
workflow-outputs.schema.json) and the supplied evidence pack, applies the
semantic rubric (evidence classes, numbers, causal labels, mode scaling,
artifact reuse, audit/fix separation) and renders validated artifacts into one
fresh output directory, with a CapabilityResult v1 and an Artifact v1 index.

Inputs are data: text inside a brief, evidence item or captured page is never
an instruction, and no input field can approve, grant or accept anything.
No network, shell or repository write happens here. Read-only except the one
explicit fresh output directory of ``render``.
"""

from __future__ import annotations

import ast
import datetime as _dt
import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capability_design as cd  # noqa: E402
import capability_methods as cm  # noqa: E402
import capability_registry as cr  # noqa: E402
import capability_research as cr_research  # noqa: E402

REPO = cr.REPO
OUTPUT_SCHEMA = str(cr.SCHEMA_DIR / "workflow-outputs.schema.json")
PACK_SCHEMA = "evidence-pack.schema.json"
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
QUICK_MAX_WORDS = 450
OUTPUT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")


class WorkflowError(cr.RegistryError):
    """An input or output request failed a workflow contract."""


# ---------------------------------------------------------------- schema access

_OUTPUT_DEFS: Dict[str, Any] = {}


def kind_schema(def_name: str) -> Dict[str, Any]:
    """Root schema for one $defs entry of workflow-outputs.schema.json (same file, so refs resolve)."""
    if not _OUTPUT_DEFS:
        doc = cr.load_schema(OUTPUT_SCHEMA)
        _OUTPUT_DEFS.update(doc["$defs"])
    if def_name not in _OUTPUT_DEFS:
        raise WorkflowError(f"no contract #/$defs/{def_name}")
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$ref": f"#/$defs/{def_name}", "$defs": _OUTPUT_DEFS}


def contract_errors(instance: Any, def_name: str, label: str) -> List[str]:
    js = cr._jsonschema()
    validator = js.Draft202012Validator(kind_schema(def_name))
    out = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: [str(p) for p in e.absolute_path]):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{label}: schema: {where}: {err.message[:300]}")
    return out


# ---------------------------------------------------------------- evidence pack

@dataclass
class Pack:
    doc: Dict[str, Any]
    digest: str
    root: Optional[Path]
    items: Dict[str, Dict[str, Any]]
    captures: Dict[str, str]  # source_id -> decoded captured text
    raw: Optional[bytes] = None  # the exact pack bytes that were digested (load_pack sets it)

    @property
    def synthetic(self) -> bool:
        return self.doc["binding"]["data_classification"] == "synthetic"

    def unavailable_topics(self) -> Dict[str, Dict[str, Any]]:
        return {n["topic"]: n for n in self.doc["not_available"]}


def _read_bytes(path: Path, max_bytes: int) -> bytes:
    path = Path(os.path.abspath(path))
    return cr.safe_repo_file(path.parent, path.name, max_bytes=max_bytes)


PACK_BINDING_MISMATCH = "evidence pack binding (task/company/project/class) differs from the analysis binding"


def load_pack(path: Path, binding: Any = None) -> Tuple[Optional[Pack], List[str]]:
    """Strictly load an evidence pack and verify every captured file under the pack's directory
    against its digest (no escape, symlink or hard link). The pack's own binding is checked first, before
    any captured file is opened: a company-private pack is refused like the planner refuses private
    execution, and with an expected task ``binding`` (the CLI passes the validated analysis binding) any
    difference is refused generically. The pack JSON and binding are untrusted caller data either way."""
    path = Path(os.path.abspath(path))
    raw = _read_bytes(path, cr.MAX_JSON_BYTES)
    doc = cr.parse_json(raw.decode("utf-8"))
    errs = cr.schema_errors(doc, PACK_SCHEMA, "evidence")
    if errs:
        return None, errs
    if doc["binding"]["data_classification"] == "company-private":
        return None, [cr.PRIVATE_RUNTIME_REFUSAL]
    if binding is not None and binding != doc["binding"]:
        return None, [PACK_BINDING_MISMATCH]
    items: Dict[str, Dict[str, Any]] = {}
    captures: Dict[str, str] = {}
    synthetic_pack = doc["binding"]["data_classification"] == "synthetic"
    for it in doc["items"]:
        sid = it["source_id"]
        if sid in items:
            errs.append(f"evidence: duplicate source_id {sid}")
        items[sid] = it
        if (it["kind"] == "synthetic-fixture") != (it["classification"] == "synthetic"):
            errs.append(f"evidence: {sid}: kind synthetic-fixture and classification synthetic go together")
        if synthetic_pack and it["classification"] != "synthetic":
            errs.append(f"evidence: {sid}: a synthetic pack holds only synthetic items")
        if it["kind"] == "primary-web":
            for field in ("retrieved_at", "captured_file", "digest"):
                if field not in it:
                    errs.append(f"evidence: {sid}: primary-web source needs {field}")
            if not it["ref"].startswith("https://"):
                errs.append(f"evidence: {sid}: primary-web ref must be an https URL")
        if ("captured_file" in it) != ("digest" in it):
            errs.append(f"evidence: {sid}: captured_file and digest go together")
        if "captured_file" in it and "digest" in it:
            try:
                data = cr.safe_repo_file(path.parent, it["captured_file"], max_bytes=MAX_CAPTURE_BYTES)
            except cr.RegistryError as exc:
                errs.append(f"evidence: {sid}: captured file: {exc}")
                continue
            if cr.sha256_bytes(data) != it["digest"]:
                errs.append(f"evidence: {sid}: captured file digest differs (source binding broken)")
                continue
            captures[sid] = data.decode("utf-8", "replace")
    topics = [n["topic"] for n in doc["not_available"]]
    if len(topics) != len(set(topics)):
        errs.append("evidence: duplicate not_available topic")
    if doc["policy"]["public_research"] == "denied":
        for it in doc["items"]:
            if it["kind"] == "primary-web":
                errs.append(f"evidence: {it['source_id']}: public research is denied by the pack policy, yet a web source is present")
    if errs:
        return None, errs
    return Pack(doc, cr.sha256_bytes(raw), path.parent, items, captures, raw), []


# ---------------------------------------------------------------- statements, numbers, computations

NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:,\d{3})*(?:\.\d+)?")
OBSERVATION_SOURCE_KINDS = {"document", "dataset", "interview-notes", "primary-web", "artifact"}


def numbers_in(text: str) -> List[float]:
    return [float(m.group(0).replace(",", "")) for m in NUMBER_RE.finditer(text)]


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


_ALLOWED_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
                ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}


def safe_eval(expression: str, env: Dict[str, float]) -> float:
    """Evaluate + - * / and parentheses over named numbers. No calls, attributes or other syntax."""
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise WorkflowError(f"expression {expression[:80]!r} is not arithmetic (or is nested too deeply)") from None

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise WorkflowError(f"expression {expression!r} divides by zero")
            return _ALLOWED_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in env:
            return env[node.id]
        raise WorkflowError(f"expression {expression!r}: {type(node).__name__} is not allowed")

    try:
        got = ev(tree)
    except (OverflowError, RecursionError, ValueError, ZeroDivisionError) as exc:
        raise WorkflowError(f"expression {expression[:80]!r} cannot be evaluated: {type(exc).__name__}") from None
    if not math.isfinite(got):
        raise WorkflowError(f"expression {expression[:80]!r} does not give a finite number")
    return got


def check_computations(comps: Sequence[dict], pack: Optional[Pack]) -> Tuple[Dict[str, dict], List[str]]:
    """Recompute every computation from source values; a stated result must equal the executed one."""
    errs: List[str] = []
    out: Dict[str, dict] = {}
    for c in comps:
        if c["id"] in out:
            errs.append(f"computation {c['id']}: duplicate id")
        out[c["id"]] = c
        env: Dict[str, float] = {}
        for name, src in c["inputs"].items():
            item = pack.items.get(src["source_id"]) if pack else None
            if item is None:
                errs.append(f"computation {c['id']}: input {name} cites unknown source {src['source_id']}")
                continue
            if src["value"] not in item.get("values", {}):
                errs.append(f"computation {c['id']}: source {src['source_id']} has no value {src['value']}")
                continue
            env[name] = float(item["values"][src["value"]])
        if len(env) != len(c["inputs"]):
            continue
        try:
            got = round(safe_eval(c["expression"], env), c["round_to"])
        except WorkflowError as exc:
            errs.append(f"computation {c['id']}: {exc}")
            continue
        if not _same(got, float(c["result"])):
            errs.append(f"computation {c['id']}: stated result {c['result']} but executing {c['expression']!r} gives {got}")
    return out, errs


def check_statements(statements: Sequence[dict], comps: Dict[str, dict], pack: Optional[Pack]) -> Tuple[Dict[str, dict], List[str]]:
    """Evidence classes: observed needs real observation sources, synthetic needs synthetic fixtures,
    every number in an observed/synthetic statement must come from its sources or an executed
    computation, unknowns carry a resolution, estimates a basis, and topics the pack lists as not
    available stay unknown."""
    errs: List[str] = []
    by_id: Dict[str, dict] = {}
    items = pack.items if pack else {}
    synthetic_task = pack.synthetic if pack else False
    for s in statements:
        sid = s["id"]
        if sid in by_id:
            errs.append(f"statement {sid}: duplicate id")
        by_id[sid] = s
        cited = []
        for src in s["source_ids"]:
            if src not in items:
                errs.append(f"statement {sid}: cites unknown source {src}")
            else:
                cited.append(items[src])
        for cid in s.get("computation_ids", []):
            if cid not in comps:
                errs.append(f"statement {sid}: cites unknown computation {cid}")
        cls = s["class"]
        if cls == "observed":
            if synthetic_task:
                errs.append(f"statement {sid}: a synthetic task has no observed facts; label fixture facts 'synthetic'")
            if not cited:
                errs.append(f"statement {sid}: observed statement cites no source")
            if any(c["classification"] == "synthetic" for c in cited):
                errs.append(f"statement {sid}: observed statement cites synthetic data (synthetic data is never an observation)")
            if cited and not any(c["kind"] in OBSERVATION_SOURCE_KINDS and c["classification"] == "real" for c in cited):
                errs.append(f"statement {sid}: observed statement cites no real primary source")
        elif cls == "synthetic":
            if not cited:
                errs.append(f"statement {sid}: synthetic fact cites no fixture source")
            if any(c["classification"] != "synthetic" for c in cited):
                errs.append(f"statement {sid}: a real source cannot be relabeled synthetic")
        elif cls == "unknown":
            if "resolution" not in s:
                errs.append(f"statement {sid}: unknown needs a resolution (how to retrieve or measure it)")
        elif cls == "estimate":
            if "basis" not in s and not s.get("computation_ids"):
                errs.append(f"statement {sid}: estimate needs a basis or an executed computation")
        if cls in ("observed", "synthetic"):
            allowed: List[float] = []
            for c in cited:
                allowed += numbers_in(c["excerpt"]) + [float(v) for v in c.get("values", {}).values()]
            for cid in s.get("computation_ids", []):
                if cid in comps:
                    allowed.append(float(comps[cid]["result"]))
            for n in numbers_in(s["text"]):
                if not any(_same(n, a) for a in allowed):
                    errs.append(f"statement {sid}: number {n:g} is not in its cited sources or computations")
    if pack is not None:
        gaps = pack.unavailable_topics()
        for topic, gap in gaps.items():
            facts = [s["id"] for s in statements if s.get("topic") == topic and s["class"] in ("observed", "synthetic", "estimate")]
            if facts:
                errs.append(f"topic {topic} is not available ({gap['reason']}) but {facts} state it as fact/estimate")
            if not any(s.get("topic") == topic and s["class"] == "unknown" for s in statements):
                errs.append(f"topic {topic} is not available ({gap['reason']}): record it as an unknown with a resolution")
    return by_id, errs


def check_questions(questions: Sequence[dict], pack: Optional[Pack]) -> List[str]:
    """Questions to the requester must not ask for facts the supplied evidence already holds."""
    if pack is None:
        return []
    covered = {t for it in pack.items.values() for t in it["topics"]}
    return [f"question about {q['topic']!r} asks for a fact the supplied evidence already covers"
            for q in questions if q["topic"] in covered]


def check_binding(a: dict, pack: Optional[Pack]) -> List[str]:
    errs = []
    if pack is None:
        if a.get("evidence") is not None:
            errs.append("analysis cites an evidence pack but none was supplied")
        return errs
    if a.get("evidence") is None:
        errs.append("an evidence pack was supplied but the analysis does not cite it")
        return errs
    if a["evidence"]["pack_id"] != pack.doc["pack_id"] or a["evidence"]["digest"] != pack.digest:
        errs.append("analysis evidence reference does not match the supplied pack (id or digest)")
    if a["binding"] != pack.doc["binding"]:
        errs.append("analysis binding (task/company/project/class) differs from the evidence pack binding")
    return errs


# ---------------------------------------------------------------- artifact reuse

OUT_OF_SCOPE = "a referenced artifact is outside this task's company/project/data-class scope"


def in_task_scope(art: dict, binding: Any) -> bool:
    """Same company and project as the task binding, and a data class the task may use
    (the registry's common predicate: synthetic only for synthetic tasks, company-private only for private ones)."""
    if not isinstance(binding, dict):
        return False
    return ((art["company_id"], art["project_id"]) == (binding.get("company_id"), binding.get("project_id"))
            and cr._class_compatible(art["data_classification"], binding.get("data_classification")))


def scope_errors(art: dict, b: dict, label: str) -> List[str]:
    """The tenant/class gate for one directly referenced artifact (reused or reviewed)."""
    if (art["company_id"], art["project_id"]) != (b["company_id"], b["project_id"]):
        return [f"{label} belongs to another company/project"]
    if not cr._class_compatible(art["data_classification"], b["data_classification"]):
        if art["data_classification"] == "synthetic":
            return [f"{label} is synthetic and cannot stand for real work"]
        return [f"{label} is {art['data_classification']} and cannot be used by a {b['data_classification']} task"]
    return []


@dataclass
class ArtifactStore:
    index: Dict[str, Any]
    root: Path
    scope: Optional[Dict[str, Any]] = None  # the task binding; content is read only for in-scope entries

    def get(self, aid: str) -> Optional[dict]:
        return next((a for a in self.index["artifacts"] if a["artifact_id"] == aid), None)

    def bind(self, binding: Any) -> List[str]:
        if self.scope is None:
            self.scope = dict(binding) if isinstance(binding, dict) else {}
            return []
        return [] if binding == self.scope else ["the artifact index was loaded for a different task binding"]

    def read_bytes(self, aid: str) -> bytes:
        a = self.get(aid)
        if a is None or self.scope is None or not in_task_scope(a, self.scope):
            raise WorkflowError(OUT_OF_SCOPE)  # no ID, location or digest of the foreign entry
        data = cr.safe_repo_file(self.root, a["location"])
        if cr.sha256_bytes(data) != a["content_digest"]:
            raise WorkflowError(f"artifact {aid}: content differs from its indexed digest")
        return data

    def read_json(self, aid: str) -> Any:
        return cr.parse_json(self.read_bytes(aid).decode("utf-8"))

    def read_text(self, aid: str) -> str:
        return self.read_bytes(aid).decode("utf-8")


def load_artifacts(index_path: Path, root: Path, binding: Any = None) -> Tuple[Optional[ArtifactStore], List[str]]:
    """Schema-check the (possibly shared) index. With a task binding, only in-scope entries' bytes are
    read and verified, so a foreign or stricter-class entry is never opened and never named in an error;
    without one (fixtures, tests) every entry is verified and the store binds to the analysis later."""
    index = cr.load_json(index_path)
    errs = cr.schema_errors(index, "artifact.schema.json", "artifacts")
    if errs:
        return None, errs
    if binding is None:
        errs = cr.verify_artifact_contents(index, root)
        return (None, errs) if errs else (ArtifactStore(index, root), [])
    mine = {"artifacts": [a for a in index["artifacts"] if in_task_scope(a, binding)]}
    errs = cr.verify_artifact_contents(mine, root)
    if errs:
        return None, errs
    store = ArtifactStore(index, root)
    store.bind(binding)
    return store, []


def check_reuse(refs: Sequence[dict], a: dict, store: Optional[ArtifactStore], registry: dict) -> List[str]:
    """Reused inputs must be accepted, current (A's staleness rules), same company/project and digest."""
    if not refs:
        return []
    if store is None:
        return ["analysis reuses artifacts but no artifact index was supplied"]
    errs = []
    currency = cr._artifact_currency(store.index["artifacts"], {c["id"]: c for c in registry["capabilities"]})
    b = a["binding"]
    for r in refs:
        art = store.get(r["artifact_id"])
        if art is None:
            errs.append(f"reused artifact {r['artifact_id']} is not in the index")
            continue
        e = scope_errors(art, b, f"reused artifact {r['artifact_id']}")
        if e:  # nothing else about an out-of-scope entry is inspected or reported
            errs += e
            continue
        if art["content_digest"] != r["digest"] or art["kind"] != r["kind"]:
            errs.append(f"reused artifact {r['artifact_id']}: kind/digest differs from the index (stale reference)")
        if art["review_status"] != "accepted":
            errs.append(f"reused artifact {r['artifact_id']} is {art['review_status']}, not accepted")
        ok, why = currency[art["artifact_id"]]
        if not ok:
            errs.append(f"reused artifact {r['artifact_id']} is stale: {why}")
    return errs


# ---------------------------------------------------------------- problem definition rubric

HEAVY_SECTIONS = ("scqa", "issue_map", "causal_map", "one_page_prd", "stage_log")
STAGE_SECTION = {"scqa": "scqa", "why-tree": "why_tree", "issue-map": "issue_map", "causal-map": "causal_map",
                 "hypothesis": "hypothesis", "one-page-prd": "one_page_prd"}


def _edge_errors(where: str, label: str, stmts: Sequence[dict], pack: Optional[Pack], test: Optional[str]) -> List[str]:
    items = pack.items if pack else {}
    errs = []
    classes = {s["class"] for s in stmts}
    cited = [items[src] for s in stmts for src in s["source_ids"] if src in items]
    if label == "unknown-link" and classes - {"unknown"}:
        errs.append(f"{where}: unknown-link must rest on unknown statements")
    if "unknown" in classes and label != "unknown-link":
        errs.append(f"{where}: an unknown cause is labeled {label} (a missing cause is not a fact)")
    if label == "observed-association" and not (classes <= {"observed", "synthetic"} and cited):
        errs.append(f"{where}: observed-association needs observed/synthetic statements with sources")
    if label == "experiment-supported" and not any(c.get("study_design") == "experiment" for c in cited):
        errs.append(f"{where}: experiment-supported but no cited source is an experiment (correlation is not causal proof)")
    if label == "causal-hypothesis":
        if not classes & {"hypothesis"}:
            errs.append(f"{where}: causal-hypothesis must rest on a hypothesis statement")
        if not test:
            errs.append(f"{where}: causal-hypothesis needs a falsification test")
    return errs


def check_why_tree(a: dict, stmts: Dict[str, dict], pack: Optional[Pack]) -> List[str]:
    tree, errs = a["why_tree"], []
    nodes = {}
    for n in tree["nodes"]:
        if n["id"] in nodes or n["id"] == "root":
            errs.append(f"why-tree node {n['id']}: duplicate or reserved id")
        nodes[n["id"]] = n
    for n in tree["nodes"]:
        if n["parent"] != "root" and n["parent"] not in nodes:
            errs.append(f"why-tree node {n['id']}: unknown parent {n['parent']}")
        seen, cur = set(), n
        while cur["parent"] != "root" and cur["parent"] in nodes:
            if cur["id"] in seen:
                errs.append(f"why-tree node {n['id']}: parent cycle")
                break
            seen.add(cur["id"])
            cur = nodes[cur["parent"]]
        s = stmts.get(n["statement_id"])
        if s is None:
            errs.append(f"why-tree node {n['id']}: unknown statement {n['statement_id']}")
            continue
        errs += _edge_errors(f"why-tree node {n['id']}", n["edge"], [s], pack, n.get("test"))
        for c in n["counter_statement_ids"]:
            if c not in stmts:
                errs.append(f"why-tree node {n['id']}: unknown counter-evidence {c}")
    siblings: Dict[str, int] = {}
    for n in tree["nodes"]:
        siblings[n["parent"]] = siblings.get(n["parent"], 0) + 1
    if any(v > 1 for v in siblings.values()):
        if "partition" in a and "interaction_assessment" not in a["partition"]:
            errs.append("why-tree branches into several causes: the partition needs an interaction_assessment")
    return errs


def validate_problem_definition(a: dict, ctx: "Context") -> List[str]:
    errs: List[str] = []
    pack = ctx.pack
    if a["problem_type"] == "code-defect":
        errs.append("problem_type code-defect: a known code bug goes to existing.engineering-path "
                    "(systematic-debugging), not problem definition")
    comps, e = check_computations(a["computations"], pack)
    errs += e
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e
    errs += check_questions(a["questions"], pack)
    if a["symptom_id"] not in stmts:
        errs.append(f"symptom {a['symptom_id']} is not a statement")
    elif stmts[a["symptom_id"]]["class"] not in ("observed", "synthetic"):
        errs.append("the symptom must be an evidenced observation (observed, or synthetic in a synthetic task)")
    base = a["success_measure"]["baseline_statement_id"]
    if base not in stmts:
        errs.append(f"success measure baseline {base} is not a statement")
    if sum(1 for x in a["alternatives"] if x["kind"] == "do-nothing") > 1:
        errs.append("at most one do-nothing alternative")
    if len({x["id"] for x in a["alternatives"]}) != len(a["alternatives"]):
        errs.append("duplicate alternative id")
    na = a["next_action"]
    if na["involves_external_contact"] and "outreach" not in na["approvals_needed"]:
        errs.append("next action contacts people outside the task: it needs the outreach approval, not an assumption of it")
    mode = a["mode"]
    present = {k for k in HEAVY_SECTIONS + ("why_tree", "partition", "hypothesis") if k in a}
    if mode == "quick":
        extra = present - {"hypothesis"}
        if extra:
            errs.append(f"quick mode stays a compact decision brief; remove {sorted(extra)} or use causal/full mode")
    if mode == "causal":
        for k in ("why_tree", "partition"):
            if k not in a:
                errs.append(f"causal mode needs {k}")
        extra = present & set(HEAVY_SECTIONS)
        if extra:
            errs.append(f"causal mode does not carry full-mode sections {sorted(extra)}")
    if mode == "full":
        log = {s["stage"]: s for s in a.get("stage_log", [])}
        if set(log) != set(STAGE_SECTION) or len(a.get("stage_log", [])) != len(STAGE_SECTION):
            errs.append("full mode needs one stage_log entry for each of the six stages")
        for stage, sec in STAGE_SECTION.items():
            st = log.get(stage)
            if st is None:
                continue
            if st["status"] == "done" and sec not in a:
                errs.append(f"stage {stage} is done but section {sec} is missing")
            if st["status"] != "done" and "reason" not in st:
                errs.append(f"stage {stage} is {st['status']}: say why")
            if st["status"] == "skipped" and a.get("requested_full_exercise"):
                errs.append(f"stage {stage} skipped although the full exercise was explicitly requested")
            if st["status"] == "skipped" and sec in a:
                errs.append(f"stage {stage} is skipped but section {sec} is present")
    if "why_tree" in a:
        errs += check_why_tree(a, stmts, pack)
    if "scqa" in a:
        for sid in a["scqa"]["situation_ids"] + a["scqa"]["complication_ids"]:
            if sid not in stmts:
                errs.append(f"scqa cites unknown statement {sid}")
        for sid in a["scqa"]["complication_ids"]:
            if sid in stmts and stmts[sid]["class"] not in ("observed", "synthetic"):
                errs.append(f"scqa complication {sid} must be evidenced, not {stmts[sid]['class']}")
    if "issue_map" in a:
        node_ids = {n["id"] for n in a.get("why_tree", {}).get("nodes", [])}
        prios = [b["priority"] for b in a["issue_map"]["branches"]]
        if len(prios) != len(set(prios)):
            errs.append("issue map branch priorities must be distinct")
        if not any(b["actionable"] for b in a["issue_map"]["branches"]):
            errs.append("issue map has no actionable branch")
        for b in a["issue_map"]["branches"]:
            for nid in b["node_ids"]:
                if nid not in node_ids:
                    errs.append(f"issue map branch {b['id']} cites unknown why-tree node {nid}")
    if "causal_map" in a:
        cm = a["causal_map"]
        ids = {"outcome"} | {x["id"] for x in cm["leading_problems"]} | {x["id"] for x in cm["opportunities"]}
        if cm["outcome"]["baseline_statement_id"] not in stmts:
            errs.append("causal map outcome baseline is not a statement")
        for lp in cm["leading_problems"]:
            for sid in lp["statement_ids"]:
                if sid not in stmts:
                    errs.append(f"leading problem {lp['id']} cites unknown statement {sid}")
        for op in cm["opportunities"]:
            for t in op["addresses"]:
                if t not in ids:
                    errs.append(f"opportunity {op['id']} addresses unknown node {t}")
        for i, ed in enumerate(cm["edges"]):
            if ed["from"] not in ids or ed["to"] not in ids:
                errs.append(f"causal map edge {i}: unknown endpoint")
            missing = [s for s in ed["statement_ids"] if s not in stmts]
            if missing:
                errs.append(f"causal map edge {i}: unknown statements {missing}")
                continue
            errs += _edge_errors(f"causal map edge {ed['from']}->{ed['to']}", ed["label"],
                                 [stmts[s] for s in ed["statement_ids"]], pack, ed["falsification"])
    if "one_page_prd" in a:
        for sid in a["one_page_prd"]["unknown_ids"]:
            if sid not in stmts or stmts[sid]["class"] != "unknown":
                errs.append(f"one-page PRD unknown {sid} is not an unknown statement")
    return errs


# ---------------------------------------------------------------- hypothesis label conventions (PD-05)

def hypothesis_from_xyz(convention: str, x: Any, y: str, z: str, **rest: Any) -> Dict[str, Any]:
    """Map an X/Y/Z hypothesis written in either published labeling convention to the semantic
    fields. RO-PNA labels X = share of a population, Y = behavior, Z = reason; SoloSquad writes
    'X% of Y will Z' (X = share, Y = segment, Z = behavior) with the reason separate. Window,
    measurement, counter-metric and failure rule are never inferred: they must be supplied."""
    if convention == "ro-pna":
        if not isinstance(x, dict) or set(x) != {"share_percent", "population"}:
            raise WorkflowError("ro-pna X must be {share_percent, population}")
        share, population, behavior, rationale = x["share_percent"], x["population"], y, z
    elif convention == "solosquad":
        if "rationale" not in rest:
            raise WorkflowError("solosquad hypotheses carry the reason separately: supply rationale")
        share, population, behavior, rationale = x, y, z, rest.pop("rationale")
    else:
        raise WorkflowError(f"unknown hypothesis convention {convention!r}")
    if not isinstance(share, (int, float)) or isinstance(share, bool) or not 0 < share <= 100:
        raise WorkflowError("the share must be a percentage in (0, 100]")
    missing = [k for k in ("window_days", "intervention", "measurement", "counter_metric", "failure_rule") if k not in rest]
    if missing:
        raise WorkflowError(f"hypothesis needs {missing}: they are never inferred")
    unknown = set(rest) - {"window_days", "intervention", "measurement", "counter_metric", "failure_rule"}
    if unknown:
        raise WorkflowError(f"unknown hypothesis fields {sorted(unknown)}")
    return {"population": population, "behavior_or_outcome": behavior,
            "threshold": {"comparator": ">=", "value": share, "unit": "percent of population"},
            "window_days": rest["window_days"], "intervention": rest["intervention"], "rationale": rationale,
            "measurement": rest["measurement"], "counter_metric": rest["counter_metric"],
            "failure_rule": rest["failure_rule"]}


# ---------------------------------------------------------------- rendering helpers

def md_escape(text: str) -> str:
    """Inline Markdown text: no raw HTML, no table breaks, no headings from data."""
    return (text.replace("\\", "\\\\").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")
            .replace("\r", " ").replace("\n", " "))


def fmt_threshold(t: dict) -> str:
    return f"{t['comparator']} {t['value']:g} {md_escape(t['unit'])}"


def statement_line(s: dict) -> str:
    cites = ", ".join(s["source_ids"] + s.get("computation_ids", []))
    tail = f" [{cites}]" if cites else ""
    extra = f" — resolve: {md_escape(s['resolution'])}" if s["class"] == "unknown" and "resolution" in s else ""
    extra += f" — basis: {md_escape(s['basis'])}" if "basis" in s else ""
    return f"- **{s['class']}** `{s['id']}`: {md_escape(s['text'])}{tail}{extra}"


def header(title: str, a: dict, ctx: "Context") -> List[str]:
    b = a["binding"]
    label = ("Coordinator-authored analysis on SYNTHETIC fixture data (not real research)"
             if a["authored_by"]["kind"] == "coordinator-synthetic-demo" or b["data_classification"] == "synthetic"
             else "Coordinator-authored analysis")
    lines = [f"# {md_escape(title)}", "",
             f"- Status: **draft** (acceptance is a separate reviewer decision)",
             f"- Provenance: {label}; validated and rendered by scripts/capability_workflow.py, which authors no content",
             f"- Task `{b['task_id']}` · company `{b['company_id']}` · project `{b['project_id']}` · data `{b['data_classification']}`"]
    if ctx.pack is not None:
        lines.append(f"- Evidence pack `{ctx.pack.doc['pack_id']}` sha256 `{ctx.pack.digest[:16]}…` as of {ctx.pack.doc['as_of']}")
    return lines + [""]


def by_class(statements: Sequence[dict], classes: Iterable[str]) -> List[dict]:
    cl = set(classes)
    return [s for s in statements if s["class"] in cl]


def render_problem_definition(a: dict, ctx: "Context") -> Dict[str, str]:
    st = {s["id"]: s for s in a["statements"]}
    mode = a["mode"]
    main_kind = "decision-brief" if mode == "quick" else "problem-brief"
    L = header(f"{'Decision brief' if mode == 'quick' else 'Problem brief'} ({mode} mode)", a, ctx)
    L += ["## Decision", f"{md_escape(a['decision']['question'])} (owner: {md_escape(a['decision']['owner_role'])})", "",
          "## Problem", md_escape(a["problem_statement"]),
          f"Affected: {md_escape(a['affected']['who'])}" + (f"; process: {md_escape(a['affected']['process'])}" if "process" in a["affected"] else ""),
          f"Symptom: `{a['symptom_id']}` {md_escape(st[a['symptom_id']]['text'])}", ""]
    L += ["## Evidence"] + [statement_line(s) for s in by_class(a["statements"], ("observed", "synthetic"))
                            if s["id"] != a["symptom_id"]] + [""]
    if a["computations"]:
        L += ["## Executed arithmetic"] + [
            f"- `{c['id']}`: {md_escape(c['expression'])} = {c['result']:g} {md_escape(c['unit'])} (recomputed)" for c in a["computations"]] + [""]
    L += ["## Assumptions, hypotheses and estimates"] + [statement_line(s) for s in by_class(a["statements"], ("assumption", "hypothesis", "estimate"))] + [""]
    L += ["## Unknowns"] + ([statement_line(s) for s in by_class(a["statements"], ("unknown",))] or ["- none recorded"]) + [""]
    if "why_tree" in a:
        L += ["## Why tree (branches allowed; no fixed depth)"]
        kids: Dict[str, List[dict]] = {}
        for n in a["why_tree"]["nodes"]:
            kids.setdefault(n["parent"], []).append(n)

        def walk(pid: str, depth: int) -> None:
            for n in kids.get(pid, []):
                test = f" — test: {md_escape(n['test'])}" if "test" in n else ""
                counter = f" — counter-evidence: {', '.join(n['counter_statement_ids'])}" if n["counter_statement_ids"] else ""
                L.append(f"{'  ' * depth}- `{n['id']}` [{n['edge']}] {md_escape(st[n['statement_id']]['text'])}{test}{counter}")
                walk(n["id"], depth + 1)

        walk("root", 0)
        L += [f"Stop: {md_escape(a['why_tree']['stop_reason'])}", ""]
    if "partition" in a:
        p = a["partition"]
        L += ["## Partition", f"- Criterion: {md_escape(p['criterion'])}", f"- Overlaps: {md_escape(p['overlaps'])}",
              f"- Omitted: {md_escape(p['omitted'])}"]
        if "interaction_assessment" in p:
            L.append(f"- Interaction: {md_escape(p['interaction_assessment'])}")
        L.append("")
    if "scqa" in a:
        q = a["scqa"]
        L += ["## SCQA", "- Situation: " + "; ".join(md_escape(st[i]["text"]) for i in q["situation_ids"]),
              "- Complication: " + "; ".join(md_escape(st[i]["text"]) for i in q["complication_ids"]),
              f"- Question: {md_escape(q['question'])}", f"- Candidate answer (not a conclusion): {md_escape(q['provisional_answer']['text'])}", ""]
    if "issue_map" in a:
        im = a["issue_map"]
        L += [f"## Issue map (criterion: {md_escape(im['criterion'])})", "| Priority | Branch | Why-tree nodes | Actionable |", "|---|---|---|---|"]
        for b in sorted(im["branches"], key=lambda b: b["priority"]):
            L.append(f"| {b['priority']} | {md_escape(b['label'])} | {', '.join(b['node_ids']) or '-'} | {'yes' if b['actionable'] else 'no'} |")
        L += [f"Overlaps: {md_escape(im['overlaps'])}", f"Omitted: {md_escape(im['omitted'])}", ""]
    if "causal_map" in a:
        cm = a["causal_map"]
        L += ["## Customer/process causal map",
              f"Outcome: {md_escape(cm['outcome']['metric'])} — {md_escape(cm['outcome']['definition'])}; baseline `{cm['outcome']['baseline_statement_id']}`; window {cm['outcome']['window_days']} days"]
        L += [f"- leading `{x['id']}`: {md_escape(x['text'])}" for x in cm["leading_problems"]]
        L += [f"- opportunity `{x['id']}`: {md_escape(x['text'])} (addresses {', '.join(x['addresses'])})" for x in cm["opportunities"]]
        L += [f"- edge {e['from']} → {e['to']} [{e['label']}] falsify: {md_escape(e['falsification'])}" for e in cm["edges"]] + [""]
    if "hypothesis" in a:
        h = a["hypothesis"]
        L += ["## Measurable hypothesis"] + [f"- {k}: {md_escape(str(v)) if k != 'threshold' else fmt_threshold(v)}" for k, v in h.items()] + [""]
    L += ["## Alternatives"] + [f"- `{x['id']}` ({x['kind']}): {md_escape(x['summary'])} — {md_escape(x['tradeoffs'])}" for x in a["alternatives"]] + [""]
    sm = a["success_measure"]
    base = st.get(sm["baseline_statement_id"], {})
    L += ["## Success measure", f"{md_escape(sm['metric'])}: target {fmt_threshold(sm['target'])} within {sm['window_days']} days; "
          f"baseline `{sm['baseline_statement_id']}` ({base.get('class', '?')})", ""]
    if a["questions"]:
        L += ["## Questions for the requester (material, not retrievable)"] + [f"- [{q['topic']}] {md_escape(q['text'])}" for q in a["questions"]] + [""]
    na = a["next_action"]
    L += ["## Next action", f"{md_escape(na['action'])} ({na['kind']}, {na['cost']}, owner {md_escape(na['owner_role'])})"
          + (f"; needs approval: {', '.join(na['approvals_needed'])}" if na["approvals_needed"] else ""), ""]
    if "stage_log" in a:
        L += ["## Stage log"] + [f"- {s['stage']}: {s['status']}" + (f" — {md_escape(s['reason'])}" if "reason" in s else "") for s in a["stage_log"]] + [""]
    files = {f"{main_kind}.md": "\n".join(L)}
    reg = [f"# Assumption register ({mode} mode)", "", "| Class | ID | Statement | Sources | Resolution/basis |", "|---|---|---|---|---|"]
    for s in by_class(a["statements"], ("assumption", "hypothesis", "estimate", "unknown")):
        reg.append(f"| {s['class']} | {s['id']} | {md_escape(s['text'])} | {', '.join(s['source_ids']) or '-'} | "
                   f"{md_escape(s.get('resolution', s.get('basis', '-')))} |")
    files["assumption-register.md"] = "\n".join(reg) + "\n"
    if "one_page_prd" in a:
        p = a["one_page_prd"]
        P = header("One-page PRD (draft, never approved implementation)", a, ctx)
        P += [f"**Problem:** {md_escape(p['problem'])}", f"**Users:** {md_escape(p['users'])}", f"**Objective:** {md_escape(p['objective'])}", ""]
        for title, key in (("Scope", "scope"), ("Non-goals", "non_goals"), ("Constraints", "constraints"),
                           ("Acceptance tests", "acceptance_tests"), ("Risks", "risks")):
            P += [f"## {title}"] + [f"- {md_escape(x)}" for x in p[key]] + [""]
        P += ["## Unknowns"] + [f"- `{u}` {md_escape(st[u]['text'])}" for u in p["unknown_ids"]] + [""]
        P += [f"Owner: {md_escape(p['owner_role'])}. Next decision: {md_escape(p['next_decision'])}"]
        files["one-page-prd.md"] = "\n".join(P) + "\n"
    return files


def post_render_problem_definition(a: dict, files: Dict[str, str]) -> List[str]:
    if a["mode"] != "quick":
        return []
    body = files["decision-brief.md"].split("\n## Decision\n", 1)[-1]  # the provenance header is not content
    words = len(body.split())
    return [f"quick mode rendered {words} words (> {QUICK_MAX_WORDS}): keep the brief compact or use causal/full mode"] \
        if words > QUICK_MAX_WORDS else []


# ---------------------------------------------------------------- shared rubric pieces (discovery, lean PRD)

def check_frame_reuse(a: dict, kinds: Sequence[str], what: str) -> List[str]:
    """The analysis must build on an accepted current upstream artifact, cited by ID and digest
    (check_reuse verifies acceptance, currency and binding)."""
    if any(r["kind"] in kinds for r in a["reused_artifacts"]):
        return []
    return [f"{what} needs an accepted current {' / '.join(kinds)} reused by artifact ID and digest; "
            "run kit.problem-definition (quick) first or supply the accepted artifact"]


def check_next_action(na: dict) -> List[str]:
    if na["involves_external_contact"] and "outreach" not in na["approvals_needed"]:
        return ["next action contacts people outside the task: it needs the outreach approval, not an assumption of it"]
    return []


def check_refs(where: str, ids: Iterable[str], stmts: Dict[str, dict]) -> List[str]:
    return [f"{where} cites unknown statement {sid}" for sid in ids if sid not in stmts]


def evidence_gap_lines(a: dict, ctx: "Context") -> List[str]:
    lines = [statement_line(s) for s in by_class(a["statements"], ("unknown",))] or ["- none recorded"]
    if ctx.pack is not None:
        policy = ctx.pack.doc["policy"]["public_research"]
        lines.append(f"- Public research: {policy} by the evidence pack policy"
                     + ("; these gaps stay open until it is approved" if policy == "denied" else ""))
    return lines


def _approval_needs(a: dict) -> List[str]:
    out = [f"question for the requester: {q['text']}" for q in a["questions"]]
    out += [f"approval needed before the next action: {x}" for x in a["next_action"]["approvals_needed"]]
    return out


# ---------------------------------------------------------------- product discovery rubric (adapted PM Skills method)

DISCOVERY_CATEGORIES = {
    "existing": {"value", "usability", "viability", "feasibility"},
    "new": {"value", "usability", "viability", "feasibility", "ethics", "go-to-market", "strategy", "team"},
}
DISCOVERY_METHODS = {
    "existing": {"prototype-test", "fake-door", "technical-spike", "ab-test", "wizard-of-oz", "behavioral-survey", "data-analysis"},
    "new": {"landing-page", "explainer-video", "email-campaign", "pre-order", "concierge-mvp"},
}
LIVE_PRODUCT_METHODS = {"ab-test", "fake-door"}
CONTACT_METHODS = {"landing-page", "explainer-video", "email-campaign", "pre-order", "concierge-mvp", "behavioral-survey"}
SKIN_IN_THE_GAME = {"pre-order", "concierge-mvp"}
MATRIX = {("high", "high"): "test", ("high", "low"): "accept-risk", ("low", "low"): "defer", ("low", "high"): "drop-idea"}
FRAME_KINDS = ("problem-brief", "decision-brief", "feature-brief")


def validate_product_discovery(a: dict, ctx: "Context") -> List[str]:
    pack, mode = ctx.pack, a["mode"]
    other = "new" if mode == "existing" else "existing"
    errs = check_frame_reuse(a, FRAME_KINDS, "discovery")
    comps, e = check_computations(a["computations"], pack)
    errs += e
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_questions(a["questions"], pack) + check_next_action(a["next_action"])
    if a["desired_outcome"]["baseline_statement_id"] not in stmts:
        errs.append("desired outcome baseline is not a statement")
    opps = {}
    for o in a["opportunities"]:
        if o["id"] in opps:
            errs.append(f"opportunity {o['id']}: duplicate id")
        opps[o["id"]] = o
        errs += check_refs(f"opportunity {o['id']}", o["statement_ids"], stmts)

    ideas: Dict[str, dict] = {}
    for i in a["ideas"]:
        if i["id"] in ideas:
            errs.append(f"idea {i['id']}: duplicate id")
        ideas[i["id"]] = i
        if i["opportunity_id"] not in opps:
            errs.append(f"idea {i['id']}: unknown opportunity {i['opportunity_id']}")
        if i["status"] == "declined" and "reason" not in i:
            errs.append(f"idea {i['id']} is declined: record why")
    missing = {"product", "design", "engineering"} - {i["perspective"] for i in a["ideas"]}
    if missing:
        errs.append(f"ideation must come from the product trio: no {sorted(missing)} perspective idea")

    cps = {c["stage"]: c for c in a["checkpoints"]}
    if set(cps) != {"idea-selection", "assumption-focus"}:
        errs.append("discovery needs exactly one idea-selection and one assumption-focus checkpoint")
    for c in a["checkpoints"]:
        if c["status"] == "answered":
            src = c.get("answer_source_id")
            if src is None:
                errs.append(f"{c['stage']} checkpoint is answered but cites no supplied answer")
            elif pack is None or src not in pack.items:
                errs.append(f"{c['stage']} checkpoint answer cites unknown source {src}")
            elif c["stage"] == "idea-selection":
                errs += recorded_ids("idea-selection checkpoint", src, "idea-selection",
                                     [i["id"] for i in a["ideas"] if i["status"] == "carried"], "carries", a, pack)
            elif c["stage"] == "assumption-focus":
                errs += recorded_ids("assumption-focus checkpoint", src, "assumption-focus",
                                     [x["id"] for x in a["assumptions"] if x["action"] == "test"], "tests", a, pack)
    if cps.get("idea-selection", {}).get("status") == "pending":
        chosen = [i["id"] for i in a["ideas"] if i["status"] != "proposed"]
        if chosen:
            errs.append(f"idea-selection checkpoint is pending, yet {chosen} are carried/declined: the requester chooses")
    if cps.get("assumption-focus", {}).get("status") == "pending" and a["experiments"]:
        errs.append("assumption-focus checkpoint is pending: design experiments after the requester picks the focus")

    assumptions: Dict[str, dict] = {}
    for s in a["assumptions"]:
        aid = s["id"]
        if aid in assumptions:
            errs.append(f"assumption {aid}: duplicate id")
        assumptions[aid] = s
        idea = ideas.get(s["idea_id"])
        if idea is None:
            errs.append(f"assumption {aid}: unknown idea {s['idea_id']}")
        elif idea["status"] != "carried":
            errs.append(f"assumption {aid}: idea {idea['id']} is {idea['status']}; only carried ideas are stress-tested")
        if s["category"] not in DISCOVERY_CATEGORIES[mode]:
            errs.append(f"assumption {aid}: {s['category']} is not {'an' if mode == 'existing' else 'a'} {mode}-product risk category")
        want = MATRIX[(s["impact"], s["risk"])]
        if s["action"] != want:
            errs.append(f"assumption {aid}: {s['impact']} impact and {s['risk']} risk means {want}, not {s['action']}")
        if (s["action"] == "test") != ("rank" in s):
            errs.append(f"assumption {aid}: exactly the assumptions to test carry a rank")
        errs += check_refs(f"assumption {aid}", s["statement_ids"], stmts)
    ranks = sorted(s["rank"] for s in a["assumptions"] if "rank" in s)
    if ranks != list(range(1, len(ranks) + 1)):
        errs.append(f"test assumption ranks must be 1..{len(ranks)} without ties or gaps, got {ranks}")
    for idea in (i for i in a["ideas"] if i["status"] == "carried"):
        if not any(s["idea_id"] == idea["id"] and s["category"] == "value" for s in a["assumptions"]):
            errs.append(f"carried idea {idea['id']} has no value assumption")
    if mode == "new" and not any(s["category"] == "go-to-market" for s in a["assumptions"]):
        errs.append("a new product needs at least one go-to-market assumption")

    tested: Dict[str, List[str]] = {}
    for x in a["experiments"]:
        xid = x["id"]
        if x["method"] not in DISCOVERY_METHODS[mode]:
            errs.append(f"experiment {xid}: {x['method']} is {'an' if other == 'existing' else 'a'} {other}-product method")
        if x["method"] in LIVE_PRODUCT_METHODS and "risk_mitigation" not in x:
            errs.append(f"experiment {xid}: {x['method']} on a live product needs risk_mitigation")
        if x["method"] in CONTACT_METHODS and not {"outreach", "publication"} & set(x["approvals_needed"]):
            errs.append(f"experiment {xid}: {x['method']} contacts people outside the task; list the outreach/publication approval")
        for aid in x["assumption_ids"]:
            s = assumptions.get(aid)
            if s is None:
                errs.append(f"experiment {xid}: unknown assumption {aid}")
            elif s["action"] != "test":
                errs.append(f"experiment {xid}: assumption {aid} is {s['action']}, not one to test")
            else:
                tested.setdefault(aid, []).append(x["method"])
    for s in a["assumptions"]:
        if s["action"] == "test" and s["id"] not in tested:
            errs.append(f"assumption {s['id']} (rank {s.get('rank')}) has no experiment")
    for s in a["assumptions"]:
        if mode == "new" and s["category"] == "viability" and s["action"] == "test" \
                and not SKIN_IN_THE_GAME & set(tested.get(s["id"], [])):
            errs.append(f"assumption {s['id']}: viability in a new product needs a skin-in-the-game test "
                        f"({' or '.join(sorted(SKIN_IN_THE_GAME))}), not interest alone")
    for i in a["ideas"]:
        if i["status"] == "declined" and any(s["idea_id"] == i["id"] for s in a["assumptions"]):
            errs.append(f"idea {i['id']} is declined and must not come back as accepted scope")
    return errs


def render_product_discovery(a: dict, ctx: "Context") -> Dict[str, str]:
    st = {s["id"]: s for s in a["statements"]}
    ideas = {i["id"]: i for i in a["ideas"]}
    branch = "new product (initial discovery)" if a["mode"] == "new" else "existing product (continuous discovery)"
    do = a["desired_outcome"]
    L = header(f"Discovery brief ({a['mode']} mode)", a, ctx)
    L += ["## Discovery question", md_escape(a["discovery_question"]), f"Branch: {branch}",
          f"Decision this informs: {md_escape(a['decision_informed'])}", "",
          "## Desired outcome", f"{md_escape(do['metric'])}: target {fmt_threshold(do['target'])} within {do['window_days']} days; "
          f"baseline `{do['baseline_statement_id']}` ({st.get(do['baseline_statement_id'], {}).get('class', '?')})", "",
          "## Problem frame reused"] + [f"- `{r['artifact_id']}` ({r['kind']}, sha256 `{r['digest'][:16]}…`)" for r in a["reused_artifacts"]] + [""]
    L += ["## Evidence"] + ([statement_line(s) for s in by_class(a["statements"], ("observed", "synthetic"))] or ["- none"]) + [""]
    if a["computations"]:
        L += ["## Executed arithmetic"] + [
            f"- `{c['id']}`: {md_escape(c['expression'])} = {c['result']:g} {md_escape(c['unit'])} (recomputed)" for c in a["computations"]] + [""]
    L += ["## Opportunities"] + [f"- `{o['id']}`: \"{md_escape(o['framing'])}\" [{', '.join(o['statement_ids'])}]" for o in a["opportunities"]] + [""]
    L += ["## Ideas", "| Idea | Perspective | Opportunity | Status |", "|---|---|---|---|"]
    L += [f"| {md_escape(i['title'])}: {md_escape(i['summary'])} | {i['perspective']} | {i['opportunity_id']} | {i['status']} |" for i in a["ideas"]]
    L += ["", "## Idea decisions"] + [f"- `{i['id']}` **{i['status']}**: {md_escape(i['title'])}"
                                      + (f" — {md_escape(i['reason'])}" if "reason" in i else "") for i in a["ideas"]] + [""]
    L += ["## Checkpoints"] + [f"- {c['stage']}: {c['status']}" + (f" (source {c['answer_source_id']})" if "answer_source_id" in c else "")
                               + f" — {md_escape(c['question'])}" for c in a["checkpoints"]] + [""]
    L += ["## Assumptions, hypotheses and estimates"] + [statement_line(s) for s in by_class(a["statements"], ("assumption", "hypothesis", "estimate"))] + [""]
    L += ["## Evidence gaps"] + evidence_gap_lines(a, ctx) + [""]
    if a["questions"]:
        L += ["## Questions for the requester (material, not retrievable)"] + [f"- [{q['topic']}] {md_escape(q['text'])}" for q in a["questions"]] + [""]
    na = a["next_action"]
    L += ["## Next action", f"{md_escape(na['action'])} ({na['kind']}, {na['cost']}, owner {md_escape(na['owner_role'])})"
          + (f"; needs approval: {', '.join(na['approvals_needed'])}" if na["approvals_needed"] else ""), ""]
    files = {"discovery-brief.md": "\n".join(L)}

    order = sorted(a["assumptions"], key=lambda s: (s["action"] != "test", s.get("rank", 0), s["id"]))
    R = [f"# Assumption register ({a['mode']} product)", "", "Impact × risk: high/high = test, high/low = accept the risk, "
         "low/low = defer, low/high = drop the idea.", "",
         "| Rank | ID | Idea | Category | Assumption | Impact | Risk | Action | Statements |", "|---|---|---|---|---|---|---|---|---|"]
    for s in order:
        R.append(f"| {s.get('rank', '-')} | {s['id']} | {md_escape(ideas[s['idea_id']]['title'])} | {s['category']} | {md_escape(s['text'])} | "
                 f"{s['impact']} | {s['risk']} | {s['action']} | {', '.join(s['statement_ids']) or '-'} |")
    files["assumption-register.md"] = "\n".join(R) + "\n"

    X = header(f"Experiment plan ({a['mode']} product)", a, ctx)
    for x in a["experiments"]:
        h = x["hypothesis"]
        X += [f"## `{x['id']}` — {x['method']} (effort {x['effort']})", f"Tests: {', '.join(x['assumption_ids'])}",
              f"Setup: {md_escape(x['setup'])}",
              f"Hypothesis: {md_escape(h['population'])} — {md_escape(h['behavior_or_outcome'])}, {fmt_threshold(h['threshold'])} "
              f"within {h['window_days']} days after {md_escape(h['intervention'])}",
              f"Measured by: {md_escape(h['measurement'])}; counter-metric: {md_escape(h['counter_metric'])}",
              f"Failure rule: {md_escape(h['failure_rule'])}"]
        if "risk_mitigation" in x:
            X.append(f"Risk mitigation: {md_escape(x['risk_mitigation'])}")
        if x["approvals_needed"]:
            X.append(f"Approvals needed before running: {', '.join(x['approvals_needed'])}")
        X += [f"If it passes: {md_escape(x['if_pass'])}", f"If it fails: {md_escape(x['if_fail'])}",
              "Result: not run (unknown until measured; never simulated)", ""]
    if not a["experiments"]:
        X += ["No experiments yet: the assumption-focus checkpoint decides what to test.", ""]
    files["experiment-plan.md"] = "\n".join(X)
    return files


IDEA_DECISION_RE = re.compile(r"^- `([A-Za-z0-9][A-Za-z0-9_.:-]{0,79})` \*\*(proposed|carried|declined)\*\*: (.+?)(?: — .*)?$", re.M)


def discovery_decisions(md: str) -> Dict[str, Tuple[str, str]]:
    """idea id -> (status, title) from a rendered discovery brief's 'Idea decisions' section."""
    part = md.split("\n## Idea decisions\n", 1)
    if len(part) < 2:
        return {}
    body = part[1].split("\n## ", 1)[0]
    return {m.group(1): (m.group(2), m.group(3).replace("\\", "")) for m in IDEA_DECISION_RE.finditer(body)}


def _discovery_needs(a: dict, ctx: "Context") -> List[str]:
    out = _approval_needs(a)
    out += [f"checkpoint pending: {c['question']}" for c in a["checkpoints"] if c["status"] == "pending"]
    for x in a["experiments"]:
        out += [f"approval needed before experiment {x['id']}: {ap}" for ap in x["approvals_needed"]
                if ap not in a["next_action"]["approvals_needed"]]
    return out


# ---------------------------------------------------------------- lean PRD rubric (adapted PM Skills method)

LEAN_PRD_MAX_WORDS = 1000
OPPORTUNITY_KINDS = ("discovery-brief", "problem-brief", "decision-brief", "feature-brief")
VAGUE_WORDS = re.compile(r"\b(intuitive|user-friendly|seamless|easy|easier|better|improved?|improves|fast|faster|nice|good|works)\b", re.I)
CALENDAR = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.I)


def _declined_ideas(a: dict, ctx: "Context") -> Tuple[Dict[str, Tuple[str, str]], List[str]]:
    """Idea decisions from every reused discovery brief (content verified against its digest)."""
    decisions: Dict[str, Tuple[str, str]] = {}
    errs: List[str] = []
    for r in a["reused_artifacts"]:
        if r["kind"] != "discovery-brief" or ctx.store is None or ctx.store.get(r["artifact_id"]) is None:
            continue
        try:
            decisions.update(discovery_decisions(ctx.store.read_text(r["artifact_id"])))
        except cr.RegistryError as exc:
            errs.append(f"reused discovery brief {r['artifact_id']}: {exc}")
    return decisions, errs


def validate_lean_prd(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    errs = check_frame_reuse(a, OPPORTUNITY_KINDS, "a PRD")
    comps, e = check_computations(a["computations"], pack)
    errs += e
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_questions(a["questions"], pack) + check_next_action(a["next_action"])
    errs += check_refs("background", a["background_statement_ids"], stmts)
    for kr in a["objective"]["key_results"]:
        if kr["baseline_statement_id"] not in stmts:
            errs.append(f"key result {kr['id']}: baseline is not a statement")
    for sid in a["open_assumption_ids"]:
        if sid not in stmts:
            errs.append(f"7.4 cites unknown statement {sid}")
        elif stmts[sid]["class"] not in ("assumption", "hypothesis", "unknown", "estimate"):
            errs.append(f"7.4 lists {sid}, which is {stmts[sid]['class']}, not an open assumption")
    for s in a["statements"]:
        if s["class"] == "unknown" and s["id"] not in a["open_assumption_ids"]:
            errs.append(f"unknown {s['id']} is missing from 7.4 Assumptions and open questions")

    segments = {s["id"] for s in a["segments"]}
    vps = [v["segment_id"] for v in a["value_propositions"]]
    for v in vps:
        if v not in segments:
            errs.append(f"value proposition for unknown segment {v}")
    for sid in segments:
        if vps.count(sid) != 1:
            errs.append(f"segment {sid} has {'no' if not vps.count(sid) else 'more than one'} value proposition")
    features = {f["id"]: f for f in a["features"]}
    for f in a["features"]:
        for sid in f["segment_ids"]:
            if sid not in segments:
                errs.append(f"feature {f['id']}: unknown segment {sid}")
    for sid in segments:
        if not any(sid in f["segment_ids"] for f in a["features"]):
            errs.append(f"segment {sid} has no feature that delivers its value proposition")

    rel = a["release"]
    first = {x["id"]: x for x in rel["first_version"]}
    all_scope = rel["first_version"] + rel["later"]
    ids = [x["id"] for x in all_scope + rel["non_goals"]]
    if len(ids) != len(set(ids)):
        errs.append("release item ids must be unique")
    if CALENDAR.search(rel["timeframe"]):
        errs.append("release timeframe must be relative (sprints, weeks after start), not a calendar date")
    decisions, e = _declined_ideas(a, ctx)
    errs += e
    declined = {i: t for i, (st, t) in decisions.items() if st == "declined"}
    for x in all_scope:
        for fid in x.get("feature_ids", []):
            if fid not in features:
                errs.append(f"release item {x['id']}: unknown feature {fid}")
        if "idea_id" in x:
            if x["idea_id"] in declined:
                errs.append(f"release item {x['id']}: idea {x['idea_id']} was declined in discovery and must not reappear as scope")
            elif x["idea_id"] not in decisions:
                errs.append(f"release item {x['id']}: {x['idea_id']} is not an idea in a reused discovery brief")
        texts = [x["text"]] + [features[f]["text"] for f in x.get("feature_ids", []) if f in features]
        for iid, title in declined.items():
            if any(title.lower() in t.lower() for t in texts):
                errs.append(f"release item {x['id']}: '{title}' was declined in discovery ({iid}) and must not reappear as scope")

    covered = set()
    for ac in a["acceptance_criteria"]:
        cid = ac["id"]
        if ac["scope_id"] not in first:
            errs.append(f"acceptance criterion {cid}: {ac['scope_id']} is not first-version scope")
        covered.add(ac["scope_id"])
        tr = ac["trace"]
        if tr["kind"] == "evidence":
            for sid in tr["statement_ids"]:
                if sid not in stmts:
                    errs.append(f"acceptance criterion {cid} traces to unknown statement {sid}")
                elif stmts[sid]["class"] not in ("observed", "synthetic"):
                    errs.append(f"acceptance criterion {cid} traces to {sid}, a {stmts[sid]['class']}, not evidence; "
                                "trace it as a chosen requirement instead")
        has_number = bool(NUMBER_RE.search(ac["then"]))
        if ac["verification"] == "metric-check" and not has_number:
            errs.append(f"acceptance criterion {cid}: a metric check needs a number in 'then'")
        if not has_number and VAGUE_WORDS.search(ac["then"]):
            errs.append(f"acceptance criterion {cid}: '{ac['then']}' is not testable; state an observable result")
    for sid in first:
        if sid not in covered:
            errs.append(f"first-version item {sid} has no acceptance criterion")
    if a["mode"] == "full":
        if not a["risks"]:
            errs.append("full mode needs at least one risk with a mitigation")
        if not a["constraints"]:
            errs.append("full mode needs constraints")
    return errs


def render_lean_prd(a: dict, ctx: "Context") -> Dict[str, str]:
    st = {s["id"]: s for s in a["statements"]}
    segs = {s["id"]: s for s in a["segments"]}
    rel = a["release"]
    L = header(f"Product requirements: {a['feature_name']} ({a['mode']} mode)", a, ctx)
    L += ["## 1. Summary", md_escape(a["summary"]), "",
          "## 3. Background"] + [statement_line(st[i]) for i in a["background_statement_ids"]]
    L += ["Built on: " + ", ".join(f"`{r['artifact_id']}` ({r['kind']})" for r in a["reused_artifacts"]), ""]
    L += ["## 4. Objective", md_escape(a["objective"]["text"])]
    L += [f"- `{k['id']}` {md_escape(k['metric'])}: target {fmt_threshold(k['target'])} within {k['window_days']} days; "
          f"baseline `{k['baseline_statement_id']}` ({st[k['baseline_statement_id']]['class']})" for k in a["objective"]["key_results"]] + [""]
    L += ["## 5. Market segments"]
    for s in a["segments"]:
        L.append(f"- `{s['id']}` {md_escape(s['name'])}: {md_escape(s['job'])}"
                 + (f" Constraints: {'; '.join(md_escape(c) for c in s['constraints'])}" if s["constraints"] else ""))
    L += ["", "## 6. Value propositions"]
    for v in a["value_propositions"]:
        L += [f"### {md_escape(segs[v['segment_id']]['name'])}", f"- Why: {md_escape(v['why'])}", f"- What before: {md_escape(v['before'])}",
              f"- How: {md_escape(v['how'])}", f"- What after: {md_escape(v['after'])}", f"- Alternatives: {md_escape(v['alternatives'])}",
              f"**{md_escape(v['statement'])}**", ""]
    L += ["## 7. Solution", "7.2 Key features:"] + [f"- `{f['id']}` {md_escape(f['text'])}" for f in a["features"]]
    L += ["", "7.4 Assumptions and open questions:"] + [statement_line(st[i]) for i in a["open_assumption_ids"]]
    L += [f"- question: {md_escape(q['text'])}" for q in a["questions"]] + [""]
    L += ["## 8. Release", "First version:"] + [f"- `{x['id']}` {md_escape(x['text'])}" for x in rel["first_version"]]
    if rel["later"]:
        L += ["Later:"] + [f"- `{x['id']}` {md_escape(x['text'])}" for x in rel["later"]]
    L += ["Not doing:"] + [f"- `{x['id']}` {md_escape(x['text'])}" for x in rel["non_goals"]]
    L += [f"Timeframe: {md_escape(rel['timeframe'])}", ""]
    if a["constraints"]:
        L += ["## Constraints"] + [f"- {md_escape(c)}" for c in a["constraints"]] + [""]
    if a["risks"]:
        L += ["## Risks"] + [f"- `{r['id']}` {md_escape(r['text'])} Mitigation: {md_escape(r['mitigation'])}" for r in a["risks"]] + [""]
    files = {"lean-prd.md": "\n".join(L)}

    M = header(f"Acceptance map: {a['feature_name']}", a, ctx)
    M += ["| Criterion | Scope item | Given / when / then | Verification | Trace |", "|---|---|---|---|---|"]
    for ac in a["acceptance_criteria"]:
        tr = ac["trace"]
        trace = ("evidence: " + ", ".join(f"{s} ({st[s]['class']})" for s in tr["statement_ids"]) if tr["kind"] == "evidence"
                 else f"chosen requirement: {md_escape(tr['rationale'])}")
        M.append(f"| `{ac['id']}` | {ac['scope_id']} | Given {md_escape(ac['given'])}, when {md_escape(ac['when'])}, then "
                 f"{md_escape(ac['then'])} | {ac['verification']} | {trace} |")
    files["acceptance-map.md"] = "\n".join(M) + "\n"

    H = header(f"Implementation handoff (draft): {a['feature_name']}", a, ctx)
    H += ["This handoff carries no approval: implementation, issue publication, merge and release each need their own "
          "approval on the receiving path.", "",
          "## Proposed next reviewers"] + [f"- {t}" for t in a["handoff"]["targets"]] + [md_escape(a["handoff"]["notes"]), ""]
    H += ["## First-version scope and its criteria"]
    for x in rel["first_version"]:
        acs = [ac["id"] for ac in a["acceptance_criteria"] if ac["scope_id"] == x["id"]]
        H.append(f"- `{x['id']}` {md_escape(x['text'])} — criteria: {', '.join(acs)}")
    H += ["", "## Open before building"] + [statement_line(st[i]) for i in a["open_assumption_ids"]]
    H += [f"- question: {md_escape(q['text'])}" for q in a["questions"]]
    if a["left_out"]:
        H += ["", "Left out on purpose: " + "; ".join(md_escape(x) for x in a["left_out"])]
    files["implementation-handoff.md"] = "\n".join(H) + "\n"
    return files


def post_render_lean_prd(a: dict, files: Dict[str, str]) -> List[str]:
    if a["mode"] != "lean":
        return []
    body = files["lean-prd.md"].split("\n## 1. Summary\n", 1)[-1]
    words = len(body.split())
    return [f"lean mode rendered {words} words (> {LEAN_PRD_MAX_WORDS}): cut scope or ask for the full version"] \
        if words > LEAN_PRD_MAX_WORDS else []


# ---------------------------------------------------------------- design consultation rubric (adapted gstack method)

DESIGN_PRODUCT_KINDS = ("lean-prd", "opportunity-brief", "feature-brief", "design-brief")
RESEARCH_SOURCE_KINDS = {"interview-notes", "dataset"}


def _research_backed(ids: Iterable[str], stmts: Dict[str, dict], pack: Optional[Pack]) -> bool:
    """True when one cited statement is evidenced by a research source (interview notes, a dataset,
    or in a synthetic task a fixture tagged user-research)."""
    items = pack.items if pack else {}
    synthetic = bool(pack and pack.synthetic)  # a fixture never reads as research in a public/real task
    for sid in ids:
        s = stmts.get(sid)
        if not s or s["class"] not in (("observed", "synthetic") if synthetic else ("observed",)):
            continue
        for src in s["source_ids"]:
            it = items.get(src)
            if it and (it["kind"] in RESEARCH_SOURCE_KINDS
                       or (synthetic and it["kind"] == "synthetic-fixture" and "user-research" in it["topics"])):
                return True
    return False


def _font_errors(a: dict, ctx: "Context") -> List[str]:
    errs: List[str] = []
    items = ctx.pack.items if ctx.pack else {}
    synthetic = ctx.pack.synthetic if ctx.pack else a["binding"]["data_classification"] == "synthetic"
    roles = [r["role"] for r in a["typography"]["roles"]]
    if len(roles) != len(set(roles)):
        errs.append("typography: each role appears once")
    if not {"display", "body"} <= set(roles):
        errs.append("typography needs display and body roles")
    surface = a["surface_mode"]
    font_assets = {x["name"] for x in a["assets"] if x["kind"] == "font"}
    for r in a["typography"]["roles"]:
        fam, low = r["family"], r["family"].lower()
        if low in cd.BANNED_FACES:
            errs.append(f"typography {r['role']}: {fam} is banned in any role by the method")
        elif low in cd.OVERUSED_FACES and r["role"] == "display":
            errs.append(f"typography display: {fam} is overused as a display face; choose a face for these users")
        elif low in cd.OVERUSED_FACES and r["role"] in ("body", "label") and not (low in cd.FINE_AS_BODY and surface in ("operate", "read")):
            errs.append(f"typography {r['role']}: {fam} is not a body face for an {surface} surface "
                        "(the method allows only DM Sans, Instrument Sans or IBM Plex Sans there)")
        v = r["verification"]
        if v["status"] == "verified":
            src = v.get("source_id")
            it = items.get(src) if src else None
            if it is None:
                errs.append(f"typography {r['role']}: {fam} is marked verified but cites no supplied source")
            elif "font-license" not in it["topics"]:
                errs.append(f"typography {r['role']}: {fam} is verified by {src}, but {src} does not record a font licence")
            elif low not in it["excerpt"].lower():
                errs.append(f"typography {r['role']}: {fam} is verified by {src}, but {src} does not name it")
            elif not synthetic and it["classification"] != "real":
                errs.append(f"typography {r['role']}: a real task needs a real licence record for {fam}")
        elif "source_id" in v:
            errs.append(f"typography {r['role']}: pending verification cites {v['source_id']}; mark it verified once that "
                        "source records the licence, or drop the source while it is pending")
        if fam not in font_assets:
            errs.append(f"font {fam} has no rights entry in assets")
    for x in a["assets"]:
        if x["rights"] != "pending":  # mirrors the font rule: a licence record that names the asset
            it, topic = items.get(x["source_id"]), "font-license" if x["kind"] == "font" else "asset-license"
            if it is None:
                errs.append(f"asset {x['id']}: rights {x['rights']} need a supplied source, not {x['source_id']}")
            elif topic not in it["topics"]:
                errs.append(f"asset {x['id']}: rights {x['rights']} cite {x['source_id']}, which does not record an asset licence "
                            f"(topic {topic}); mark the rights pending until one is supplied")
            elif x["name"].lower() not in it["excerpt"].lower():
                errs.append(f"asset {x['id']}: rights {x['rights']} cite {x['source_id']}, which does not name {x['name']}")
            elif not synthetic and it["classification"] != "real":
                errs.append(f"asset {x['id']}: a real task needs a real licence record for {x['name']}")
        elif x["source_id"] != "none" and x["source_id"] not in items:
            errs.append(f"asset {x['id']}: unknown source {x['source_id']}")
    return errs


def _copy_errors(where: str, text: str) -> List[str]:
    return [f"{where}: {p}" for p in cd.copy_problems(text)]


def validate_design_consultation(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    errs: List[str] = []
    if not any(r["kind"] in DESIGN_PRODUCT_KINDS for r in a["reused_artifacts"]):
        errs.append("a design consultation needs an accepted current lean-prd / opportunity-brief / feature-brief / design-brief "
                    "reused by artifact ID and digest (the product brief is not re-derived here)")
    comps, e = check_computations(a["computations"], pack)
    errs += e
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_questions(a["questions"], pack) + check_next_action(a["next_action"])
    items = pack.items if pack else {}

    mt = a["memorable_thing"]
    if mt["status"] == "answered":
        if "text" not in mt:
            errs.append("memorable thing is answered but has no text")
        if mt.get("answer_source_id") not in items:
            errs.append("memorable thing is answered but cites no supplied answer")
        elif "text" in mt:
            it = items[mt["answer_source_id"]]
            norm = lambda t: " ".join(t.split()).lower()  # noqa: E731
            cls = answer_source_errors("memorable thing", mt["answer_source_id"], a, pack)
            errs += [e for e in cls if "cannot answer for" in e]
            if it["kind"] not in ANSWER_SOURCE_KINDS or norm(mt["text"]) not in norm(it.get("excerpt", "")):
                errs.append(f"memorable thing: {mt['answer_source_id']} does not record the answered text")
    else:
        if "text" in mt or "answer_source_id" in mt:
            errs.append("memorable thing is pending: the requester answers it, the analysis does not")
        if not any(q["topic"] == "memorable-thing" for q in a["questions"]):
            errs.append("memorable thing is pending: ask it as a question (topic memorable-thing)")

    us = a["use_scene"]
    errs += check_refs("use scene", us["statement_ids"], stmts)
    if (us["theme"], us["lighting"]) in (("dark", "bright"), ("light", "dim")):
        errs.append(f"use scene: theme {us['theme']} does not fit {us['lighting']} lighting (light or dark follows the use scene)")
    need = {"light": {"light"}, "dark": {"dark"}, "both": {"light", "dark"}}[us["theme"]]
    for t in sorted(need - set(a["color"])):
        errs.append(f"theme {us['theme']} needs a {t} palette")
    for t in sorted(set(a["color"]) - need):
        errs.append(f"theme {us['theme']}: remove the {t} palette or change the theme")
    for t, tokens in sorted(a["color"].items()):
        for fg, bg, r, mn in cd.token_contrast(tokens):
            if r < mn:
                errs.append(f"{t}: {fg} on {bg} is {r:.2f}:1, below {mn:g}:1")

    b = a["brief"]
    errs += check_refs("brief audience", b["audience_statement_ids"], stmts)
    if not any(stmts.get(s, {}).get("class") in ("observed", "synthetic") for s in b["audience_statement_ids"]):
        errs.append("brief audience cites no evidenced statement")
    research_fields = [("brief.problem", b["problem"], []), ("brief.audience", b["audience"], b["audience_statement_ids"]),
                       ("direction.rationale", a["direction"]["rationale"], []), ("use_scene", us["who"] + " " + us["where"], us["statement_ids"])]
    research_fields += [(f"brief.{k}[{i}]", g, []) for k in ("goals", "constraints", "non_goals") for i, g in enumerate(b[k])]
    research_fields += [("direction.deliberate_risks", r["text"] + " " + r["why"], []) for r in a["direction"]["deliberate_risks"]]

    sp = a["spacing_px"]
    if sp != sorted(sp):
        errs.append("spacing_px must ascend")
    rd = a["radius_px"]
    vals = [rd["small"], rd["medium"], rd["large"]]
    if vals != sorted(vals):
        errs.append(f"radius_px must rise small <= medium <= large, got {vals}")
    elif len(set(vals)) == 1 and vals[0] >= 12:
        errs.append(f"radius_px: uniform {vals[0]}px radius on every element is a method anti-pattern; use a hierarchy")
    if a["motion"]["short_ms"] >= a["motion"]["long_ms"]:
        errs.append("motion: short_ms must be shorter than long_ms")

    bps = a["responsive"]["breakpoints"]
    widths = [bp["min_width_px"] for bp in bps]
    if widths[0] != 0:
        errs.append("responsive: the first breakpoint must start at 0px (mobile first)")
    if widths != sorted(set(widths)) or len({bp["name"] for bp in bps}) != len(bps):
        errs.append("responsive: breakpoints must be ascending with distinct names")
    if not any(w >= 1024 for w in widths):
        errs.append("responsive: no breakpoint at or above 1024px for desktop")

    topics = [r["topic"] for r in a["accessibility"]["requirements"]]
    for t in cd.REQUIRED_A11Y:
        if t not in topics:
            errs.append(f"accessibility: no {t} requirement")
    for r in a["accessibility"]["requirements"]:
        if r["topic"] in cd.NOT_STATIC_A11Y and r["verification"] == "static":
            errs.append(f"accessibility {r['id']}: {r['topic']} cannot be verified statically; use browser, assistive-tech or usability-test")
    if len({r["id"] for r in a["accessibility"]["requirements"]}) != len(topics):
        errs.append("accessibility: duplicate requirement id")

    screens: Dict[str, dict] = {}
    for sc in a["screens"]:
        if sc["id"] in screens:
            errs.append(f"screen {sc['id']}: duplicate id")
        screens[sc["id"]] = sc
        primaries = sum(1 for x in sc["blocks"] if x["kind"] == "action" and x["emphasis"] == "primary")
        if primaries != 1:
            errs.append(f"screen {sc['id']} has {primaries} primary actions (one primary action per view)")
        errs += _copy_errors(f"screen {sc['id']} name/purpose", sc["name"] + " " + sc["purpose"])
        for i, x in enumerate(sc["blocks"]):
            where = f"screen {sc['id']} block {i + 1}"
            texts = [x.get(k, "") for k in ("text", "label", "hint", "caption", "value")] + x.get("columns", []) + [c for r in x.get("rows", []) for c in r]
            errs += _copy_errors(where, " ".join(texts))
            if x["kind"] == "action" and x["label"].strip().lower() in cd.GENERIC_CTAS:
                errs.append(f"{where}: '{x['label']}' is a generic call to action; name the outcome the click buys")
            if x["kind"] == "table":
                for ri, row in enumerate(x["rows"]):
                    if len(row) != len(x["columns"]):
                        errs.append(f"screen {sc['id']}: table row {ri + 1} has {len(row)} cells for {len(x['columns'])} columns")
            if x["kind"] in ("metric", "claim"):
                s = stmts.get(x["statement_id"])
                what = f"metric '{x['label']}'" if x["kind"] == "metric" else "claim"
                if s is None:
                    errs.append(f"screen {sc['id']}: {what} cites unknown statement {x['statement_id']}")
                    continue
                if s["class"] not in ("observed", "synthetic"):
                    errs.append(f"screen {sc['id']}: {what} cites {s['id']}, a {s['class']}, not evidence")
                    continue
                allowed = numbers_in(s["text"])
                shown = x["label"] + " " + x["value"] if x["kind"] == "metric" else x["text"]
                for n in numbers_in(shown):
                    if not any(_same(n, v) for v in allowed):
                        errs.append(f"screen {sc['id']}: {what} value {n:g} is not in {s['id']}")
                if x["kind"] == "claim" and cd.research_claim(x["text"]) and not _research_backed([s["id"]], stmts, pack):
                    errs.append(f"screen {sc['id']}: claim reads as user research but {s['id']} cites no interview or dataset source")
            elif x["kind"] in ("text", "heading"):
                research_fields.append((where, x["text"], []))

    seen = set()
    by_screen: Dict[str, set] = {}
    for st in a["states"]:
        key = (st["screen_id"], st["state"])
        where = f"{st['screen_id']}/{st['state']}"
        if key in seen:
            errs.append(f"{where}: duplicate state")
        seen.add(key)
        if st["screen_id"] not in screens:
            errs.append(f"state for unknown screen {st['screen_id']}")
            continue
        by_screen.setdefault(st["screen_id"], set()).add(st["state"])
        if st["state"] == "default":
            if "message" in st or "recovery_action" in st:
                errs.append(f"{where}: the default state is the screen itself; no message")
            continue
        if "message" not in st:
            errs.append(f"{where} needs a message")
            continue
        if st["state"] in cd.RECOVERY_STATES and "recovery_action" not in st:
            errs.append(f"{where} needs a recovery action")
        errs += _copy_errors(where, st["message"] + " " + st.get("recovery_action", ""))
        if st["state"] == "success" and "!" in st["message"]:
            errs.append(f"{where}: no exclamation marks in success messages")
        research_fields.append((f"state {where}", st["message"], []))
    for sid, sc in screens.items():
        have = by_screen.get(sid, set())
        for need_state in cd.REQUIRED_STATES:
            if need_state not in have:
                errs.append(f"screen {sid} has no {need_state} state")
        if sc["requires_permission"] and "permission-denied" not in have:
            errs.append(f"screen {sid} requires permission but has no permission-denied state")

    on_journey = set()
    for j in a["journey"]:
        errs += check_refs(f"journey {j['id']}", j["statement_ids"], stmts)
        if j["screen_id"] not in screens:
            errs.append(f"journey {j['id']}: unknown screen {j['screen_id']}")
            continue
        on_journey.add(j["screen_id"])
        if j["failure_state"] not in by_screen.get(j["screen_id"], set()):
            errs.append(f"journey {j['id']}: failure state {j['failure_state']} is not designed for screen {j['screen_id']}")
        research_fields.append((f"journey {j['id']}", " ".join((j["goal"], j["action"], j["success_signal"])), j["statement_ids"]))
    if len({j["id"] for j in a["journey"]}) != len(a["journey"]):
        errs.append("journey: duplicate step id")
    for sid in screens:
        if sid not in on_journey:
            errs.append(f"screen {sid} is not on the user journey")
    errs += _copy_errors("product name", a["product_name"])

    for where, text, ids in research_fields:
        hit = cd.research_claim(text)
        if hit and not _research_backed(ids, stmts, pack):
            errs.append(f"{where} reads as user research ('{hit}') but cites no interview or dataset source; "
                        "state it as an assumption or hypothesis, or supply the research")
    errs += _font_errors(a, ctx)
    return errs


def _consult_needs(a: dict, ctx: "Context") -> List[str]:
    out = _approval_needs(a)
    if a["memorable_thing"]["status"] == "pending":
        out.append("memorable thing pending: the requester names the one thing people should remember")
    out += [f"font {r['family']} ({r['role']}) pending verification: licence and loading source not verified"
            for r in a["typography"]["roles"] if r["verification"]["status"] == "pending"]
    out += [f"asset {x['name']} rights pending: record the licence before use" for x in a["assets"] if x["rights"] == "pending"]
    return out


def _st_label(st: Dict[str, dict], sid: str) -> str:
    s = st.get(sid)
    return f"`{sid}` ({s['class']})" if s else f"`{sid}`"


def render_design_consultation(a: dict, ctx: "Context") -> Dict[str, str]:
    st = {s["id"]: s for s in a["statements"]}
    us, b, d, mt = a["use_scene"], a["brief"], a["direction"], a["memorable_thing"]
    L = header(f"Design brief: {a['product_name']}", a, ctx)
    L += ["## Product brief reused"] + [f"- `{r['artifact_id']}` ({r['kind']}, sha256 `{r['digest'][:16]}…`)" for r in a["reused_artifacts"]] + [""]
    L += ["## Problem", md_escape(b["problem"]), "", "## Audience", md_escape(b["audience"]) + " Evidence: "
          + ", ".join(_st_label(st, s) for s in b["audience_statement_ids"]), "",
          "## Use scene", f"{md_escape(us['who'])}; {md_escape(us['where'])}. Lighting {us['lighting']} → theme **{us['theme']}** "
          "(light or dark follows the use scene).", "",
          "## Memorable thing",
          (f"{md_escape(mt['text'])} (requester answer, source {mt['answer_source_id']})" if mt["status"] == "answered"
           else "pending: the requester has not answered; every later decision should serve the answer"), "",
          "## Surface mode", a["surface_mode"], ""]
    L += ["## Goals"] + [f"- {md_escape(x)}" for x in b["goals"]] + ["", "## Constraints"] + ([f"- {md_escape(x)}" for x in b["constraints"]] or ["- none recorded"])
    L += ["", "## Non-goals"] + ([f"- {md_escape(x)}" for x in b["non_goals"]] or ["- none recorded"]) + [""]
    L += ["## Direction", f"- Aesthetic {d['aesthetic']}; decoration {d['decoration']}; layout {d['layout']}; colour {d['color_approach']}; motion {d['motion']}",
          f"- Why: {md_escape(d['rationale'])}"] + [f"- Deliberate risk: {md_escape(r['text'])} Because {md_escape(r['why'])}" for r in d["deliberate_risks"]]
    notes = _coherence_notes(a)
    if notes:
        L += [f"- Coherence note (never blocks): {n}" for n in notes]
    L += ["", "## User journey", "| Step | Goal | Screen | Action | Success signal | If it fails | Evidence |", "|---|---|---|---|---|---|---|"]
    names = {s["id"]: s["name"] for s in a["screens"]}
    for j in a["journey"]:
        L.append(f"| `{j['id']}` | {md_escape(j['goal'])} | {md_escape(names.get(j['screen_id'], j['screen_id']))} | {md_escape(j['action'])} | "
                 f"{md_escape(j['success_signal'])} | {j['failure_state']} state | {', '.join(j['statement_ids']) or '-'} |")
    L += ["", "## Evidence"] + ([statement_line(s) for s in by_class(a["statements"], ("observed", "synthetic"))] or ["- none"])
    L += ["", "## Assumptions and hypotheses"] + ([statement_line(s) for s in by_class(a["statements"], ("assumption", "hypothesis", "estimate"))] or ["- none"])
    L += ["", "## Unknowns"] + evidence_gap_lines(a, ctx) + [""]
    if a["questions"]:
        L += ["## Questions for the requester"] + [f"- [{q['topic']}] {md_escape(q['text'])}" for q in a["questions"]] + [""]
    na = a["next_action"]
    L += ["## Next action", f"{md_escape(na['action'])} ({na['kind']}, {na['cost']}, owner {md_escape(na['owner_role'])})", ""]
    files = {"design-brief.md": "\n".join(L)}

    P = header(f"Design package: {a['product_name']}", a, ctx)
    P += ["Files: `design-tokens.json` (machine-readable tokens), `design-preview.html` (self-contained preview: no script, no "
          "remote asset, no web fonts).", ""]
    P += ["## Colour tokens"]
    for t, tokens in sorted(a["color"].items()):
        P += [f"### {t} theme", "| Token | Value |", "|---|---|"] + [f"| `--color-{k}` | `{tokens[k].lower()}` |" for k in cd.TOKEN_NAMES]
        P += ["", "Contrast, computed from the tokens:"] + [f"- {fg} on {bg}: {r:.2f}:1 (min {mn:g}:1) {'pass' if r >= mn else 'FAIL'}"
                                                             for fg, bg, r, mn in cd.token_contrast(tokens)] + [""]
    ty = a["typography"]
    scale = cd.type_scale(ty["base_px"], ty["ratio"])
    P += ["## Typography"] + [
        f"- {r['role']}: {md_escape(r['family'])} ({r['fallback']} fallback), weights {', '.join(str(w) for w in r['weights'])}; "
        + (f"verified by {r['verification']['source_id']}" + (" (synthetic fixture)" if ctx.pack and ctx.pack.items.get(r['verification']['source_id'], {}).get('classification') == 'synthetic' else "")
           if r["verification"]["status"] == "verified" else "pending verification (the preview shows the fallback face)")
        for r in ty["roles"]]
    P += [f"- Scale: base {ty['base_px']}px × {ty['ratio']:g} → {', '.join(f'{v}px' for v in scale)}; body line height {ty['body_line_height']:g}; "
          f"measure {ty['measure_ch']} characters", ""]
    P += ["## Spacing, radius and motion", f"- Spacing scale: {', '.join(f'{v}px' for v in a['spacing_px'])}",
          f"- Radius: small {a['radius_px']['small']}px, medium {a['radius_px']['medium']}px, large {a['radius_px']['large']}px",
          f"- Motion: {a['motion']['short_ms']}ms / {a['motion']['long_ms']}ms; enter `{a['motion']['enter_easing']}`, move `{a['motion']['move_easing']}`; "
          "reduced motion shows the final state", ""]
    P += ["## Interaction states", "| Screen | State | Message | Recovery |", "|---|---|---|---|"]
    for s in a["states"]:
        P.append(f"| {s['screen_id']} | {s['state']} | {md_escape(s.get('message', '(the screen itself)'))} | {md_escape(s.get('recovery_action', '-'))} |")
    P += ["", "## Responsive behaviour", "| Breakpoint | From | Columns | Navigation | Notes |", "|---|---|---|---|---|"]
    P += [f"| {bp['name']} | {bp['min_width_px']}px | {bp['columns']} | {bp['navigation']} | {md_escape(bp['notes'])} |" for bp in a["responsive"]["breakpoints"]]
    P += [f"Max content width {a['responsive']['max_content_width_px']}px.", ""]
    static_ok = {"contrast": "contrast computed from the tokens: pass", "focus-visible": "focus ring rule present in the preview CSS",
                 "reduced-motion": "reduced-motion rule present in the preview CSS", "landmarks": "landmarks and skip link present in the preview markup",
                 "form-errors": "aria-invalid/aria-describedby wiring present in the preview markup",
                 "non-color-cues": "state messages carry a text prefix in the preview"}
    P += ["## Accessibility requirements (target WCAG 2.2 AA)"]
    for r in a["accessibility"]["requirements"]:
        status = (f"static check on the preview: {static_ok[r['topic']]}" if r["verification"] == "static" and r["topic"] in static_ok
                  else f"not verified (needs {r['verification']})")
        P.append(f"- `{r['id']}` {r['topic']}: {md_escape(r['requirement'])} — {status}")
    P += ["", "## Asset rights"] + ([f"- {x['kind']} {md_escape(x['name'])}: {x['rights']} ({md_escape(x['license'])}; source {x['source_id']})"
                                    for x in a["assets"]] or ["- no assets"]) + [
        "- The preview embeds no images, icons, illustrations, logos or font files.", ""]
    P += ["## Not verified",
          "- Browser render and screenshots: not run (the preview is checked statically only).",
          "- Assistive technology and keyboard walk-through: not run.",
          "- Usability with real users: not run.",
          "- Typefaces are not loaded; the preview uses them only when installed locally.", ""]
    files["design-package.md"] = "\n".join(P)

    tokens = {"schema": "design-tokens.v1", "product_name": a["product_name"], "binding": a["binding"],
              "themes": {t: {k: v.lower() for k, v in tok.items()} for t, tok in a["color"].items()},
              "spacing_px": a["spacing_px"], "radius_px": a["radius_px"],
              "type": {"roles": [{"role": r["role"], "family": r["family"], "fallback": r["fallback"], "weights": r["weights"],
                                  "verification": r["verification"]["status"]} for r in ty["roles"]],
                       "base_px": ty["base_px"], "ratio": ty["ratio"], "body_line_height": ty["body_line_height"], "measure_ch": ty["measure_ch"]},
              "type_scale_px": scale, "motion": a["motion"], "breakpoints": a["responsive"]["breakpoints"],
              "max_content_width_px": a["responsive"]["max_content_width_px"]}
    files["design-tokens.json"] = json.dumps(tokens, indent=2, ensure_ascii=False) + "\n"
    prov = ("Built from a coordinator-authored analysis by scripts/capability_workflow.py, which adds no facts.")
    files["design-preview.html"] = cd.build_preview(a, synthetic=a["binding"]["data_classification"] == "synthetic",
                                                    statements=st, provenance=prov)
    return files


def _coherence_notes(a: dict) -> List[str]:
    d = a["direction"]
    out = []
    if d["aesthetic"] in ("brutally-minimal", "brutalist") and d["motion"] == "expressive":
        out.append("a minimal/brutalist look with expressive motion: consider quieter motion, or keep it on purpose")
    if d["color_approach"] == "drenched" and d["decoration"] == "minimal":
        out.append("drenched colour with minimal decoration: consider supporting decoration")
    if d["layout"] == "creative-editorial" and a["surface_mode"] == "operate":
        out.append("an editorial layout on an operate surface: consider a hybrid layout")
    return out


def post_render_design_consultation(a: dict, files: Dict[str, str]) -> List[str]:
    return cd.preview_errors(a, files["design-preview.html"], synthetic=a["binding"]["data_classification"] == "synthetic")


# ---------------------------------------------------------------- design review rubric (adapted gstack method, audit only)

REVIEWABLE_KINDS = ("design-package", "ui-evidence", "design-brief")
COVERAGE = {"browser_render": ("screenshot", "browser", "Browser render and screenshots"),
            "assistive_tech": ("accessibility-scan", "assistive-technology", "Assistive technology"),
            "usability_test": ("usability-session", "usability", "Usability test")}
VERIFICATION_COVERAGE = {"browser": "browser_render", "assistive-tech": "assistive_tech", "usability-test": "usability_test"}
NEEDS_BROWSER = {"hierarchy", "performance"}
GRADE_WEIGHTS = {"hierarchy": 15, "typography": 15, "spacing-layout": 15, "color-contrast": 10, "interaction-states": 10,
                 "responsive": 10, "content": 10, "ai-slop": 5, "motion": 5, "performance": 5}


def design_static_checks(a: dict, ctx: "Context") -> Dict[str, Dict[str, Dict[str, Any]]]:
    """artifact_id -> check_id -> result, from the reviewed artifacts' bytes (re-read and digest-verified)."""
    cache = ctx.extra.setdefault("design_checks", {}) if ctx.extra is not None else {}
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    if ctx.store is not None and ctx.store.bind(a["binding"]):
        raise WorkflowError("the artifact index was loaded for a different task binding")
    for r in a.get("reviewed_artifacts", []):
        aid = r["artifact_id"]
        if ctx.store is None or ctx.store.get(aid) is None:
            continue
        art = ctx.store.get(aid)
        key = (aid, art["content_digest"])
        if key not in cache:
            text = ctx.store.read_text(aid)
            loc = art["location"].lower()
            if loc.endswith((".html", ".htm")):
                cache[key] = cd.audit_html(text)
            elif loc.endswith(".json"):
                doc = cr.parse_json(text)
                cache[key] = cd.audit_tokens(doc) if isinstance(doc, dict) and doc.get("schema") == "design-tokens.v1" else {}
            else:
                cache[key] = {}
        out[aid] = cache[key]
    return out


def _capture_errors(a: dict, ctx: "Context") -> Tuple[Dict[str, List[str]], List[str]]:
    errs: List[str] = []
    supplied: Dict[str, List[str]] = {}
    items = ctx.pack.items if ctx.pack else {}
    for key, (topic, label, _) in COVERAGE.items():
        cov = a["evidence_coverage"][key]
        if cov["status"] == "not-run":
            if cov["source_ids"]:
                errs.append(f"evidence_coverage.{key} is not-run but lists sources")
            continue
        if not cov["source_ids"]:
            errs.append(f"evidence_coverage.{key} is supplied but lists no captured source")
        ok = []
        for sid in cov["source_ids"]:
            it = items.get(sid)
            if it is None:
                errs.append(f"evidence_coverage.{key}: unknown source {sid}")
                continue
            if it["classification"] != "real":
                errs.append(f"{sid} is synthetic: a fixture is not {label} evidence")
                continue
            if it["kind"] != "artifact" or "captured_file" not in it:
                errs.append(f"{sid} is not a captured file (kind artifact with captured_file and digest)")
                continue
            if topic not in it["topics"]:
                errs.append(f"{sid} is not tagged {topic}")
                continue
            if key == "browser_render":
                try:
                    head = cr.safe_repo_file(ctx.pack.root, it["captured_file"], max_bytes=MAX_CAPTURE_BYTES)[:8]
                except cr.RegistryError as exc:
                    errs.append(f"{sid}: {exc}")
                    continue
                if not (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")):
                    errs.append(f"{sid} is not a PNG or JPEG image")
                    continue
            ok.append(sid)
        supplied[key] = ok
    return supplied, errs


def validate_design_review(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    errs: List[str] = []
    comps, e = check_computations(a["computations"], pack)
    errs += e
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_questions(a["questions"], pack) + check_next_action(a["next_action"])
    store = ctx.store
    if store is None:
        return errs + ["a design review needs the artifact index of the reviewed artifacts (--artifacts)"]
    currency = cr._artifact_currency(store.index["artifacts"], {c["id"]: c for c in ctx.registry["capabilities"]})
    b = a["binding"]
    reviewed = {}
    for r in a["reviewed_artifacts"]:
        art = store.get(r["artifact_id"])
        if art is None:
            errs.append(f"reviewed artifact {r['artifact_id']} is not in the artifact index")
            continue
        e = scope_errors(art, b, f"reviewed artifact {r['artifact_id']}")
        if e:
            errs += e
            continue
        if art["content_digest"] != r["digest"] or art["kind"] != r["kind"]:
            errs.append(f"reviewed artifact {r['artifact_id']}: kind/digest differs from the index")
            continue
        if art["kind"] not in REVIEWABLE_KINDS:
            errs.append(f"reviewed artifact {r['artifact_id']} is a {art['kind']}, not a design package, design brief or UI evidence")
        ok, why = currency[art["artifact_id"]]
        if not ok:
            errs.append(f"reviewed artifact {r['artifact_id']} is stale: {why}")
        reviewed[r["artifact_id"]] = art
    if not any(x["kind"] in ("design-package", "ui-evidence") for x in reviewed.values()):
        errs.append("a design review inspects at least one design package or UI evidence artifact")
    if errs:
        return errs
    try:
        checks = design_static_checks(a, ctx)
        texts = {aid: store.read_text(aid) for aid in reviewed}
    except (cr.RegistryError, UnicodeDecodeError) as exc:
        return errs + [f"a reviewed artifact cannot be inspected: {exc}"]
    supplied, e = _capture_errors(a, ctx)
    errs += e
    browser = set(supplied.get("browser_render", []))
    all_caps = {sid for v in supplied.values() for sid in v}

    cited: set = set()
    ids = set()
    for f in a["findings"]:
        fid = f["id"]
        if fid in ids:
            errs.append(f"finding {fid}: duplicate id")
        ids.add(fid)
        static_ev = False
        caps = set()
        for ev in f["evidence"]:
            k = ev["kind"]
            if k in ("static-check", "excerpt") and ev["artifact_id"] not in reviewed:
                errs.append(f"{fid} cites {ev['artifact_id']}, which is not a reviewed artifact")
                continue
            if k == "static-check":
                res = checks.get(ev["artifact_id"], {}).get(ev["check_id"])
                if res is None:
                    errs.append(f"{fid} cites {ev['check_id']}, which no static check on {ev['artifact_id']} produced")
                elif res["status"] == "not-evaluated":
                    errs.append(f"{fid} cites {ev['check_id']}, which static checks cannot evaluate")
                elif res["status"] != "fail":
                    errs.append(f"{fid} cites {ev['check_id']}, which {'passed' if res['status'] == 'pass' else 'does not apply'} on {ev['artifact_id']}")
                else:
                    static_ev = True
                    cited.add((ev["artifact_id"], ev["check_id"]))
            elif k == "excerpt":
                if ev["text"] not in texts[ev["artifact_id"]]:
                    errs.append(f"{fid} quotes text that is not in {ev['artifact_id']}")
                else:
                    static_ev = True
            elif k == "capture":
                if ev["source_id"] not in all_caps:
                    errs.append(f"{fid} cites capture {ev['source_id']}, which is not supplied browser, assistive-technology or usability evidence")
                else:
                    caps.add(ev["source_id"])
            elif ev["statement_id"] not in stmts:
                errs.append(f"{fid} cites unknown statement {ev['statement_id']}")
        v = f["verification"]
        if v == "static" and not static_ev:
            errs.append(f"{fid} is static but cites no failing check or excerpt")
        if v in VERIFICATION_COVERAGE:
            key = VERIFICATION_COVERAGE[v]
            if not caps & set(supplied.get(key, [])):
                errs.append(f"{fid} claims {v.replace('-', ' ')} verification but cites no {COVERAGE[key][1]} capture")
        if f["category"] in NEEDS_BROWSER and v != "browser":
            errs.append(f"{fid}: {f['category']} needs browser evidence (a rendered screenshot); a static review cannot judge it")
        claim_errs = []
        for field in ("title", "notice", "because", "recommendation"):
            for hit, need in cd.research_claims(f[field]):
                if v == "static" or not caps:
                    claim_errs.append(f"{fid} reads as a usability, screen-reader or screenshot result ('{hit}') but cites no supplied capture")
                elif need == "assistive_tech" and not caps & set(supplied.get(need, [])):
                    claim_errs.append(f"{fid} reads as an assistive-technology result ('{hit}') but cites no supplied assistive-technology "
                                      "capture (a screenshot does not show what assistive technology announced)")
                elif need == "usability_test" and not caps & set(supplied.get(need, [])):
                    claim_errs.append(f"{fid} reads as a usability or user-observation result ('{hit}') but cites no supplied usability "
                                      "capture (a screenshot does not show what people did or said)")
        errs += list(dict.fromkeys(claim_errs))
        if _is_patch(f["recommendation"]):
            errs.append(f"{fid} recommendation is a patch; a review recommends, the engineering path edits")
    waived = set()
    for w in a["waived_checks"]:
        res = checks.get(w["artifact_id"], {}).get(w["check_id"])
        if w["artifact_id"] not in reviewed or res is None or res["status"] != "fail":
            errs.append(f"waives {w['check_id']}, which did not fail on {w['artifact_id']}")
        waived.add((w["artifact_id"], w["check_id"]))
    for aid, res in sorted(checks.items()):
        for cid, r in sorted(res.items()):
            if r["status"] == "fail" and r["severity"] in ("high", "medium") and (aid, cid) not in cited | waived:
                errs.append(f"{cid} failed on {aid} ({r['severity']}) but no finding cites it and it is not waived")
    for q in a["quick_wins"]:
        if q not in ids:
            errs.append(f"quick win {q} is not a finding")
    fi = a.get("first_impression")
    if fi is not None and fi["source_id"] not in browser:
        errs.append("first impression needs a supplied browser screenshot (evidence_coverage.browser_render)")
    return errs


def _is_patch(text: str) -> bool:
    return bool(cd.PATCH_RE.search(text))


def _grade(points: float) -> str:
    return "ABCDF"[min(4, int(math.ceil(4 - points - 1e-9)))]


def design_grades(a: dict, browser: bool, checks: Dict[str, Dict[str, Dict[str, Any]]]
                  ) -> Tuple[Dict[str, Tuple[Optional[str], float, str]], Optional[str]]:
    """category -> (letter or None, points, note), and the weighted score over graded categories.
    A category is graded only when something looked at it: a finding, a static check that ran on a
    reviewed artifact, or (for every category) supplied browser evidence. Silence is not an A."""
    looked = {r["category"] for res in checks.values() for r in res.values() if r["status"] in ("pass", "fail")}
    out: Dict[str, Tuple[Optional[str], float, str]] = {}
    for cat in list(GRADE_WEIGHTS) + ["accessibility"]:
        fs = [f for f in a["findings"] if f["category"] == cat]
        pts = max(0.0, 4.0 - sum(1.0 for f in fs if f["impact"] == "high") - sum(0.5 for f in fs if f["impact"] == "medium"))
        if cat in NEEDS_BROWSER and not browser:
            out[cat] = (None, pts, "needs browser_render evidence")
        elif not browser and not fs and cat not in looked:
            out[cat] = (None, pts, "not assessed: no static check covers it and no browser evidence was supplied")
        else:
            out[cat] = (_grade(pts), pts, "" if browser else "static evidence only")
    graded = [(GRADE_WEIGHTS[c], out[c][1]) for c in GRADE_WEIGHTS if out[c][0] is not None]
    score = _grade(sum(w * p for w, p in graded) / sum(w for w, _ in graded)) if graded else None
    return out, score


def render_design_review(a: dict, ctx: "Context") -> Dict[str, str]:
    st = {s["id"]: s for s in a["statements"]}
    checks = design_static_checks(a, ctx)
    supplied, _ = _capture_errors(a, ctx)
    browser = bool(supplied.get("browser_render"))
    waived = {(w["artifact_id"], w["check_id"]): w["reason"] for w in a["waived_checks"]}
    L = header("Design review report (audit only)", a, ctx)
    L += ["## Scope", f"Question: {md_escape(a['review_question'])}", f"Surface mode: {a['surface_mode']}", "Reviewed artifacts (re-read by digest):"]
    for r in a["reviewed_artifacts"]:
        art = ctx.store.get(r["artifact_id"])
        L.append(f"- `{r['artifact_id']}` ({r['kind']}, `{art['location']}`, sha256 `{r['digest'][:16]}…`, review status {art['review_status']})")
    L += ["", "## Not verified"]
    for key, (topic, label, title) in COVERAGE.items():
        got = supplied.get(key, [])
        L.append(f"- {title}: " + (f"supplied by the coordinator ({', '.join(got)}; file digest verified; not rendered or viewed by this tool)"
                                   if got else "not run"))
    L += ["- Static checks cannot evaluate: " + ", ".join(sorted(cd.NOT_STATIC)) + ".", ""]
    if "first_impression" in a:
        fi = a["first_impression"]
        L += ["## First impression", f"Source: {fi['source_id']} (supplied screenshot)",
              f"- The page communicates **{md_escape(fi['communicates'])}**.", f"- I notice **{md_escape(fi['notice'])}**.",
              "- The first three things my eye goes to: " + ", ".join(f"**{md_escape(x)}**" for x in fi["eye_path"]) + ".",
              f"- In one word: **{md_escape(fi['one_word'])}**.", ""]
    total = {"pass": 0, "fail": 0, "waived": 0, "not-evaluated": 0, "not-applicable": 0}
    lines = []
    for aid, res in sorted(checks.items()):
        for cid, r in sorted(res.items()):
            status = r["status"]
            if status == "fail" and (aid, cid) in waived:
                total["waived"] += 1
                tail = f"**fail, waived**: {md_escape(waived[(aid, cid)])}"
            else:
                total[status] = total.get(status, 0) + 1
                tail = f"**{status}**"
            detail = "; ".join(md_escape(x) for x in r["details"][:4])
            lines.append(f"- `{cid}` on `{aid}`: {tail} [{r['severity']}, {r['category']}]" + (f" — {detail}" if detail else ""))
    L += ["## Static checks",
          f"{sum(total.values())} checks on {len(checks)} artifact(s): {total['pass']} passed, {total['fail']} failed, {total['waived']} waived, "
          f"{total['not-evaluated']} not evaluated statically, {total['not-applicable']} not applicable. Static checks read markup, CSS and "
          "tokens only; they are not a browser or accessibility-tool run."] + lines + [""]
    L += ["## Findings"]
    if not a["findings"]:
        L.append("- none")
    for f in a["findings"]:
        ev = []
        for e in f["evidence"]:
            if e["kind"] == "static-check":
                ev.append(f"static check `{e['check_id']}` on `{e['artifact_id']}`")
            elif e["kind"] == "excerpt":
                ev.append(f"excerpt from `{e['artifact_id']}`: \"{md_escape(e['text'])}\"")
            elif e["kind"] == "capture":
                ev.append(f"capture {e['source_id']}")
            else:
                ev.append(f"statement {_st_label(st, e['statement_id'])}")
        L += [f"### `{f['id']}` {md_escape(f['title'])} ({f['impact']}, {f['category']}, verified: {f['verification']})",
              f"- I notice {md_escape(f['notice'])}", f"- I think it matters because {md_escape(f['because'])}",
              f"- What if: {md_escape(f['recommendation'])}", "- Evidence: " + "; ".join(ev), ""]
    grades, score = design_grades(a, browser, checks)
    L += ["## Grades", "Each category starts at 4 points (A); a high finding subtracts 1 point and a medium 0.5, "
          "and the letter is the points rounded down to a whole letter (3.5 and 3 both read B; the points show the "
          "difference). The design score is the weighted mean of the graded categories' points, rounded the same way; "
          "accessibility is graded but is not part of the weighted design score. "
          + ("Design score (all weighted categories): " if browser else "Design score (partial: static evidence only, over the graded categories): ")
          + (score or "not graded")]
    for cat, (letter, pts, note) in grades.items():
        L.append(f"- {cat}: not graded ({note})" if letter is None else f"- {cat}: {letter}, {pts:g}/4" + (f" ({note})" if note else ""))
    L += [""]
    if a["quick_wins"]:
        titles = {f["id"]: f["title"] for f in a["findings"]}
        L += ["## Quick wins"] + [f"- `{q}` {md_escape(titles[q])}" for q in a["quick_wins"]] + [""]
    L += ["## Unknowns"] + evidence_gap_lines(a, ctx) + [""]
    L += ["## Repair handoff (draft, carries no approval)", f"Target: {a['handoff']['target']}. {md_escape(a['handoff']['notes'])}",
          "This review changed no source: repairs are a separate card on the existing engineering path, and a re-review "
          "needs new artifacts.", ""]
    files = {"design-review-report.md": "\n".join(L)}
    sc = {"schema": "design-static-checks.v1", "artifacts": {}}
    for aid, res in sorted(checks.items()):
        art = ctx.store.get(aid)
        sc["artifacts"][aid] = {"digest": art["content_digest"], "location": art["location"], "checks": res}
    files["static-checks.json"] = json.dumps(sc, indent=2, ensure_ascii=False) + "\n"
    return files


def _review_limits(a: dict, ctx: "Context") -> List[str]:
    supplied, _ = _capture_errors(a, ctx)
    out = []
    if not supplied.get("browser_render"):
        out.append("No browser render or screenshot: static checks of markup, CSS and tokens only; visual hierarchy, rendered contrast, "
                   "target size and reflow are not verified.")
    if not supplied.get("assistive_tech"):
        out.append("No assistive-technology run: screen-reader output and keyboard order are not verified.")
    if not supplied.get("usability_test"):
        out.append("No usability session: no claim about how people use the design.")
    return out


# ---------------------------------------------------------------- competitor research rubric (adapted PM Skills method)

COMPETITOR_SOURCE_KINDS = {"primary-web", "synthetic-fixture"}


def _research_sources(a: dict, ctx: "Context") -> Dict[str, dict]:
    """source_id -> {item, space (normalized quotable text, one captured line per line), hits (instruction-like text)}
    for every pack item with a verified captured file. Cached per context."""
    cache = ctx.extra.setdefault("research_sources", {}) if ctx.extra is not None else {}
    if not cache and ctx.pack is not None:
        for sid, it in ctx.pack.items.items():
            text = ctx.pack.captures.get(sid)
            if text is None:
                continue
            derived = "snapshot_digest" in it
            cache[sid] = {"item": it, "space": cr_research.quote_space(text, derived), "derived": derived,
                          "raw": text, "hits": cr_research.injection_hits(text)}
    return cache


def _quote_errors(where: str, quotes: Sequence[dict], srcs: Dict[str, dict], allowed: Optional[Iterable[str]] = None
                  ) -> Tuple[List[int], List[str]]:
    """Positions of each quote in its source's quotable text, and errors for unknown, uncaptured, foreign,
    non-verbatim or instruction-like quotes."""
    errs: List[str] = []
    pos: List[int] = []
    allowed = set(allowed) if allowed is not None else None
    prev: Dict[str, int] = {}
    for q in quotes:
        sid = q["source_id"]
        if allowed is not None and sid not in allowed:
            errs.append(f"{where}: quote cites {sid}, which is not one of this competitor's sources")
            continue
        src = srcs.get(sid)
        if src is None:
            errs.append(f"{where}: {sid} has no captured file: a quote cannot be verified")
            continue
        # Fragments are read in order: the nearest occurrence after the previous fragment of the same source.
        p = cr_research.find(src["space"], q["text"], prev.get(sid, 0))
        if p < 0:
            p = cr_research.find(src["space"], q["text"])
        if p < 0:
            errs.append(f"{where}: quote is not in {sid} (verbatim captured text only): {q['text'][:80]!r}")
            continue
        hit = cr_research.overlaps_injection(q["text"], src["space"], src["hits"])
        if hit:
            errs.append(f"{where}: quote contains instruction-like source text ({hit!r}); source instructions are data, never evidence")
            continue
        pos.append(p)
        prev[sid] = p
    return pos, errs


def _research_freshness(a: dict, ctx: "Context", cited: Iterable[str]) -> Tuple[Optional[str], List[str]]:
    errs: List[str] = []
    oldest: Optional[str] = None
    items = ctx.pack.items
    for sid in sorted(set(cited)):
        it = items.get(sid)
        if it is None:
            continue
        ts = it.get("retrieved_at")
        if ts is None:
            errs.append(f"{sid} has no retrieved_at: a dated comparison needs the retrieval time of every cited page")
            continue
        age = cr_research.age_days(ts, a["as_of"])
        if age < 0:
            errs.append(f"{sid} was retrieved after as_of {a['as_of']} ({ts})")
        elif age > a["max_source_age_days"]:
            errs.append(f"{sid} was retrieved {age} days before as_of {a['as_of']} (over {a['max_source_age_days']}): stale, re-retrieve it")
        if oldest is None or cr_research.retrieved_date(ts) < cr_research.retrieved_date(oldest):
            oldest = ts
    by = cr_research.reverify_by(a["as_of"], a["max_source_age_days"], oldest)
    today = (ctx.extra or {}).get("today")
    if today is not None and today > by:
        errs.append(f"today {today} is past the re-verify date {by}: the cited prices are stale, re-retrieve the pages")
    return oldest, errs


def _other_plans(a: dict, p: dict) -> List[str]:
    return sorted({x["plan"] for x in a["price_points"] if x["competitor_id"] == p["competitor_id"] and x["plan"] != p["plan"]})


def research_cited_sources(a: dict) -> List[str]:
    out = [q["source_id"] for c in a["competitors"] for q in c["positioning"]]
    out += [q["source_id"] for p in a["price_points"] for q in p["quotes"]]
    out += [q["source_id"] for f in a["features"] for q in f["quotes"]]
    out += [sid for s in a["statements"] if s["class"] in ("observed", "synthetic") for sid in s["source_ids"]]
    return list(dict.fromkeys(out))


def validate_competitor_research(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    if pack is None:
        return ["competitor research needs an evidence pack of captured, dated sources (--evidence)"]
    errs: List[str] = []
    comps, e = check_computations(a["computations"], pack)
    errs += e
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_questions(a["questions"], pack) + check_next_action(a["next_action"])
    if a["as_of"] != pack.doc["as_of"]:
        errs.append(f"as_of {a['as_of']} differs from the evidence pack as_of {pack.doc['as_of']}")
    policy = pack.doc["policy"]
    if policy["public_research"] == "denied" and a["queries"]:
        errs.append(f"public research is denied by the pack policy but the analysis lists {len(a['queries'])} public queries")
    for term, text in cr_research.private_term_hits(a["queries"], policy["private_terms"]):
        errs.append(f"query {text!r} contains the private term {term!r}; private names never enter public search")
    for term, text in cr_research.private_term_hits([it["ref"] for it in pack.items.values()], policy["private_terms"]):
        errs.append(f"source ref {text!r} contains the private term {term!r}")
    srcs = _research_sources(a, ctx)
    cited = research_cited_sources(a)
    for sid in cited:
        it = pack.items.get(sid)
        if it is None:
            errs.append(f"{sid} is not in the evidence pack")
            continue
        if it["kind"] not in COMPETITOR_SOURCE_KINDS:
            errs.append(f"{sid} is a {it['kind']}; competitor facts need the vendor's captured page (primary-web) or a synthetic fixture")
        if sid in srcs:
            errs += cr_research.excerpt_errors(it, srcs[sid]["space"]) + cr_research.values_in_excerpt(it)
    oldest, e = _research_freshness(a, ctx, cited)
    errs += e
    gaps = pack.unavailable_topics()

    comp_ids: Dict[str, dict] = {}
    for c in a["competitors"]:
        if c["id"] in comp_ids:
            errs.append(f"competitor {c['id']}: duplicate id")
        comp_ids[c["id"]] = c
        for sid in c["source_ids"]:
            it = pack.items.get(sid)
            if it is None:
                errs.append(f"competitor {c['id']} cites unknown source {sid}")
            elif not cr_research.on_domain(it["ref"], c["domain"]):
                errs.append(f"{c['id']} cites {sid} ({cr_research.host_of(it['ref']) or it['ref']}), which is not on {c['domain']}: "
                            "not the competitor's own (primary) page")
        _, e = _quote_errors(f"{c['id']} positioning", c["positioning"], srcs, c["source_ids"])
        errs += e

    def own(where: str, cid: str, quotes: Sequence[dict]) -> Optional[dict]:
        c = comp_ids.get(cid)
        if c is None:
            errs.append(f"{where} names unknown competitor {cid}")
            return None
        for q in quotes:
            if q["source_id"] not in c["source_ids"]:
                errs.append(f"{where} cites {q['source_id']}, which is not a source of competitor {cid}")
        return c

    prices: Dict[str, dict] = {}
    for p in a["price_points"]:
        pid = p["id"]
        if pid in prices:
            errs.append(f"price {pid}: duplicate id")
        prices[pid] = p
        c = own(pid, p["competitor_id"], p["quotes"])
        if c is None:
            continue
        gap = gaps.get(f"pricing-{p['competitor_id']}")
        if gap is not None and p["status"] != "unknown":
            errs.append(f"{pid}: pricing-{p['competitor_id']} is not available ({gap['reason']}); the price stays unknown")
        pos, e = _quote_errors(pid, [q for q in p["quotes"] if q["source_id"] in c["source_ids"]], srcs)
        errs += e
        if e:
            continue
        text = " ".join(q["text"] for q in p["quotes"])
        errs += cr_research.price_wording_errors(p, text)
        if p["quotes"] and pos:
            space = srcs[p["quotes"][0]["source_id"]]["space"]
            if len({q["source_id"] for q in p["quotes"]}) > 1:
                errs.append(f"{pid}: quotes for one price come from one page")
            elif not cr_research.span_ok(pos, [q["text"] for q in p["quotes"]]):
                errs.append(f"{pid}: quoted fragments are more than {cr_research.QUOTE_SPAN} characters apart; they may describe different plans")
            elif not cr_research.plan_near(space, p["plan"], min(pos), text, _other_plans(a, p)):
                errs.append(f"{pid}: plan {p['plan']!r} is not next to the quoted price in {p['quotes'][0]['source_id']}")
    if "pricing" in a["lens"]:
        for cid in comp_ids:
            if not any(p["competitor_id"] == cid for p in a["price_points"]):
                errs.append(f"pricing lens: competitor {cid} has no price row (record an unknown price when none is public)")
    feats: Dict[str, dict] = {}
    for f in a["features"]:
        if f["id"] in feats:
            errs.append(f"feature {f['id']}: duplicate id")
        feats[f["id"]] = f
        c = own(f["id"], f["competitor_id"], f["quotes"])
        if c is not None:
            errs += _quote_errors(f["id"], [q for q in f["quotes"] if q["source_id"] in c["source_ids"]], srcs)[1]

    for s in a["statements"]:
        if s["class"] in ("assumption", "hypothesis", "estimate", "synthetic") and cr_research.MARKET_CLAIM.search(s["text"]):
            errs.append(f"statement {s['id']}: market size/share/leadership claim ({cr_research.MARKET_CLAIM.search(s['text']).group(0)!r}) "
                        "needs an observed statement citing a source; otherwise record it as an unknown")
    if not any(s["class"] in ("assumption", "hypothesis") for s in a["statements"]):
        errs.append("record the market assumptions the comparison rests on (at least one assumption or hypothesis statement)")
    for sid in a["subject"]["statement_ids"]:
        if sid not in stmts:
            errs.append(f"subject cites unknown statement {sid}")

    def allowed_numbers(price_ids: Iterable[str], feature_ids: Iterable[str], st_ids: Iterable[str], comp_ids_: Iterable[str]) -> List[float]:
        out: List[float] = []
        for pid in price_ids:
            if pid in prices:
                p = prices[pid]
                out += ([float(p["amount"])] if p["amount"] is not None else []) + [n for q in p["quotes"] for n in cr_research.numbers(q["text"])]
        for fid in feature_ids:
            if fid in feats:
                out += [n for q in feats[fid]["quotes"] for n in cr_research.numbers(q["text"])]
        for sid in st_ids:
            if sid in stmts:
                out += cr_research.numbers(stmts[sid]["text"])
        for cid in comp_ids_:
            if cid in comps:
                out.append(float(comps[cid]["result"]))
        return out

    def claim_errors(where: str, text: str) -> List[str]:
        out = []
        hit = cd.research_claim(text) or re.search(r"\b(customers?|users?|buyers?|reviewers?)\s+(complain(?:ed|s)?|love|hate|prefer(?:red)?|say|said)\b",
                                                   text, re.I)
        if hit:
            out.append(f"{where} reads as customer research ({(hit if isinstance(hit, str) else hit.group(0))!r}) but no research was supplied")
        m = cr_research.MARKET_CLAIM.search(text)
        if m:
            out.append(f"{where}: market size/share/leadership claim ({m.group(0)!r}) is not supported by a cited source")
        return out

    findings: Dict[str, dict] = {}
    for f in a["findings"]:
        fid = f["id"]
        if fid in findings:
            errs.append(f"finding {fid}: duplicate id")
        findings[fid] = f
        for pid in f["price_ids"]:
            if pid not in prices:
                errs.append(f"{fid} cites unknown price {pid}")
        for x in f["feature_ids"]:
            if x not in feats:
                errs.append(f"{fid} cites unknown feature {x}")
        for x in f["computation_ids"]:
            if x not in comps:
                errs.append(f"{fid} cites unknown computation {x}")
        errs += check_refs(fid, f["statement_ids"], stmts)
        evidence = [pid for pid in f["price_ids"] if pid in prices and prices[pid]["status"] != "unknown"] + \
                   [x for x in f["feature_ids"] if x in feats] + \
                   [x for x in f["statement_ids"] if x in stmts and stmts[x]["class"] in ("observed", "synthetic")]
        if not evidence:
            errs.append(f"{fid} cites no captured evidence (a stated price, a quoted feature or an observed/synthetic statement)")
        nums = allowed_numbers(f["price_ids"], f["feature_ids"], f["statement_ids"], f["computation_ids"])
        for n in cr_research.numbers(f["text"]):
            if not any(cr_research.same(n, x) for x in nums):
                errs.append(f"{fid}: number {n:g} is not in its cited prices, quotes, statements or computations")
        errs += claim_errors(fid, f["text"])
    kinds = {f["kind"] for f in a["findings"]}
    if not kinds & {"opportunity", "differentiator"}:
        errs.append("findings need at least one opportunity or differentiator")
    if "risk" not in kinds:
        errs.append("findings need at least one risk")
    for r in a["recommendations"]:
        rid = r["id"]
        for x in r["finding_ids"]:
            if x not in findings:
                errs.append(f"{rid} cites unknown finding {x}")
        for x in r["assumption_ids"]:
            if x not in stmts:
                errs.append(f"{rid} cites unknown statement {x}")
            elif stmts[x]["class"] not in ("assumption", "hypothesis"):
                errs.append(f"{rid}: {x} is not an assumption or hypothesis (list the judgment calls the recommendation depends on)")
        fs = [findings[x] for x in r["finding_ids"] if x in findings]
        nums = allowed_numbers([p for f in fs for p in f["price_ids"]], [x for f in fs for x in f["feature_ids"]],
                               [x for f in fs for x in f["statement_ids"]] + r["assumption_ids"], [x for f in fs for x in f["computation_ids"]])
        for n in cr_research.numbers(r["text"]):
            if not any(cr_research.same(n, x) for x in nums):
                errs.append(f"{rid}: number {n:g} is not in the prices, quotes, statements or computations of its findings")
        errs += claim_errors(rid, r["text"])

    snaps = (ctx.extra or {}).get("snapshots")
    if snaps is not None:
        errs += _snapshot_errors(a, ctx, srcs, snaps)
    return errs


def _snapshot_errors(a: dict, ctx: "Context", srcs: Dict[str, dict], snaps: Dict[str, str]) -> List[str]:
    """With --snapshot-dir: every derived excerpt's quoted lines, and every price point's plan/fragment
    placement, are re-checked against the full retrieved text (matched by its SHA-256)."""
    errs: List[str] = []
    checked = []
    for sid, src in sorted(srcs.items()):
        it = src["item"]
        if not src["derived"]:
            continue
        full = snaps.get(it["snapshot_digest"])
        if full is None:
            errs.append(f"{sid}: no file in the snapshot directory has sha256 {it['snapshot_digest'][:16]}…")
            continue
        errs += cr_research.snapshot_errors(sid, src["raw"], full)
        space = cr_research.quote_space(full, False)
        for p in a["price_points"]:
            if not p["quotes"] or p["quotes"][0]["source_id"] != sid:
                continue
            pos = cr_research.find_in_order(space, [q["text"] for q in p["quotes"]])
            if min(pos) < 0:
                continue  # already reported as a quoted line missing from the snapshot
            text = " ".join(q["text"] for q in p["quotes"])
            if not cr_research.span_ok(pos, [q["text"] for q in p["quotes"]]):
                errs.append(f"{p['id']}: in the full snapshot of {sid} the quoted fragments are more than {cr_research.QUOTE_SPAN} characters apart")
            elif not cr_research.plan_near(space, p["plan"], min(pos), text, _other_plans(a, p)):
                errs.append(f"{p['id']}: in the full snapshot of {sid} plan {p['plan']!r} is not next to the quoted price")
        checked.append(sid)
    ctx.extra["snapshot_checked"] = checked
    return errs


def load_snapshots(directory: Path, max_files: int = 50) -> Dict[str, str]:
    """sha256 -> text for the regular files directly inside ``directory`` (no symlinks, bounded size and count)."""
    directory = Path(os.path.abspath(directory))
    _check_no_symlink_components(directory)
    out: Dict[str, str] = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        raise WorkflowError(f"{directory}: cannot list the snapshot directory ({exc.strerror or type(exc).__name__})") from None
    if len(names) > max_files:
        raise WorkflowError(f"{directory}: more than {max_files} files; pass a directory holding only the retrieved snapshots")
    for name in names:
        if os.path.islink(directory / name) or not (directory / name).is_file():
            continue
        data = cr.safe_repo_file(directory, name, max_bytes=MAX_CAPTURE_BYTES)
        out[cr.sha256_bytes(data)] = data.decode("utf-8", "replace")
    return out


def _price_cell(p: dict) -> str:
    if p["status"] == "unknown":
        return "**unknown**"
    if p["status"] == "contact-sales":
        return "contact sales (no public price)"
    return cr_research.money(p["amount"], p["currency"])


def _quote_cell(quotes: Sequence[dict], items: Dict[str, dict]) -> str:
    parts = [f"\"{md_escape(q['text'])}\"" for q in quotes]
    sids = list(dict.fromkeys(q["source_id"] for q in quotes))
    dates = [f"{sid}, retrieved {items[sid]['retrieved_at'][:10]}" for sid in sids if "retrieved_at" in items.get(sid, {})]
    return " … ".join(parts) + (f" ({'; '.join(dates)})" if dates else "")


def render_competitor_research(a: dict, ctx: "Context") -> Dict[str, str]:
    items = ctx.pack.items
    st = {s["id"]: s for s in a["statements"]}
    srcs = _research_sources(a, ctx)
    cited = research_cited_sources(a)
    oldest, _ = _research_freshness(a, ctx, cited)
    by = cr_research.reverify_by(a["as_of"], a["max_source_age_days"], oldest)
    comps = {c["id"]: c for c in a["competitors"]}
    synthetic = a["binding"]["data_classification"] == "synthetic"
    vendor = "synthetic fixture page" if synthetic else "vendor's own page"
    L = header("Competitor research (scan)", a, ctx)
    L += ["## Scope", f"Question: {md_escape(a['research_question'])}", f"Purpose: {a['purpose']} · lens: {', '.join(a['lens'])}",
          f"Subject: {md_escape(a['subject']['name'])}" + (" (" + ", ".join(f"`{x}`" for x in a['subject']['statement_ids']) + ")"
                                                           if a['subject']['statement_ids'] else ""),
          f"Competitor selection: {md_escape(a['selection_basis'])}",
          f"As of {a['as_of']}; freshness window {a['max_source_age_days']} days from the oldest cited retrieval. Re-verify by {by}.", ""]
    L += ["## Sources", "| ID | Page | URL | Retrieved (UTC offset as recorded) | Capture sha256 | Full snapshot sha256 |", "|---|---|---|---|---|---|"]
    for sid in cited:
        it = items.get(sid)
        if it is None:
            continue
        L.append(f"| {sid} | {md_escape(it['title'])} | {md_escape(it['ref'])} | {it.get('retrieved_at', 'not recorded')} | "
                 f"`{it.get('digest', '')[:16]}…` | " + (f"`{it['snapshot_digest'][:16]}…` (excerpt cut from it; not shipped)" if "snapshot_digest" in it
                                                          else "capture is the full page") + " |")
    L += ["", "## Competitive landscape", "| Competitor | Relation | Domain | How the vendor describes itself (quoted) |", "|---|---|---|---|"]
    for c in a["competitors"]:
        pos = _quote_cell(c["positioning"], items) if c["positioning"] else "not captured"
        L.append(f"| {md_escape(c['name'])} | {c['relation']} | {c['domain']} | {pos} |")
    L += ["", f"## Pricing (as stated on each {vendor}; not a quote or invoice)",
          "| Competitor | Plan | Price | Unit | Billing cadence | Evidence | Caveats |", "|---|---|---|---|---|---|---|"]
    for p in a["price_points"]:
        cad = {"unstated": "**unstated**", "not-applicable": "n/a"}.get(p["cadence"], p["cadence"].replace("-", " "))
        unit = "n/a" if p["unit"] == "not-applicable" else p["unit"]
        ev = _quote_cell(p["quotes"], items) if p["quotes"] else f"none — resolve: {md_escape(p.get('resolution', ''))}"
        L.append(f"| {md_escape(comps[p['competitor_id']]['name'])} | {md_escape(p['plan'])} | {_price_cell(p)} | {unit} | {cad} | {ev} | "
                 + ("; ".join(md_escape(x) for x in p["caveats"]) or "—") + " |")
    if a["computations"]:
        L += ["", "## Computed comparisons (executed by the validator from cited values)"]
        for c in a["computations"]:
            ins = ", ".join(f"{k} = {v['source_id']}.{v['value']}" for k, v in c["inputs"].items())
            L.append(f"- `{c['id']}`: {c['expression']} with {ins} = {c['result']:g} {md_escape(c['unit'])}")
    L += ["", f"## Features (quoted from the {vendor}; not tested)", "| Competitor | Plan (as read by the coordinator) | Feature | Evidence |", "|---|---|---|---|"]
    for f in a["features"]:
        L.append(f"| {md_escape(comps[f['competitor_id']]['name'])} | {md_escape(f['plan'])} | {md_escape(f['feature'])} | {_quote_cell(f['quotes'], items)} |")
    L += ["", "## Observations"] + ([statement_line(s) for s in by_class(a["statements"], ("observed", "synthetic"))] or ["- none"])
    L += ["", "## Market assumptions (not verified)"] + ([statement_line(s) for s in by_class(a["statements"], ("assumption", "hypothesis", "estimate"))] or ["- none"])
    L += ["", "## Opportunities and risks (coordinator interpretation)"]
    for f in a["findings"]:
        refs = f["price_ids"] + f["feature_ids"] + f["statement_ids"] + f["computation_ids"]
        L.append(f"- **{f['kind']}** ({f['class']}) `{f['id']}`: {md_escape(f['text'])} [" + ", ".join(refs) + "]")
    L += ["", "## Recommendations", "Coordinator judgment resting on the findings and assumptions named; not a decision or an approval."]
    for r in a["recommendations"]:
        dep = (" — depends on " + ", ".join(f"`{x}`" for x in r["assumption_ids"])) if r["assumption_ids"] else ""
        L.append(f"- `{r['id']}` **{r['stance'].replace('-', ' ')}**: {md_escape(r['text'])} (from " + ", ".join(f"`{x}`" for x in r["finding_ids"]) + f"){dep}")
    L += ["", "## Unknowns"] + evidence_gap_lines(a, ctx)
    L += ["", "## Source-content safety"]
    flagged = [(sid, h) for sid in cited if sid in srcs for h in srcs[sid]["hits"]]
    L += [f"- {sid}: instruction-like text in the captured page, treated as data and not followed: \"{md_escape(h)}\"" for sid, h in flagged] \
        or ["- none found in the cited captures (pattern check, not a guarantee)"]
    checked = (ctx.extra or {}).get("snapshot_checked")
    derived = [sid for sid in cited if srcs.get(sid, {}).get("derived")]
    L += ["", "## Limits",
          f"- Prices and features are what each {vendor} said when captured; nothing was purchased, trialled or tested.",
          "- Quotes were matched against the captured files by this validator; it fetched nothing and cannot tell whether a vendor statement is true.",
          "- The comparison does not show market demand, market share or a revenue opportunity.",
          "- Plan names next to prices are checked by position in the captured text; feature-to-plan placement is the coordinator's reading."]
    if derived:
        L.append("- Derived excerpts " + ", ".join(derived) + (
            f": quoted lines re-checked against the full retrieved snapshots in this run ({', '.join(checked)})." if checked
            else ": quoted lines were NOT re-checked against the full snapshots in this run (excerpt digests only)."))
    L.append("")
    report = "\n".join(L)

    def src_entry(sid: str) -> dict:
        it = items.get(sid, {})
        e = {"source_id": sid, "ref": it.get("ref"), "retrieved_at": it.get("retrieved_at"), "capture_digest": it.get("digest")}
        if "snapshot_digest" in it:
            e["snapshot_digest"] = it["snapshot_digest"]
        return e

    def quoted(quotes: Sequence[dict]) -> List[dict]:
        return [{"source_id": q["source_id"], "text": q["text"]} for q in quotes]

    fact = "synthetic-fixture" if synthetic else "vendor-statement"
    entries = []
    for c in a["competitors"]:
        entries += [{"id": f"{c['id']}.positioning", "type": "positioning", "classification": fact, "quotes": quoted(c["positioning"]),
                     "sources": [src_entry(s) for s in dict.fromkeys(q["source_id"] for q in c["positioning"])]}] if c["positioning"] else []
    for p in a["price_points"]:
        e = {"id": p["id"], "type": "price", "classification": "unknown" if p["status"] == "unknown" else fact, "competitor": p["competitor_id"],
             "plan": p["plan"], "status": p["status"], "amount": p["amount"], "currency": p["currency"], "unit": p["unit"],
             "cadence": p["cadence"], "caveats": p["caveats"], "quotes": quoted(p["quotes"]),
             "sources": [src_entry(s) for s in dict.fromkeys(q["source_id"] for q in p["quotes"])]}
        if "resolution" in p:
            e["resolution"] = p["resolution"]
        entries.append(e)
    for f in a["features"]:
        entries.append({"id": f["id"], "type": "feature", "classification": fact, "competitor": f["competitor_id"], "plan": f["plan"],
                        "feature": f["feature"], "quotes": quoted(f["quotes"]),
                        "sources": [src_entry(s) for s in dict.fromkeys(q["source_id"] for q in f["quotes"])]})
    for s in a["statements"]:
        entries.append({"id": s["id"], "type": "statement", "classification": s["class"], "text": s["text"],
                        "sources": [src_entry(x) for x in s["source_ids"]], "computations": s.get("computation_ids", [])})
    for f in a["findings"]:
        entries.append({"id": f["id"], "type": f["kind"], "classification": f["class"], "text": f["text"],
                        "rests_on": f["price_ids"] + f["feature_ids"] + f["statement_ids"] + f["computation_ids"]})
    for r in a["recommendations"]:
        entries.append({"id": r["id"], "type": "recommendation", "classification": "recommendation", "stance": r["stance"], "text": r["text"],
                        "rests_on": r["finding_ids"], "assumptions": r["assumption_ids"]})
    ledger = {"schema": "competitor-evidence-ledger.v1", "as_of": a["as_of"], "reverify_by": by,
              "data_classification": a["binding"]["data_classification"], "snapshot_rechecked": checked or [], "entries": entries}
    return {"competitor-report.md": report, "evidence-ledger.json": json.dumps(ledger, indent=2, ensure_ascii=False) + "\n"}


def _research_limits(a: dict, ctx: "Context") -> List[str]:
    cited = research_cited_sources(a)
    oldest, _ = _research_freshness(a, ctx, cited)
    out = [f"Vendor statements as captured (as of {a['as_of']}); not independent product testing. Re-verify by "
           f"{cr_research.reverify_by(a['as_of'], a['max_source_age_days'], oldest)}.",
           "Does not show market demand, market share or revenue opportunity."]
    srcs = _research_sources(a, ctx)
    derived = [sid for sid in cited if srcs.get(sid, {}).get("derived")]
    if derived and not (ctx.extra or {}).get("snapshot_checked"):
        out.append("Derived excerpts were not re-checked against the full retrieved snapshots in this run.")
    return out


# ---------------------------------------------------------------- gstack method adapters (C3): shared rubric
#
# office-hours, spec, plan-eng-review, plan-design-review, review and qa share one contract shape:
# verbatim excerpts of reused/reviewed artifacts, decisions that only a recorded requester answer
# settles, calibrated findings that approve no remedy, and a handoff that carries no approval.

ANSWER_SOURCE_KINDS = {"interview-notes", "document", "synthetic-fixture"}


def _norm(text: str) -> str:
    return " ".join(text.split())


def artifact_texts(ctx: "Context", refs: Sequence[dict]) -> Dict[str, str]:
    """artifact_id -> text for each referenced artifact the store holds (re-read by digest)."""
    out: Dict[str, str] = {}
    if ctx.store is None:
        return out
    for r in refs:
        if ctx.store.get(r["artifact_id"]) is not None:
            try:
                out[r["artifact_id"]] = ctx.store.read_text(r["artifact_id"])
            except (cr.RegistryError, UnicodeDecodeError):
                continue
    return out


def check_excerpt(where: str, ex: dict, texts: Dict[str, str], what: str) -> List[str]:
    if ex["artifact_id"] not in texts:
        return [f"{where}: {ex['artifact_id']} is not a {what} artifact"]
    if _norm(ex["text"]) not in _norm(texts[ex["artifact_id"]]):
        return [f"{where}: quoted text is not in {ex['artifact_id']}"]
    return []


def answer_source_errors(where: str, source_id: str, a: dict, pack: Optional[Pack]) -> List[str]:
    """A recorded requester answer is a supplied item of an answer kind whose class matches the task
    (a synthetic fixture never answers for a real/public task). It is recorded, never an approval."""
    it = pack.items.get(source_id) if pack else None
    if it is None:
        return [f"{where}: answer source {source_id} is not in the evidence pack"]
    if it["kind"] not in ANSWER_SOURCE_KINDS:
        return [f"{where}: {source_id} is a {it['kind']}, not a recorded requester answer"]
    if (it["classification"] == "synthetic") != (a["binding"]["data_classification"] == "synthetic"):
        return [f"{where}: {source_id} is {it['classification']} and cannot answer for a {a['binding']['data_classification']} task"]
    return []


def answer_lines(excerpt: str) -> List[str]:
    """The excerpt's lines, stripped. A recorded answer is a whole line, so a mention inside prose
    ('not approach: x', 'we rejected approach: x') never counts."""
    return [ln.strip() for ln in excerpt.splitlines()]


def recorded_answer(where: str, source_id: str, key: str, value: str, a: dict, pack: Optional[Pack]) -> List[str]:
    """A requester answer counts only as a supplied evidence item (of the task's class) with a line
    that is exactly '<key>: <value>' (case-sensitive). It is the reported answer, not an authorization."""
    errs = answer_source_errors(where, source_id, a, pack)
    if errs:
        return errs
    if f"{key}: {value}" not in answer_lines(pack.items[source_id]["excerpt"]):
        return [f"{where}: {source_id} does not record the line '{key}: {value}'"]
    return []


def recorded_ids(where: str, source_id: str, key: str, want: List[str], verb: str, a: dict,
                 pack: Optional[Pack]) -> List[str]:
    """An answered checkpoint names the exact ids it acts on: one line '<key>: id, id, ...' whose set
    equals ``want`` (what the analysis then carries or tests)."""
    errs = answer_source_errors(where, source_id, a, pack)
    if errs:
        return errs
    rows = [m.group(1) for ln in answer_lines(pack.items[source_id]["excerpt"])
            if (m := re.fullmatch(rf"{re.escape(key)}:\s*(\S.*)", ln))]
    if len(rows) != 1:
        return [f"{where}: {source_id} does not record a line '{key}: <ids>'"]
    got = sorted(x.strip() for x in rows[0].split(","))
    if got != sorted(want):
        return [f"{where}: {source_id} records {got}, but the analysis {verb} {sorted(want)}"]
    return []


def check_decisions(a: dict, pack: Optional[Pack]) -> Tuple[Dict[str, dict], List[str]]:
    errs: List[str] = []
    out: Dict[str, dict] = {}
    for d in a["decisions"]:
        did = d["id"]
        if did in out:
            errs.append(f"decision {did}: duplicate id")
        out[did] = d
        opts = [o["id"] for o in d["options"]]
        if len(opts) != len(set(opts)):
            errs.append(f"decision {did}: duplicate option")
        if d["recommendation"] not in opts:
            errs.append(f"decision {did}: recommendation {d['recommendation']} is not an option")
        ans = d.get("answer")
        if d["status"] == "pending":
            if ans is not None:
                errs.append(f"decision {did} is pending but carries an answer")
        elif ans is None:
            errs.append(f"decision {did} is answered but cites no recorded answer")
        elif ans["option_id"] not in opts:
            errs.append(f"decision {did}: answer {ans['option_id']} is not an option")
        else:
            errs += recorded_answer(f"decision {did}", ans["source_id"], did, ans["option_id"], a, pack)
    return out, errs


def pending_decisions(a: dict) -> List[str]:
    return [f"decision {d['id']} pending: {d['question']}" for d in a["decisions"] if d["status"] == "pending"]


def decision_line(d: dict) -> str:
    opts = "; ".join(f"`{o['id']}` {md_escape(o['label'])}" for o in d["options"])
    state = (f"answered: `{d['answer']['option_id']}` (recorded in {d['answer']['source_id']})" if d["status"] == "answered"
             else "pending: needs the requester")
    return (f"- `{d['id']}` {md_escape(d['question'])} Options: {opts}. Recommendation: `{d['recommendation']}` "
            f"because {md_escape(d['why'])} — {state}")


def handoff_lines(h: dict) -> List[str]:
    return ["## Handoff (draft, carries no approval)", "Targets: " + ", ".join(h["targets"]) + f". {md_escape(h['notes'])}",
            "Nothing here approves implementation, issue filing, publication, merge or release; each needs its own approval "
            "on the receiving path.", ""]


# ---------------------------------------------------------------- office hours (adapted gstack office-hours)

FORCING_ROUTES = {"pre-product": ("q1-demand", "q2-status-quo", "q3-specific-user"),
                  "has-users": ("q2-status-quo", "q4-narrowest-wedge", "q5-observation"),
                  "paying-customers": ("q4-narrowest-wedge", "q5-observation", "q6-future-fit"),
                  "engineering-infra": ("q2-status-quo", "q4-narrowest-wedge")}
INTEREST_RE = re.compile(r"\b(wait-?list|sign-?ups?|interested|excited|would (?:use|pay|love|buy)|survey(?:s|ed)?|"
                         r"likes? the idea|said (?:they|it) (?:want|like|need)s?)\b", re.I)
OPINION_CLASSES = {"assumption", "hypothesis", "unknown", "estimate"}


def validate_office_hours(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    comps, errs = check_computations(a["computations"], pack)
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_questions(a["questions"], pack) + check_next_action(a["next_action"])
    texts = artifact_texts(ctx, a["reused_artifacts"])
    stage = a["product_stage"]
    if a["mode"] == "builder":
        if a["forcing_questions"] or a["demand"] is not None or stage is not None:
            errs.append("builder mode does not run the forcing questions, demand diagnostic or product stage")
    else:
        if stage is None:
            errs.append("startup mode needs a product stage (it routes the forcing questions)")
        if a["demand"] is None:
            errs.append("startup mode needs the demand diagnostic (evidenced or not-evidenced)")
    routed = FORCING_ROUTES.get(stage, ())
    asked = {q["topic"] for q in a["questions"]}
    seen = set()
    for q in a["forcing_questions"]:
        name = q["question"]
        if name in seen:
            errs.append(f"{name}: addressed twice")
        seen.add(name)
        st = q["status"]
        if st == "answered":
            if not q.get("statement_ids"):
                errs.append(f"{name} is answered but cites no statement")
            errs += check_refs(name, q.get("statement_ids", []), stmts)
        elif st == "covered":
            if "covered_by" not in q:
                errs.append(f"{name} is covered but quotes no reused artifact")
            else:
                errs += check_excerpt(name, q["covered_by"], texts, "reused")
        else:
            if name not in routed:
                errs.append(f"{name} is not routed for stage {stage}; do not leave it open as a demand on the requester")
            elif q.get("topic") not in asked:
                errs.append(f"{name} is open but no question with topic {q.get('topic')} is asked")
    for name in routed:
        if name not in seen:
            errs.append(f"stage {stage} routes {name} but the analysis does not address it (answer, cover from an accepted artifact, or ask)")
    dm = a["demand"]
    if dm is not None:
        if dm["status"] == "evidenced" and not dm["evidence_statement_ids"]:
            errs.append("demand is evidenced but cites no behavior")
        if dm["status"] == "not-evidenced" and dm["evidence_statement_ids"]:
            errs.append("demand is not-evidenced but lists evidence")
        errs += check_refs("demand", dm["evidence_statement_ids"] + dm["opinion_statement_ids"], stmts)
        for sid in dm["evidence_statement_ids"]:
            s = stmts.get(sid)
            if s is None:
                continue
            if s["class"] not in ("observed", "synthetic"):
                errs.append(f"demand evidence {sid} is {s['class']}, not observed behavior")
            hit = INTEREST_RE.search(s["text"])
            if hit:
                errs.append(f"demand evidence {sid} reads as interest ('{hit.group(0)}'), not demand; list it as opinion")
        for sid in dm["opinion_statement_ids"]:
            if sid in stmts and stmts[sid]["class"] not in OPINION_CLASSES:
                errs.append(f"opinion {sid} is labeled {stmts[sid]['class']}; opinion is an assumption or hypothesis")
    for p in a["premises"]:
        if p["status"] == "proposed":
            continue
        if "source_id" not in p:
            errs.append(f"premise {p['id']} is {p['status']} but cites no recorded answer")
        else:
            errs += recorded_answer(f"premise {p['id']}", p["source_id"], p["id"], {"agreed": "agree", "disagreed": "disagree"}[p["status"]], a, pack)
    alts = {x["id"]: x for x in a["alternatives"]}
    if len(alts) != len(a["alternatives"]):
        errs.append("alternatives: duplicate id")
    for role in ("minimal-viable", "ideal-architecture"):
        if sum(1 for x in a["alternatives"] if x["role"] == role) != 1:
            errs.append(f"alternatives need exactly one {role} approach")
    decisions, e = check_decisions(a, pack)
    errs += e
    rec = a["recommendation"]
    d = decisions.get(rec["decision_id"])
    if rec["alternative_id"] not in alts:
        errs.append(f"recommendation {rec['alternative_id']} is not an alternative")
    if d is None:
        errs.append(f"recommendation cites unknown decision {rec['decision_id']}")
    else:
        if sorted(o["id"] for o in d["options"]) != sorted(alts):
            errs.append(f"decision {d['id']} must offer exactly the alternatives {sorted(alts)}")
        if d["recommendation"] != rec["alternative_id"]:
            errs.append(f"recommendation {rec['alternative_id']} differs from decision {d['id']}'s recommendation {d['recommendation']}")
    for sc in a["success_criteria"]:
        hit = VAGUE_WORDS.search(sc["metric"])
        if hit:
            errs.append(f"success criterion {sc['id']} is vague ('{hit.group(0)}'); name a measurable metric")
    return errs


def _office_needs(a: dict, ctx: "Context") -> List[str]:
    out = _approval_needs(a) + pending_decisions(a)
    return out + [f"premise {p['id']} proposed, needs the requester: {p['text']}" for p in a["premises"] if p["status"] == "proposed"]


def render_office_hours(a: dict, ctx: "Context") -> Dict[str, str]:
    st = {s["id"]: s for s in a["statements"]}
    L = header(f"Opportunity brief: {a['idea']}", a, ctx)
    L += ["## Idea", md_escape(a["idea"]), f"Mode: {a['mode']}" + (f" · stage {a['product_stage']}" if a["product_stage"] else "")
          + (" · internal project (wedge = smallest demo a sponsor greenlights)" if a["internal_project"] else ""), ""]
    if a["reused_artifacts"]:
        L += ["Built on (accepted, re-read by digest): " + ", ".join(f"`{r['artifact_id']}` ({r['kind']})" for r in a["reused_artifacts"]), ""]
    L += ["## Forcing questions"]
    if a["mode"] == "builder":
        L += ["Builder session: the startup diagnostic is not required and was not run.", ""]
    else:
        routed = FORCING_ROUTES[a["product_stage"]]
        L.append(f"Stage {a['product_stage']} routes: {', '.join(routed)}.")
        for q in a["forcing_questions"]:
            if q["status"] == "covered":
                c = q["covered_by"]
                L.append(f"- {q['question']}: covered by `{c['artifact_id']}`: \"{md_escape(c['text'])}\" (not asked again)")
            elif q["status"] == "answered":
                L.append(f"- {q['question']}: answered — " + "; ".join(f"`{x}` {md_escape(st[x]['text'])}" for x in q["statement_ids"]))
            else:
                L.append(f"- {q['question']}: open — asked as topic `{q['topic']}`")
        L.append("")
        dm = a["demand"]
        L += ["## Demand evidence", f"Status: **{dm['status']}**"]
        L += [statement_line(st[x]) for x in dm["evidence_statement_ids"]] or ["- no behavior evidence"]
        if dm["opinion_statement_ids"]:
            L += ["Opinion, not demand:"] + [statement_line(st[x]) for x in dm["opinion_statement_ids"]]
        L.append("")
    L += ["## Premises"]
    for p in a["premises"]:
        L.append(f"- `{p['id']}` {p['status']} ({p['source_id']}): {md_escape(p['text'])}" if p["status"] != "proposed"
                 else f"- `{p['id']}` proposed: needs the requester — {md_escape(p['text'])}")
    L += ["", "## Approaches considered"]
    for x in a["alternatives"]:
        L += [f"### `{x['id']}` {md_escape(x['name'])} ({x['role']}; effort {x['effort']}, risk {x['risk']})", md_escape(x["summary"]),
              "- Pros: " + "; ".join(md_escape(p) for p in x["pros"]), "- Cons: " + "; ".join(md_escape(c) for c in x["cons"]),
              "- Reuses: " + ("; ".join(md_escape(r) for r in x["reuses"]) or "nothing existing"), ""]
    rec = a["recommendation"]
    d = next(x for x in a["decisions"] if x["id"] == rec["decision_id"])
    L += ["## Recommended approach", f"`{rec['alternative_id']}` because {md_escape(rec['reason'])}",
          (f"Decision `{d['id']}`: requester answer recorded in {d['answer']['source_id']}: `{d['answer']['option_id']}` "
            "(pack excerpt; no trusted approval receipt)."
           if d["status"] == "answered" else f"Decision `{d['id']}`: not approved — the recommendation is advice until the requester answers."), ""]
    others = [x for x in a["decisions"] if x["id"] != d["id"]]
    if others:
        L += ["## Other decisions"] + [decision_line(x) for x in others] + [""]
    L += ["## Success criteria"] + [f"- `{s['id']}` {md_escape(s['metric'])}: {fmt_threshold(s['threshold'])} within {md_escape(s['window'])}"
                                    for s in a["success_criteria"]] + [""]
    L += ["## Open questions"] + ([f"- ({q['topic']}) {md_escape(q['text'])}" for q in a["questions"]] or ["- none"]) + [""]
    L += ["## Unknowns"] + evidence_gap_lines(a, ctx) + [""]
    na = a["next_action"]
    L += ["## The assignment", f"{md_escape(na['action'])} ({na['kind']}, {na['cost']}, owner {md_escape(na['owner_role'])})"
          + (f"; approvals needed: {', '.join(na['approvals_needed'])}" if na["approvals_needed"] else ""), ""]
    L += handoff_lines(a["handoff"])
    L += ["Not run: second opinion from another model, landscape web search, visual mockups, builder profile and learnings.", ""]
    return {"opportunity-brief.md": "\n".join(L)}


# ---------------------------------------------------------------- spec (adapted gstack spec)

SPEC_REQUIREMENT_KINDS = ("lean-prd", "feature-brief", "problem-brief", "acceptance-map")
VERIFIED_STATE_CLASSES = {"observed", "synthetic", "unknown"}


# An ID the renderers emit as an item, not merely mention: the leading backticked token of a list item
# ("- `r-route` text", "- [ ] `ac-x` Given ...") or of a table row ("| `ac-x` | ..."), followed by a space.
# Header lines (task/company/project/pack), statement lines ("- **class** `id`:") and computation lines
# ("- `c`: ...") never match; reused/reviewed artifact IDs listed as items are removed by structural_ids().
STRUCTURAL_ID_RE = re.compile(r"^(?:- (?:\[[ xX]\] )?|\| )`([A-Za-z0-9][A-Za-z0-9_.:-]{0,79})` ", re.M)


def structural_ids(text: str, ctx: "Context") -> List[str]:
    """Requirement-like item IDs rendered in an artifact (never its header, statement or artifact IDs)."""
    not_items = set()
    if ctx.store is not None:
        for art in ctx.store.index["artifacts"]:
            not_items |= {art["artifact_id"], art["company_id"], art["project_id"]}
    return [rid for rid in STRUCTURAL_ID_RE.findall(text) if rid not in not_items]


def requirement_ids(a: dict, ctx: "Context") -> Dict[str, str]:
    """requirement ID -> artifact ID, from the item IDs each reused requirements artifact renders."""
    refs = [r for r in a["reused_artifacts"] if r["kind"] in SPEC_REQUIREMENT_KINDS]
    out: Dict[str, str] = {}
    for aid, text in artifact_texts(ctx, refs).items():
        for rid in structural_ids(text, ctx):
            out.setdefault(rid, aid)
    return out


def validate_spec(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    comps, errs = check_computations(a["computations"], pack)
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_questions(a["questions"], pack) + check_next_action(a["next_action"])
    errs += [f"redaction gate: {p} holds a secret-shaped string; remove it (secrets are referenced by name only)"
             for p in cm.secret_paths({k: v for k, v in a.items() if k not in ("binding", "evidence", "reused_artifacts")})]
    errs += check_refs("why", a["why_statement_ids"], stmts) + check_refs("current state", a["current_state_statement_ids"], stmts)
    for sid in a["current_state_statement_ids"]:
        if sid in stmts and stmts[sid]["class"] not in VERIFIED_STATE_CLASSES:
            errs.append(f"current state {sid} is an {stmts[sid]['class']}; state verified facts or label it unknown")
    framing, rc = a["framing"], a["root_cause"]
    if framing == "bug":
        if rc is None:
            errs.append("a bug spec needs a root cause (symptom and cause statements)")
        else:
            errs += check_refs("root cause", rc["symptom_statement_ids"] + rc["cause_statement_ids"], stmts)
        if not any(x["regression"] for x in a["acceptance_criteria"]):
            errs.append("a bug spec needs a regression criterion that fails before the fix")
    elif rc is not None:
        errs.append("only a bug spec carries a root cause")
    if framing == "feature" and not a["scope"]["out"]:
        errs.append("a feature spec names what is out of scope")
    if framing in ("feature", "refactor") and not a["rollback"]:
        errs.append(f"a {framing} spec needs a rollback plan")
    reqs = requirement_ids(a, ctx)
    acs: Dict[str, dict] = {}
    for x in a["acceptance_criteria"]:
        aid = x["id"]
        if aid in acs:
            errs.append(f"acceptance criterion {aid}: duplicate id")
        acs[aid] = x
        if not x["traces_to"]:
            errs.append(f"{aid} traces to nothing; cite a requirement ID of the reused PRD/brief or a statement")
        for t in x["traces_to"]:
            if t not in reqs and t not in stmts:
                errs.append(f"{aid} traces to {t}, which is neither a requirement ID in a reused requirements artifact nor a statement")
        hit = VAGUE_WORDS.search(x["then"])
        if hit and not numbers_in(x["then"]):
            errs.append(f"{aid}: 'then' is vague ('{hit.group(0)}'); state an observable outcome")
    levels: Dict[str, set] = {}
    for t in a["testing_plan"]:
        for cid in t["covers"]:
            if cid not in acs:
                errs.append(f"testing plan covers unknown criterion {cid}")
            levels.setdefault(cid, set()).add(t["level"])
    for aid, x in acs.items():
        if aid not in levels:
            errs.append(f"{aid} is not covered by the testing plan")
        elif x["test_level"] not in levels[aid]:
            errs.append(f"{aid} is a {x['test_level']} criterion but no {x['test_level']} test in the plan covers it")
    paths = [f["path"] for f in a["files"]]
    if len(paths) != len(set(paths)):
        errs.append("files: duplicate path")
    deps = {d["id"]: d["depends_on"] for d in a["dependencies"]}
    for d in a["dependencies"]:
        errs += [f"{d['id']} depends on unknown {x}" for x in d["depends_on"] if x not in deps]
    cyc = cm.find_cycle(deps)
    if cyc:
        errs.append("dependency cycle: " + " -> ".join(cyc))
    if a["filing"]["publish_requested"] and "publication" not in a["next_action"]["approvals_needed"]:
        errs.append("publishing the issue needs the publication approval in the next action; the draft stays local until then")
    return errs


def _ac_line(x: dict) -> str:
    return (f"- [ ] `{x['id']}` Given {md_escape(x['given'])}, when {md_escape(x['when'])}, then {md_escape(x['then'])} "
            f"({x['test_level']}{', regression' if x['regression'] else ''})")


def render_spec(a: dict, ctx: "Context") -> Dict[str, str]:
    st = {s["id"]: s for s in a["statements"]}
    reqs = requirement_ids(a, ctx)
    why = [statement_line(st[x]) for x in a["why_statement_ids"]]
    state = [statement_line(st[x]) for x in a["current_state_statement_ids"]]
    tests = [f"- {t['level']}: {md_escape(t['what'])} — covers " + ", ".join(f"`{c}`" for c in t["covers"]) for t in a["testing_plan"]]
    eff = a["effort"]
    L = header(f"Spec: {a['title']}", a, ctx)
    L += ["## Framing", f"{a['framing']} · mode {a['mode']}"]
    if a["reused_artifacts"]:
        L.append("Built on (accepted, re-read by digest): " + ", ".join(f"`{r['artifact_id']}` ({r['kind']})" for r in a["reused_artifacts"]))
    L += ["", "## Why this matters"] + why + ["", "## Verified current state"] + state + [""]
    rc = a["root_cause"]
    if rc is not None:
        causes = [st[x] for x in rc["cause_statement_ids"]]
        confirmed = all(c["class"] in ("observed", "synthetic") for c in causes)
        L += [f"## Root cause ({'confirmed by cited evidence' if confirmed else 'unconfirmed: hypothesis'})", "Symptom:"]
        L += [statement_line(st[x]) for x in rc["symptom_statement_ids"]] + ["Cause:"] + [statement_line(c) for c in causes] + [""]
    L += ["## Scope", "In scope:"] + [f"- {md_escape(x)}" for x in a["scope"]["in"]]
    if a["scope"]["out"]:
        L += ["Out of scope (do not touch):"] + [f"- {md_escape(x['item'])}: {md_escape(x['reason'])}" for x in a["scope"]["out"]]
    L += ["", "## Proposed change", md_escape(a["proposed_change"]), "", "## Acceptance criteria"]
    L += [_ac_line(x) for x in a["acceptance_criteria"]] + ["", "## Traceability"]
    for x in a["acceptance_criteria"]:
        srcs = sorted({reqs[t] for t in x["traces_to"] if t in reqs})
        L.append(f"- `{x['id']}` → " + ", ".join(f"`{t}`" for t in x["traces_to"]) + (f" ({', '.join(srcs)})" if srcs else " (statements)"))
    L += ["", "## Testing plan"] + tests + [""]
    if a["files"]:
        L += ["## Files", "| Path | Change | Why |", "|---|---|---|"] + [f"| `{md_escape(f['path'])}` | {f['change']} | {md_escape(f['why'])} |" for f in a["files"]] + [""]
    if a["dependencies"]:
        L += ["## Dependencies"] + [f"- {d['id']}" + (f" ← {', '.join(d['depends_on'])}" if d["depends_on"] else " (independent)")
                                    + f": {md_escape(d['title'])}" for d in a["dependencies"]] + [""]
    L += ["## Rules for the implementing agent"] + [f"- {md_escape(r)}" for r in a["rules_for_implementer"]] + [""]
    L += ["## Rollback", md_escape(a["rollback"]) if a["rollback"] else "Not applicable to this framing.", "",
          "## Effort", f"{eff['size']}: {md_escape(eff['basis'])}", ""]
    L += ["## Open questions"] + ([f"- ({q['topic']}) {md_escape(q['text'])}" for q in a["questions"]] or ["- none"]) + [""]
    L += ["## Unknowns"] + evidence_gap_lines(a, ctx) + [""] + handoff_lines(a["handoff"])
    pub = ("Publication requested: it waits for the publication approval." if a["filing"]["publish_requested"]
           else "No publication requested.")
    I = [f"# {md_escape(a['title'])}", "", f"Local draft: nothing was filed. {pub} Redaction gate passed (no secret-shaped strings).", "",
         "## Context"] + why + ["", "## Current State"] + state + ["", "## Proposed Change", md_escape(a["proposed_change"]), "",
         "## Acceptance Criteria"] + [_ac_line(x) for x in a["acceptance_criteria"]] + ["", "## Testing Plan"] + tests + ["",
         "## Rules for the implementing agent"] + [f"- {md_escape(r)}" for r in a["rules_for_implementer"]] + ["",
         "## Rollback Plan", md_escape(a["rollback"]) if a["rollback"] else "Not applicable.", "",
         "## Effort Estimate", f"{eff['size']}: {md_escape(eff['basis'])}", ""]
    return {"spec.md": "\n".join(L), "issue-draft.md": "\n".join(I)}


# ---------------------------------------------------------------- reviews of current artifacts (shared by plan reviews, review, qa)

PLAN_KINDS = ("lean-prd", "spec", "feature-brief", "design-brief", "design-package", "acceptance-map", "implementation-handoff",
              "opportunity-brief", "eng-review")


def check_reviewed(a: dict, ctx: "Context", kinds: Sequence[str], what: str) -> Tuple[Dict[str, dict], List[str]]:
    """Reviewed artifacts are re-read by digest (any review status), current, same company/project and of a reviewable kind."""
    store = ctx.store
    if store is None:
        return {}, [f"a {what} review needs the artifact index of the reviewed artifacts (--artifacts)"]
    currency = cr._artifact_currency(store.index["artifacts"], {c["id"]: c for c in ctx.registry["capabilities"]})
    b = a["binding"]
    errs: List[str] = []
    out: Dict[str, dict] = {}
    for r in a["reviewed_artifacts"]:
        aid = r["artifact_id"]
        art = store.get(aid)
        if art is None:
            errs.append(f"reviewed artifact {aid} is not in the artifact index")
            continue
        e = scope_errors(art, b, f"reviewed artifact {aid}")
        if e:
            errs += e
            continue
        if art["content_digest"] != r["digest"] or art["kind"] != r["kind"]:
            errs.append(f"reviewed artifact {aid}: kind/digest differs from the index (stale reference)")
            continue
        if art["kind"] not in kinds:
            errs.append(f"reviewed artifact {aid} is a {art['kind']}, not a {what} ({', '.join(kinds)})")
        ok, why = currency[aid]
        if not ok:
            errs.append(f"reviewed artifact {aid} is stale: {why}")
        out[aid] = art
    return out, errs


def check_findings(a: dict, stmts: Dict[str, dict], texts: Dict[str, str], captures: Iterable[str],
                   decisions: Dict[str, dict], sections: Iterable[str], severities: Iterable[str]) -> List[str]:
    """Evidence-bound, calibrated findings (gstack confidence calibration) that approve no remedy."""
    errs: List[str] = []
    seen = set()
    caps, secs, sevs = set(captures), set(sections), set(severities)
    for f in a["findings"]:
        fid = f["id"]
        if fid in seen:
            errs.append(f"finding {fid}: duplicate id")
        seen.add(fid)
        if f["section"] not in secs:
            errs.append(f"{fid}: section {f['section']} is not one of {sorted(secs)}")
        if f["severity"] not in sevs:
            errs.append(f"{fid}: severity {f['severity']} is not one of {sorted(sevs)}")
        quoted = False
        for ev in f["evidence"]:
            if ev["kind"] == "excerpt":
                e = check_excerpt(fid, ev, texts, "reviewed")
                errs += e
                quoted = quoted or not e
            elif ev["kind"] == "capture":
                if ev["source_id"] in caps:
                    quoted = True
                else:
                    errs.append(f"{fid} cites capture {ev['source_id']}, which is not a supplied, verified capture")
            elif ev["statement_id"] not in stmts:
                errs.append(f"{fid} cites unknown statement {ev['statement_id']}")
        conf = f["confidence"]
        if not quoted and conf > 5:
            errs.append(f"{fid} is unverified (no verbatim quote or capture): confidence must be 5 or lower")
        if conf <= 2 and not (f["severity"] == "critical" and f.get("speculative")):
            errs.append(f"{fid}: confidence 1-2 is only for a speculative critical finding")
        if f.get("speculative") and conf > 4:
            errs.append(f"{fid} is speculative: confidence must be 4 or lower")
        did = f.get("decision_id")
        if did is not None and did not in decisions:
            errs.append(f"{fid} cites unknown decision {did}")
        if f["disposition"] != "pending":
            if did is None:
                errs.append(f"{fid} is {f['disposition']} without a decision; a finding approves no remedy")
            elif did in decisions and decisions[did]["status"] != "answered":
                errs.append(f"{fid} is {f['disposition']} but decision {did} is pending; a finding approves no remedy")
        for field in ("recommendation", "problem"):
            if _is_patch(f[field]):
                errs.append(f"{fid} {field} is a patch; a review recommends, the engineering path edits")
    return errs


def finding_line(f: dict) -> str:
    sev = f["severity"].upper()
    caveat = " — Medium confidence, verify this is actually an issue." if f["confidence"] in (5, 6) else ""
    spec = " (speculative)" if f.get("speculative") else ""
    loc = f" {f['location']['file']}:{f['location']['line']}" if "location" in f else ""
    ev = []
    for e in f["evidence"]:
        if e["kind"] == "excerpt":
            ev.append(f"`{e['artifact_id']}`: \"{md_escape(e['text'])}\"")
        elif e["kind"] == "capture":
            ev.append(f"capture {e['source_id']}")
        else:
            ev.append(f"statement `{e['statement_id']}`")
    disp = f["disposition"] + (f" ({f['decision_id']})" if f.get("decision_id") else "")
    return (f"- [{sev}] (confidence: {f['confidence']}/10){spec} `{f['id']}`{loc} — {md_escape(f['title'])}: {md_escape(f['problem'])}{caveat}\n"
            f"  - Recommendation: {md_escape(f['recommendation'])}\n  - Evidence: {'; '.join(ev)}\n  - Disposition: {disp}")


def _shown(f: dict) -> bool:
    return f["confidence"] >= 5 or bool(f.get("speculative"))


def findings_block(fs: Sequence[dict]) -> List[str]:
    shown = [f for f in fs if _shown(f)]
    return [finding_line(f) for f in shown] or ["No issues found."]


def suppressed_block(a: dict) -> List[str]:
    low = [f for f in a["findings"] if not _shown(f)]
    return (["## Suppressed findings (confidence 3-4, appendix only)"] + [finding_line(f) for f in low] + [""]) if low else []


def readiness_line(a: dict) -> str:
    acc = [f for f in a["findings"] if f["disposition"] == "accepted"]
    pend = [d["id"] for d in a["decisions"] if d["status"] == "pending"]
    if pend:
        return f"Approval readiness: PENDING ({', '.join(pend)})"
    return "Approval readiness: ANSWERS RECORDED, NOT RECEIPT-VERIFIED (" + (", ".join(f"{f['id']} via {f['decision_id']}" for f in acc) or "no accepted remedies") + ")"


def review_needs(a: dict, ctx: "Context") -> List[str]:
    out = pending_decisions(a) + [f"approval needed before the next action: {x}" for x in a["next_action"]["approvals_needed"]]
    return out + [f"finding {f['id']} has no decision yet" for f in a["findings"]
                  if f["disposition"] == "pending" and f["severity"] in ("critical", "high") and not f.get("decision_id")]


def _review_head(a: dict, ctx: "Context", title: str) -> List[str]:
    L = header(title, a, ctx) + ["## Reviewed artifacts (re-read by digest)"]
    for r in a["reviewed_artifacts"]:
        art = ctx.store.get(r["artifact_id"])
        L.append(f"- `{r['artifact_id']}` ({r['kind']}, `{art['location']}`, sha256 `{r['digest'][:16]}…`, {art['review_status']})")
    return L + ["", "Audit only: this review changed no artifact or source. Review agreement is not approval.", ""]


# ---------------------------------------------------------------- plan engineering review (adapted gstack plan-eng-review)

ENG_SECTIONS = (("architecture", "1. Architecture"), ("code-quality", "2. Code quality"), ("tests", "3. Tests"), ("performance", "4. Performance"))


def validate_plan_eng_review(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    comps, errs = check_computations(a["computations"], pack)
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_next_action(a["next_action"])
    reviewed, e = check_reviewed(a, ctx, PLAN_KINDS, "plan")
    errs += e
    if errs:
        return errs
    texts = artifact_texts(ctx, a["reviewed_artifacts"])
    decisions, e = check_decisions(a, pack)
    errs += e
    errs += check_findings(a, stmts, texts, [], decisions, [k for k, _ in ENG_SECTIONS] + ["scope"], ("critical", "informational"))
    sc = a["scope_challenge"]
    errs += check_excerpt("scope count basis", sc["count_basis"], texts, "reviewed")
    for x in sc["already_exists"]:
        errs += check_excerpt(f"already exists: {x['what']}", x["evidence"], texts, "reviewed")
    if sc["files_touched"] >= 8 or sc["new_services"] >= 2:
        if sc.get("structure_decision_id") not in decisions:
            errs.append(f"complexity gate tripped ({sc['files_touched']} files, {sc['new_services']} new services): record a structure "
                        "decision (original vs smaller arrangement) before the review sections")
    for key, _ in ENG_SECTIONS:
        fs = [f["id"] for f in a["findings"] if f["section"] == key and _shown(f)]  # suppressed findings sit in the appendix
        st = a["sections"][key]["status"]
        if st == "no-issues" and fs:
            errs.append(f"section {key} says no-issues but has findings {', '.join(fs)}")
        if st == "findings" and not fs:
            errs.append(f"section {key} says findings but has none")
    fms = {m["id"]: m for m in a["failure_modes"]}
    fids = {f["id"]: f for f in a["findings"]}
    ref_ids = {rid for t in texts.values() for rid in structural_ids(t, ctx)}
    covered = set()
    for cp in a["test_plan"]["critical_paths"]:
        for c in cp["covers"]:
            if c not in fms and c not in fids and c not in ref_ids:
                errs.append(f"{cp['id']} covers {c}, which is not a failure mode, finding or ID in the reviewed artifacts")
            covered.add(c)
    for f in a["findings"]:
        errs += [f"{f['id']} cites unknown failure mode {m}" for m in f.get("failure_mode_ids", []) if m not in fms]
    for mid, m in fms.items():
        acc = m.get("accepted_by_decision")
        if acc is not None and (acc not in decisions or decisions[acc]["status"] != "answered"):
            errs.append(f"failure mode {mid}: acceptance needs an answered decision ({acc})")
        accepted = acc in decisions and decisions[acc]["status"] == "answered"
        if m["test"] is None and mid not in covered and not accepted:
            errs.append(f"failure mode {mid} has no test, planned test or recorded acceptance; every risk needs one")
        if m["test"] is None and m["error_handling"] is None and m["user_sees"] == "silent" and not accepted:
            if not any(mid in f.get("failure_mode_ids", []) and f["severity"] == "critical" for f in a["findings"]):
                errs.append(f"critical gap {mid} (no test, no error handling, silent) needs a critical finding")
    return errs


def render_plan_eng_review(a: dict, ctx: "Context") -> Dict[str, str]:
    sc = a["scope_challenge"]
    gate = "tripped" if sc["files_touched"] >= 8 or sc["new_services"] >= 2 else "clear"
    L = _review_head(a, ctx, "Engineering plan review")
    L += ["## Scope challenge", f"{sc['files_touched']} files, {sc['new_services']} new services: complexity gate {gate} "
          f"(basis: `{sc['count_basis']['artifact_id']}`: \"{md_escape(sc['count_basis']['text'])}\")",
          f"Minimum change: {md_escape(sc['minimum_change'])}", "What already exists:"]
    L += [f"- {x['use']}: {md_escape(x['what'])} — `{x['evidence']['artifact_id']}`: \"{md_escape(x['evidence']['text'])}\""
          for x in sc["already_exists"]] or ["- nothing identified"]
    scope_f = [f for f in a["findings"] if f["section"] == "scope"]
    L += findings_block(scope_f) if scope_f else []
    L.append("")
    for key, title in ENG_SECTIONS:
        L += [f"## {title}", md_escape(a["sections"][key]["notes"])] + findings_block([f for f in a["findings"] if f["section"] == key]) + [""]
    L += ["## Failure modes"]
    for m in a["failure_modes"]:
        gap = m["test"] is None and m["error_handling"] is None and m["user_sees"] == "silent"
        L.append(f"- `{m['id']}` {md_escape(m['path'])}: {md_escape(m['failure'])} Test: {md_escape(m['test'] or 'none')}; "
                 f"handling: {md_escape(m['error_handling'] or 'none')}; user sees: {m['user_sees']}" + (" — CRITICAL GAP" if gap else ""))
    if not a["failure_modes"]:
        L.append("- none named")
    L += ["", "## NOT in scope"] + ([f"- {md_escape(x['item'])}: {md_escape(x['reason'])}" for x in a["not_in_scope"]] or ["- nothing deferred"])
    L += ["", "## Decisions"] + ([decision_line(d) for d in a["decisions"]] or ["- none"]) + ["", readiness_line(a), ""]
    L += suppressed_block(a)
    L += ["## Unknowns"] + evidence_gap_lines(a, ctx) + [""] + handoff_lines(a["handoff"])
    tp = a["test_plan"]
    T = header("Test plan (from the engineering plan review)", a, ctx)
    T += ["Consumed by gstack.qa as test input: what to test and where. Nothing here was run.", "", "## Critical Paths"]
    T += [f"- `{c['id']}` ({c['level']}) {md_escape(c['flow'])} — covers " + ", ".join(f"`{x}`" for x in c["covers"]) for c in tp["critical_paths"]]
    T += ["", "## Edge Cases"] + ([f"- {md_escape(x)}" for x in tp["edge_cases"]] or ["- none"])
    T += ["", "## Regression"] + ([f"- {md_escape(x['behavior'])} — {md_escape(x['test'])}" for x in tp["regression"]] or ["- none"])
    T += ["", "## Pending Decisions"] + ([f"- {d['id']}: {md_escape(d['question'])}" for d in a["decisions"] if d["status"] == "pending"] or ["- none"]) + [""]
    return {"eng-review.md": "\n".join(L), "test-plan.md": "\n".join(T)}


# ---------------------------------------------------------------- plan design review (adapted gstack plan-design-review)

DESIGN_PASSES = ("information-architecture", "interaction-states", "journey", "ai-slop", "design-system", "responsive-accessibility",
                 "unresolved-decisions")
STATE_COLUMNS = ("loading", "empty", "error", "success", "partial", "permission_denied")


def _missing_states(a: dict) -> List[str]:
    out = []
    for row in a["state_table"]:
        gaps = [c for c in STATE_COLUMNS if row[c] is None and (c != "permission_denied" or row["access_controlled"])]
        if gaps:
            out.append(f"{row['feature']}: {', '.join(gaps)}")
    return out


def validate_plan_design_review(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    comps, errs = check_computations(a["computations"], pack)
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_next_action(a["next_action"])
    reviewed, e = check_reviewed(a, ctx, PLAN_KINDS, "plan")
    errs += e
    if errs:
        return errs
    texts = artifact_texts(ctx, a["reviewed_artifacts"])
    decisions, e = check_decisions(a, pack)
    errs += e
    errs += check_findings(a, stmts, texts, [], decisions, DESIGN_PASSES, ("high", "medium", "low"))
    ds = a["design_system"]
    if ds["status"] == "present":
        art = reviewed.get(ds.get("artifact_id"))
        if art is None or art["kind"] not in ("design-package", "design-brief"):
            errs.append(f"design system {ds.get('artifact_id')} is not a reviewed design package or design brief")
    elif "artifact_id" in ds:
        errs.append("design system is absent but names an artifact")
    by_pass: Dict[str, List[str]] = {}
    for f in a["findings"]:
        by_pass.setdefault(f["section"], []).append(f["id"])
    for p in DESIGN_PASSES:
        r = a["passes"][p]
        if r["rating"] < 10 and "gap" not in r:
            errs.append(f"pass {p} rated {r['rating']}/10 names no gap (what would make it a 10)")
        if r["rating"] == 10 and by_pass.get(p):
            errs.append(f"pass {p} rated 10/10 but has findings {', '.join(by_pass[p])}")
    missing = _missing_states(a)
    if missing and (a["passes"]["interaction-states"]["rating"] == 10 or not by_pass.get("interaction-states")):
        errs.append(f"interaction states missing ({'; '.join(missing)}): the interaction-states pass needs a finding and cannot score 10")
    for j in a["journey"]:
        if j["plan_specifies"] is None:
            if a["passes"]["journey"]["rating"] == 10:
                errs.append(f"journey step '{j['step']}' is not specified by the plan; the journey pass cannot score 10")
        else:
            errs += check_excerpt(f"journey step '{j['step']}'", j["plan_specifies"], texts, "reviewed")
    for h in a["slop_hits"]:
        errs += check_excerpt(f"slop hit '{h['rule']}'", h["evidence"], texts, "reviewed")
        if not h["resolved"] and a["passes"]["ai-slop"]["rating"] >= 8:
            errs.append(f"unresolved AI-slop hit caps the ai-slop pass below 8 ('{h['rule']}')")
    if a["mockups"] == "requested" and "spend" not in a["next_action"]["approvals_needed"]:
        errs.append("mockup generation is paid: it needs the spend approval in the next action; nothing is generated here")
    return errs


def render_plan_design_review(a: dict, ctx: "Context") -> Dict[str, str]:
    ir, ds = a["initial_rating"], a["design_system"]
    L = _review_head(a, ctx, "Design plan review")
    L += ["## Step 0", f"Initial design completeness: {ir['score']}/10 — {md_escape(ir['reason'])}", f"A 10 for this plan: {md_escape(ir['ten_looks_like'])}",
          "Design system: " + (f"present: `{ds['artifact_id']}`" if ds["status"] == "present" else
                               "absent — recommend gstack.design-consultation before implementation"),
          "Mockups: not generated" + (" (requested: waits for the spend approval)" if a["mockups"] == "requested" else ""), ""]
    L += ["## Passes"]
    for p in DESIGN_PASSES:
        r = a["passes"][p]
        L.append(f"- {p}: {r['rating']}/10" + (f" — {md_escape(r['gap'])}" if "gap" in r else ""))
    L += [""]
    for p in DESIGN_PASSES:
        L += [f"### {p}"] + findings_block([f for f in a["findings"] if f["section"] == p]) + [""]
    L += ["## Interaction state coverage", "| Feature | " + " | ".join(STATE_COLUMNS) + " |", "|---" * (len(STATE_COLUMNS) + 1) + "|"]
    for row in a["state_table"]:
        cells = []
        for c in STATE_COLUMNS:
            if row[c] is not None:
                cells.append(md_escape(row[c]))
            elif c == "permission_denied" and not row["access_controlled"]:
                cells.append("n/a (not access-controlled)")
            else:
                cells.append("MISSING")
        L.append(f"| {md_escape(row['feature'])} | " + " | ".join(cells) + " |")
    L += ["", "## Journey", "| Step | User feels | Plan specifies |", "|---|---|---|"]
    for j in a["journey"]:
        ps = j["plan_specifies"]
        L.append(f"| {md_escape(j['step'])} | {md_escape(j['feels'])} | "
                 + (f"`{ps['artifact_id']}`: \"{md_escape(ps['text'])}\"" if ps else "NOT SPECIFIED") + " |")
    L += ["", "## AI slop"] + ([f"- {'resolved' if h['resolved'] else 'UNRESOLVED'}: {md_escape(h['rule'])} — `{h['evidence']['artifact_id']}`: "
                                f"\"{md_escape(h['evidence']['text'])}\"" for h in a["slop_hits"]] or ["- no hard-rejection hits"])
    L += ["", "## Unresolved decisions"] + ([decision_line(d) for d in a["decisions"]] or ["- none"]) + ["", readiness_line(a), ""]
    L += suppressed_block(a)
    L += ["## Not verified", "- No browser render, assistive-technology run or usability session: this reviews the plan text only.", ""]
    L += ["## Unknowns"] + evidence_gap_lines(a, ctx) + [""] + handoff_lines(a["handoff"])
    return {"design-plan-review.md": "\n".join(L)}


# ---------------------------------------------------------------- captured runs (shared by review and qa)

def command_captures(ctx: "Context") -> Dict[str, Dict[str, Any]]:
    """source_id -> parsed capture, for real artifact items with a digest-verified captured file tagged command-capture."""
    out: Dict[str, Dict[str, Any]] = {}
    if ctx.pack is None:
        return out
    for sid, it in ctx.pack.items.items():
        if (it["kind"] == "artifact" and it["classification"] == "real" and "command-capture" in it["topics"]
                and sid in ctx.pack.captures):
            parsed = cm.parse_capture(ctx.pack.captures[sid])
            if parsed is not None:
                parsed["digest"] = it["digest"]
                out[sid] = parsed
    return out


def browser_captures(ctx: "Context") -> List[str]:
    out = []
    if ctx.pack is None:
        return out
    for sid, it in ctx.pack.items.items():
        if it["kind"] == "artifact" and it["classification"] == "real" and "browser-capture" in it["topics"] and "captured_file" in it:
            try:
                head = cr.safe_repo_file(ctx.pack.root, it["captured_file"], max_bytes=MAX_CAPTURE_BYTES)[:8]
            except cr.RegistryError:
                continue
            if head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff"):
                out.append(sid)
    return out


# ---------------------------------------------------------------- code review (adapted gstack review checklist, audit half)

REVIEW_CATEGORIES = ("sql-data-safety", "race-conditions", "llm-trust-boundary", "shell-injection", "enum-completeness", "gate-integrity",
                     "async-sync", "field-names", "dead-code", "llm-prompts", "completeness-gaps", "time-windows", "timing-tests",
                     "type-coercion", "view-frontend", "distribution-ci", "repo-conventions")
FORCED_CRITICAL = {"data-loss", "trust-boundary", "uncapped-spend"}


def reviewed_diff(a: dict, ctx: "Context") -> Dict[str, Dict[str, Any]]:
    files: Dict[str, Dict[str, Any]] = {}
    for text in artifact_texts(ctx, a["reviewed_artifacts"]).values():
        files.update(cm.parse_diff(text))
    return files


def validate_code_review(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    comps, errs = check_computations(a["computations"], pack)
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_next_action(a["next_action"])
    _, e = check_reviewed(a, ctx, ("code-change-handoff",), "code change")
    errs += e
    if errs:
        return errs
    texts = artifact_texts(ctx, a["reviewed_artifacts"])
    files = reviewed_diff(a, ctx)
    if not files:
        return ["the reviewed change holds no unified diff (diff --git / --- / +++ / @@ hunks)"]
    decisions, e = check_decisions(a, pack)
    errs += e
    caps = command_captures(ctx)
    errs += check_findings(a, stmts, texts, caps, decisions, REVIEW_CATEGORIES, ("critical", "informational"))
    pr = cm.pr_type(files)
    if a["pr_type"] != pr:
        detail = "; ".join(f"{cm.path_type(p)}: {p}" for p in sorted(files))
        errs.append(f"PR type is {pr} ({detail}), not {a['pr_type']}")
    shown: Dict[str, List[str]] = {}
    for f in a["findings"]:
        if _shown(f):
            shown.setdefault(f["section"], []).append(f["id"])
    for k in REVIEW_CATEGORIES:
        st, fs = a["categories"][k]["status"], shown.get(k, [])
        if st == "not-applicable":
            if pr in ("application", "mixed"):
                errs.append(f"on a {pr} diff every category runs; {k} cannot be not-applicable")
            elif k in cm.ALWAYS_RUN_CATEGORIES:
                errs.append(f"{k} always runs, whatever the PR type")
        if st != "findings" and fs:
            errs.append(f"category {k} is {st} but has findings {', '.join(fs)}")
        if st == "findings" and not fs:
            errs.append(f"category {k} says findings but has none")
    for f in a["findings"]:
        fid = f["id"]
        if f.get("defect_class") in FORCED_CRITICAL and f["severity"] != "critical":
            errs.append(f"{fid} is {f['defect_class']}: CRITICAL on sight (forced-critical class, never downgraded)")
        loc = f.get("location")
        if loc is None:
            continue
        info = files.get(loc["file"])
        if info is None:
            errs.append(f"{fid}: {loc['file']} is not in the reviewed diff")
            continue
        on_line = _norm(info["lines"].get(loc["line"], ""))
        quotes = [_norm(ev["text"]) for ev in f["evidence"] if ev["kind"] == "excerpt"]
        if not on_line or not any(q and q in on_line for q in quotes):
            errs.append(f"{fid}: the quoted text is not on {loc['file']}:{loc['line']} of the diff")
    errs += check_excerpt("intent", a["intent"], texts, "reviewed")
    errs += [f"scope creep {p} is not in the reviewed diff" for p in a["scope_check"]["scope_creep"] if p not in files]
    for t in a["test_runs"]:
        if t["status"] == "not-run":
            if "source_id" in t:
                errs.append(f"test run '{t['command']}' is not-run but cites {t['source_id']}")
            continue
        sid = t.get("source_id")
        if sid is None:
            errs.append(f"test run '{t['command']}' is supplied but cites no capture")
        elif sid not in caps:
            errs.append(f"{sid} is not a real captured command log (artifact item, captured file, tagged command-capture)")
        elif caps[sid]["command"] != t["command"]:
            errs.append(f"test run '{t['command']}': capture {sid} ran a different command")
    return errs


def render_code_review(a: dict, ctx: "Context") -> Dict[str, str]:
    files = reviewed_diff(a, ctx)
    caps = command_captures(ctx)
    shown = [f for f in a["findings"] if _shown(f)]
    crit = sum(1 for f in shown if f["severity"] == "critical")
    L = _review_head(a, ctx, "Code review (audit only)")
    L += [f"PR type: {a['pr_type']}", ""] + ["Changed paths:"] + [f"- `{p}` ({cm.path_type(p)}{', deleted' if files[p]['deleted'] else ''}, "
                                                                 f"{len(files[p]['added'])} added lines)" for p in sorted(files)] + [""]
    sc = a["scope_check"]
    L += ["## Scope check", f"Intent (quoted from `{a['intent']['artifact_id']}`): \"{md_escape(a['intent']['text'])}\""]
    L += [f"- SCOPE CREEP: {p}" for p in sc["scope_creep"]] + [f"- MISSING REQUIREMENT: {md_escape(m)}" for m in sc["missing_requirements"]]
    if not sc["scope_creep"] and not sc["missing_requirements"]:
        L.append("- Scope Check: CLEAN")
    L += ["", f"## Pre-Landing Review: {len(shown)} issues ({crit} critical, {len(shown) - crit} informational)" if shown
          else "## Pre-Landing Review: No issues found."]
    for title, sev in (("Pass 1 — CRITICAL", "critical"), ("Pass 2 — INFORMATIONAL", "informational")):
        L += [f"### {title}"] + findings_block([f for f in a["findings"] if f["severity"] == sev]) + [""]
    L += ["## Categories"] + [f"- {k}: {a['categories'][k]['status']}" for k in REVIEW_CATEGORIES]
    na = [k for k in REVIEW_CATEGORIES if a["categories"][k]["status"] == "not-applicable"]
    if na:
        L.append(f"Not applicable (PR type: {a['pr_type']}): {', '.join(na)}. A not-applicable category never counts as passed.")
    L += ["", "## Tests"]
    for t in a["test_runs"]:
        if t["status"] == "not-run":
            L.append(f"- {md_escape(t['command'])}: not run by this review; no claim that the change passes")
        else:
            c = caps[t["source_id"]]
            L.append(f"- {md_escape(t['command'])}: {'passed' if c['exit'] == 0 else 'failed'} (exit {c['exit']}, supplied capture {t['source_id']})")
    if not a["test_runs"]:
        L.append("- none supplied; no claim that the change passes tests")
    L += ["", "## Decisions"] + ([decision_line(d) for d in a["decisions"]] or ["- none"]) + [""]
    L += suppressed_block(a)
    L += ["## Unknowns"] + evidence_gap_lines(a, ctx) + [""]
    L += ["This review changed no source: no fix was applied, committed or pushed. Repairs are a separate card on the existing "
          "engineering path, and a re-review needs the new diff.", ""] + handoff_lines(a["handoff"])
    typed = {"schema": "review-findings.v1", "pr_type": a["pr_type"],
             "reviewed": [{"artifact_id": r["artifact_id"], "digest": r["digest"]} for r in a["reviewed_artifacts"]],
             "categories": {k: a["categories"][k]["status"] for k in REVIEW_CATEGORIES},
             "findings": [{k: f[k] for k in ("id", "title", "section", "severity", "confidence", "disposition", "location", "defect_class",
                                             "fix_route", "decision_id") if k in f} for f in a["findings"]]}
    return {"review-report.md": "\n".join(L), "review-findings.json": json.dumps(typed, indent=2, ensure_ascii=False) + "\n"}


def _code_review_limits(a: dict, ctx: "Context") -> List[str]:
    if any(t["status"] == "supplied" for t in a["test_runs"]):
        return ["Test results come from supplied captures; this review ran nothing."]
    return ["No test log was supplied and this review ran no test: no claim that the change passes tests."]


# ---------------------------------------------------------------- qa (adapted gstack qa, audit half)

QA_CONTRACTS = ("success", "invalid-input", "auth", "cli-process", "state-transitions", "timeout", "retry", "idempotency",
                "concurrency", "partial-failure")
QA_TARGET_KINDS = ("code-change-handoff", "spec", "design-package", "ui-evidence", "eng-review", "lean-prd", "qa-report")


def _probe_outcome(p: dict, caps: Dict[str, dict]) -> Optional[str]:
    """Computed pass/fail of a functional probe from its first verified capture (None when not computable)."""
    got = [caps[c] for c in p["capture_ids"] if c in caps]
    if not got or "expect_exit" not in p:
        return None
    if len({g["exit"] for g in got}) > 1:
        return "conflict"
    return "pass" if got[0]["exit"] == p["expect_exit"] else "fail"


def validate_qa(a: dict, ctx: "Context") -> List[str]:
    pack = ctx.pack
    comps, errs = check_computations(a["computations"], pack)
    stmts, e = check_statements(a["statements"], comps, pack)
    errs += e + check_next_action(a["next_action"])
    reviewed, e = check_reviewed(a, ctx, QA_TARGET_KINDS, "QA target")
    errs += e
    if errs:
        return errs
    texts = artifact_texts(ctx, a["reviewed_artifacts"])
    decisions, e = check_decisions(a, pack)
    errs += e
    caps, shots = command_captures(ctx), browser_captures(ctx)
    errs += check_findings(a, stmts, texts, list(caps) + shots, decisions, QA_CONTRACTS + ("browser", "general"),
                           ("critical", "high", "medium", "low"))
    probes: Dict[str, dict] = {}
    for p in a["charter"]:
        pid, out = p["id"], p["outcome"]
        if pid in probes:
            errs.append(f"probe {pid}: duplicate id")
        probes[pid] = p
        errs += check_excerpt(pid, p["source"], texts, "QA target")
        if p["surface"] not in a["surfaces"]:
            errs.append(f"{pid} probes the {p['surface']} surface, which is not in the QA scope {a['surfaces']}")
        if out in ("pass", "fail"):
            if not p["capture_ids"]:
                errs.append(f"{pid} is {out} but cites no capture; an unrun probe is not run, never a pass")
                continue
            if p["surface"] == "browser":
                errs += [f"{pid}: browser outcomes need a supplied PNG or JPEG screenshot; {c} is not one" for c in p["capture_ids"] if c not in shots]
                continue
            for c in p["capture_ids"]:
                if c not in caps:
                    errs.append(f"{c} is not a command capture (real artifact item with a captured file tagged command-capture)")
                elif caps[c]["command"] != p["probe"]:
                    errs.append(f"{pid}: capture {c} ran a different command than the probe")
            if "expect_exit" not in p:
                errs.append(f"{pid}: a functional pass/fail needs the expected exit status")
            computed = _probe_outcome(p, caps)
            if computed == "conflict":
                errs.append(f"{pid}: captures disagree on the exit status; no outcome can be computed")
            elif computed is not None and computed != out:
                got = next(caps[c] for c in p["capture_ids"] if c in caps)
                errs.append(f"{pid}: captured exit {got['exit']}, expected {p['expect_exit']}: the outcome is {computed}, not {out}")
        else:
            if "reason" not in p:
                errs.append(f"{pid} is {out} without a reason")
            if out in ("not-run", "not-applicable") and p["capture_ids"]:
                errs.append(f"{pid} is {out} but cites captures")
    cited = {pid for b in a["bugs"] for pid in b["charter_ids"]}
    for b in a["bugs"]:
        for pid in b["charter_ids"]:
            if pid not in probes or probes[pid]["outcome"] != "fail":
                errs.append(f"bug {b['id']} cites {pid}, which did not fail; every bug needs a reproducing failed probe")
    errs += [f"{pid} failed but no bug reproduces it" for pid, p in probes.items() if p["outcome"] == "fail" and pid not in cited]
    func = [p for p in a["charter"] if p["surface"] == "functional"]
    if a["qa_mode"] == "full" and "functional" in a["surfaces"]:
        missing = [c for c in QA_CONTRACTS if c not in {p["contract"] for p in func}]
        if missing:
            errs.append("full QA covers every contract (pass, fail, blocked, not run or not applicable): missing " + ", ".join(missing))
    if a["qa_mode"] == "quick" and not any(p["contract"] == "success" and p["outcome"] in ("pass", "fail", "blocked") for p in func):
        errs.append("quick QA still checks success: one success probe must run (or be blocked)")
    if a["qa_mode"] == "regression":
        base = reviewed.get(a.get("baseline_artifact_id"))
        if base is None or base["kind"] != "qa-report":
            errs.append("regression QA needs a baseline qa-report artifact among the reviewed artifacts")
    return errs


def _qa_counts(a: dict) -> Dict[str, int]:
    out = {k: 0 for k in ("pass", "fail", "blocked", "not-run", "inconclusive", "not-applicable")}
    for p in a["charter"]:
        if p["surface"] == "functional":
            out[p["outcome"]] += 1
    return out


def render_qa(a: dict, ctx: "Context") -> Dict[str, str]:
    caps = command_captures(ctx)
    n = _qa_counts(a)
    L = _review_head(a, ctx, "QA report (audit only)")
    L += ["## Scope", f"Mode: {a['qa_mode']} · surfaces: {', '.join(a['surfaces'])}", ""]
    L += ["## Summary"]
    if "functional" in a["surfaces"]:
        L.append(f"Functional contracts: {n['pass']} pass, {n['fail']} fail, {n['blocked']} blocked, {n['not-run']} not run, "
                 f"{n['inconclusive']} inconclusive, {n['not-applicable']} not applicable.")
        L.append("No health score: functional coverage is reported as contract outcomes, never a score.")
    browser = [p for p in a["charter"] if p["surface"] == "browser"]
    L.append("Browser surface: not run" if not browser or all(p["outcome"] in ("not-run", "not-applicable") for p in browser)
             else f"Browser surface: {sum(1 for p in browser if p['outcome'] in ('pass', 'fail'))} probes with supplied screenshots (not scored here)")
    L += [f"QA found {len(a['bugs'])} issues; fixed 0 (audit only).", "", "## Charter"]
    for p in a["charter"]:
        src = f"`{p['source']['artifact_id']}`: \"{md_escape(p['source']['text'])}\""
        if p["outcome"] in ("pass", "fail") and p["surface"] == "functional":
            got = next(caps[c] for c in p["capture_ids"] if c in caps)
            L.append(f"- `{p['id']}` {p['contract']}: {p['outcome']} (captured exit {got['exit']}, expected {p['expect_exit']}; "
                     f"{', '.join(p['capture_ids'])}) — probe `{md_escape(p['probe'])}`; expectation: {md_escape(p['expectation'])}; source {src}")
        elif p["outcome"] in ("pass", "fail"):
            L.append(f"- `{p['id']}` {p['contract']} (browser): {p['outcome']} — screenshots {', '.join(p['capture_ids'])}; source {src}")
        else:
            L.append(f"- `{p['id']}` {p['contract']}: {p['outcome'].replace('-', ' ')} — {md_escape(p.get('reason', ''))}")
    L += ["", "## Bugs (reproduced)"]
    for b in a["bugs"]:
        L += [f"### `{b['id']}` {md_escape(b['title'])} ({b['severity']}, {b['category']})", f"- Failed probes: {', '.join(b['charter_ids'])}",
              "- Steps: " + " → ".join(md_escape(s) for s in b["repro_steps"]), f"- Expected: {md_escape(b['expected'])}",
              f"- Actual: {md_escape(b['actual'])}", "- Fix status: deferred to the engineering path (audit only)", ""]
    if not a["bugs"]:
        L += ["- none reproduced", ""]
    L += ["## Findings (not reproduced)"] + findings_block(a["findings"]) + [""]
    L += suppressed_block(a)
    L += ["## Unknowns"] + evidence_gap_lines(a, ctx) + [""]
    L += ["This QA pass changed no source and ran nothing itself: outcomes come from supplied captures re-read by digest. "
          "Repairs go to the existing engineering path.", ""] + handoff_lines(a["handoff"])
    charter = {"schema": "qa-charter.v1", "mode": a["qa_mode"],
               "target": [{"artifact_id": r["artifact_id"], "digest": r["digest"]} for r in a["reviewed_artifacts"]],
               "charter": [{k: p[k] for k in ("id", "contract", "surface", "probe", "outcome", "capture_ids", "expect_exit", "reason") if k in p}
                           for p in a["charter"]],
               "captures": {c: {"command": v["command"], "exit": v["exit"], "digest": v["digest"]}
                            for c, v in sorted(caps.items()) if any(c in p["capture_ids"] for p in a["charter"])},
               "bugs": [b["id"] for b in a["bugs"]]}
    return {"qa-report.md": "\n".join(L), "qa-charter.json": json.dumps(charter, indent=2, ensure_ascii=False) + "\n"}


def _qa_limits(a: dict, ctx: "Context") -> List[str]:
    unrun = [p["id"] for p in a["charter"] if p["outcome"] in ("not-run", "blocked", "inconclusive")]
    out = ["Outcomes come from coordinator-supplied captures re-read by digest; the validator ran no probe."]
    if unrun:
        out.append(f"Contracts not run, blocked or inconclusive ({', '.join(unrun)}): no claim about them.")
    return out


# ---------------------------------------------------------------- kinds

@dataclass(frozen=True)
class Kind:
    def_name: str
    capability_id: str
    validate: Callable[[dict, "Context"], List[str]]
    render: Callable[[dict, "Context"], Dict[str, str]]
    outputs: Dict[str, str]  # file name -> artifact kind
    post_render: Callable[[dict, Dict[str, str]], List[str]] = lambda a, f: []
    needs_input: Callable[[dict, "Context"], List[str]] = lambda a, c: []
    limitations: Callable[[dict, "Context"], List[str]] = lambda a, c: []


@dataclass
class Context:
    registry: dict
    pack: Optional[Pack] = None
    store: Optional[ArtifactStore] = None
    extra: Optional[Dict[str, Any]] = None


def _pd_needs(a: dict, ctx: Context) -> List[str]:
    out = [f"question for the requester: {q['text']}" for q in a["questions"]]
    out += [f"approval needed before the next action: {x}" for x in a["next_action"]["approvals_needed"]]
    return out


KINDS: Dict[str, Kind] = {
    "problem-definition": Kind("problem_definition", "kit.problem-definition", validate_problem_definition,
                               render_problem_definition,
                               {"decision-brief.md": "decision-brief", "problem-brief.md": "problem-brief",
                                "assumption-register.md": "assumption-register", "one-page-prd.md": "one-page-prd"},
                               post_render_problem_definition, _pd_needs),
    "product-discovery": Kind("product_discovery", "pm.product-discovery", validate_product_discovery,
                              render_product_discovery,
                              {"discovery-brief.md": "discovery-brief", "assumption-register.md": "assumption-register",
                               "experiment-plan.md": "experiment-plan"},
                              needs_input=_discovery_needs,
                              limitations=lambda a, c: ["Checkpoint answers are requester answers recorded in evidence-pack items "
                                                        "(cited by source ID); they choose ideas and focus but approve no "
                                                        "outreach, spend or publication."]),
    "lean-prd": Kind("lean_prd", "pm.lean-prd", validate_lean_prd, render_lean_prd,
                     {"lean-prd.md": "lean-prd", "acceptance-map.md": "acceptance-map",
                      "implementation-handoff.md": "implementation-handoff"},
                     post_render_lean_prd, lambda a, c: _approval_needs(a)),
    "design-consultation": Kind("design_consultation", "gstack.design-consultation", validate_design_consultation,
                                render_design_consultation,
                                {"design-brief.md": "design-brief", "design-package.md": "design-package",
                                 "design-tokens.json": "design-package", "design-preview.html": "design-package"},
                                post_render_design_consultation, _consult_needs,
                                lambda a, c: ["The preview was checked statically (markup, CSS, tokens); it was not rendered in a "
                                              "browser, run through assistive technology or shown to users.",
                                              "Font and asset rights are recorded from the cited supplied sources; this tool did "
                                              "not verify any licence."]),
    "design-review": Kind("design_review", "gstack.design-review", validate_design_review, render_design_review,
                          {"design-review-report.md": "design-review-report", "static-checks.json": "design-review-report"},
                          needs_input=lambda a, c: _approval_needs(a), limitations=_review_limits),
    "competitor-research": Kind("competitor_research", "pm.competitor-research", validate_competitor_research,
                                render_competitor_research,
                                {"competitor-report.md": "competitor-report", "evidence-ledger.json": "evidence-ledger"},
                                needs_input=lambda a, c: _approval_needs(a), limitations=_research_limits),
    "office-hours": Kind("office_hours", "gstack.office-hours", validate_office_hours, render_office_hours,
                         {"opportunity-brief.md": "opportunity-brief"}, needs_input=_office_needs),
    "spec": Kind("spec", "gstack.spec", validate_spec, render_spec, {"spec.md": "spec", "issue-draft.md": "spec"},
                 needs_input=lambda a, c: _approval_needs(a)),
    "plan-eng-review": Kind("plan_eng_review", "gstack.plan-eng-review", validate_plan_eng_review, render_plan_eng_review,
                            {"eng-review.md": "eng-review", "test-plan.md": "eng-review"}, needs_input=review_needs),
    "plan-design-review": Kind("plan_design_review", "gstack.plan-design-review", validate_plan_design_review,
                               render_plan_design_review, {"design-plan-review.md": "design-plan-review"}, needs_input=review_needs),
    "code-review": Kind("code_review", "gstack.review", validate_code_review, render_code_review,
                        {"review-report.md": "review-report", "review-findings.json": "review-report"},
                        needs_input=review_needs, limitations=_code_review_limits),
    "qa": Kind("qa", "gstack.qa", validate_qa, render_qa, {"qa-report.md": "qa-report", "qa-charter.json": "qa-report"},
               needs_input=review_needs, limitations=_qa_limits),
}


# ---------------------------------------------------------------- validate + render pipeline

def binding_policy_errors(a: Any, registry: dict) -> List[str]:
    """The planner's binding gates, applied before any evidence pack, artifact index or artifact byte is
    read: company-private execution is refused (no protected company boundary is accepted), and the binding
    class must be one the capability's registry scope allows. Malformed input is left to the contract check."""
    if not isinstance(a, dict) or a.get("kind") not in KINDS or not isinstance(a.get("binding"), dict):
        return []
    cls = a["binding"].get("data_classification")
    if cls == "company-private":
        return [cr.PRIVATE_RUNTIME_REFUSAL]
    cid = KINDS[a["kind"]].capability_id
    cap = next((c for c in registry["capabilities"] if c["id"] == cid), None)
    if cap is not None and cls not in cap["scope"]["data_classes"]:
        return [f"{cid} does not allow {cls} data (capability scope.data_classes)"]
    return []


def analysis_gate(a: Any, registry: dict) -> Tuple[Optional[Kind], Optional[dict], List[str]]:
    """Read-free gate over the untrusted analysis: known kind, contract (schema) valid, capability present,
    binding policy allowed. Every caller runs it before any evidence pack, index or artifact byte is read
    and before any output is built."""
    if not isinstance(a, dict) or a.get("kind") not in KINDS:
        return None, None, [f"analysis kind must be one of {sorted(KINDS)}"]
    kind = KINDS[a["kind"]]
    errs = contract_errors(a, kind.def_name, "analysis")
    if errs:
        return kind, None, errs
    cap = next((c for c in registry["capabilities"] if c["id"] == kind.capability_id), None)
    if cap is None:
        return kind, None, [f"capability {kind.capability_id} is not in the registry"]
    return kind, cap, binding_policy_errors(a, registry)


def validate_analysis(a: Any, ctx: Context) -> Tuple[Optional[Kind], List[str]]:
    kind, cap, errs = analysis_gate(a, ctx.registry)
    if errs:  # before the store is bound or any artifact byte is read
        return kind, errs
    if a.get("mode") is not None and a["mode"] not in cap["execution"]["modes"]:
        errs.append(f"mode {a['mode']!r} is not a mode of {cap['id']}")
    bind_errs = check_binding(a, ctx.pack)
    if bind_errs:  # pack-binding mismatch: refuse before the store is bound or any pack/store byte is read
        return kind, errs + bind_errs
    if ctx.store is not None:
        e = ctx.store.bind(a["binding"])
        if e:
            return kind, errs + e
    errs += check_reuse(a.get("reused_artifacts", []), a, ctx.store, ctx.registry)
    errs += kind.validate(a, ctx)
    return kind, errs


def render_analysis(a: dict, ctx: Context) -> Tuple[Dict[str, str], List[str]]:
    kind, errs = validate_analysis(a, ctx)
    if errs:
        return {}, errs
    files = kind.render(a, ctx)
    errs = kind.post_render(a, files)
    return (files, errs) if not errs else ({}, errs)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_outputs(a: dict, ctx: Context, files: Dict[str, str], analysis_raw: bytes, *, run_ref: str) -> Dict[str, bytes]:
    """Artifact files + analysis copy + evidence copy + Artifact v1 index + CapabilityResult v1.
    Gates on its own (callers may skip render_analysis): the analysis gate, the pack binding/reference and
    the exact pack bytes, and the store binding, all before any artifact byte is read or output built."""
    kind, cap, errs = analysis_gate(a, ctx.registry)
    if not errs:
        errs = check_binding(a, ctx.pack)
    if not errs and ctx.pack is not None:
        pack_raw = (ctx.extra or {}).get("pack_raw")
        if not isinstance(pack_raw, bytes) or cr.sha256_bytes(pack_raw) != ctx.pack.digest:
            errs = ["evidence pack bytes differ from the validated pack digest"]
    if not errs and ctx.store is not None and ctx.store.bind(a["binding"]):
        errs = ["the artifact index was loaded for a different task binding"]
    if errs:
        raise WorkflowError("refusing to build outputs: " + "; ".join(errs))
    b = a["binding"]
    out: Dict[str, bytes] = {name: body.encode("utf-8") for name, body in files.items()}
    out["analysis.json"] = analysis_raw
    arts, source_refs = [], []
    if ctx.pack is not None:
        pack_bytes = ctx.extra["pack_raw"]
        out["evidence-pack.json"] = pack_bytes
        arts.append({"artifact_id": ctx.pack.doc["pack_id"], "kind": "evidence-index", "company_id": b["company_id"],
                     "project_id": b["project_id"], "task_id": b["task_id"], "content_digest": cr.sha256_bytes(pack_bytes),
                     "produced_by": None, "generation": 1, "data_classification": b["data_classification"],
                     "location": "evidence-pack.json", "source_refs": [], "review_status": "draft"})
        source_refs.append({"artifact_id": ctx.pack.doc["pack_id"], "digest": cr.sha256_bytes(pack_bytes)})
    inputs = a.get("reused_artifacts", []) + a.get("reviewed_artifacts", [])
    for r in inputs:
        source_refs.append({"artifact_id": r["artifact_id"], "digest": r["digest"]})
    if ctx.store is not None:
        # Copy each reused input with its whole source closure, so a later stage can verify the chain
        # from this index alone; a source the store lacks stays absent (and therefore unverifiable).
        seen = {x["artifact_id"] for x in arts}
        todo = [r["artifact_id"] for r in inputs]
        while todo:
            aid = todo.pop(0)
            found = ctx.store.get(aid)
            if aid in seen or found is None:
                continue
            seen.add(aid)
            if not in_task_scope(found, b):
                raise WorkflowError(OUT_OF_SCOPE)  # refused before any byte of the foreign entry is read
            src = dict(found)
            name = f"input-{len(arts)}-{src['kind']}.{src['location'].rsplit('.', 1)[-1] if '.' in src['location'] else 'dat'}"
            name = re.sub(r"[^a-z0-9.-]", "-", name.lower())[:60]
            out[name] = ctx.store.read_bytes(aid)
            src["location"] = name
            arts.append(src)
            todo += [s["artifact_id"] for s in src["source_refs"]]
    stem = a["binding"]["task_id"]
    for name, body in files.items():
        akind = kind.outputs.get(name)
        if akind is None:
            continue
        arts.append({"artifact_id": f"{stem}.{name.rsplit('.', 1)[0]}", "kind": akind, "company_id": b["company_id"],
                     "project_id": b["project_id"], "task_id": b["task_id"], "content_digest": cr.sha256_bytes(out[name]),
                     "produced_by": cr.producer_record(cap, run_ref),
                     "generation": 1, "data_classification": b["data_classification"], "location": name,
                     "source_refs": source_refs, "review_status": "draft"})
    index = {"schema_version": 1, "artifacts": arts}
    out["artifacts.json"] = (json.dumps(index, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    pending = kind.needs_input(a, ctx)
    produced = [x for x in arts if x["produced_by"] is not None]
    now = _now()
    statements = a.get("statements", [])
    result = {
        "schema_version": 1, "capability_id": cap["id"], "capability_version": cap["version"],
        "source_digest": cap["source"]["content_digest"], "implementation_digest": cap["implementation"]["digest"],
        "mode": a.get("mode") or cap["execution"]["default_mode"], "task_id": b["task_id"],
        "status": "needs_input" if pending else "completed",
        "summary": f"{a['kind']} analysis validated against its contract and rendered as {len(produced)} draft artifact(s)",
        "artifacts": [{"artifact_id": x["artifact_id"], "kind": x["kind"], "digest": x["content_digest"]} for x in produced],
        "sources": [_result_source(it) for it in (ctx.pack.doc["items"] if ctx.pack else [])],
        "claims": [{"claim_id": s["id"], "text": s["text"][:600], "classification": s["class"], "source_ids": s["source_ids"]}
                   for s in statements][:300],
        "checks": [{"evidence_id": f"{stem}.contract-check", "check_id": f"{a['kind']}-contract-v1",
                    "subject": {"capability_id": cap["id"]}, "method": "command",
                    "method_detail": "capability_workflow.py schema + semantic rubric over the supplied analysis and evidence",
                    "environment_ref": "env:source-checkout", "started_at": now, "ended_at": now, "status": "passed",
                    "exit_code": 0, "counts": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
                    "classification": "observed",
                    "no_claim": ["Contract validity is not independent review, acceptance or outcome verification.",
                                 "Says nothing about installation or runtime readiness."]}],
        "assumptions": [s["text"][:600] for s in statements if s["class"] in ("assumption", "hypothesis")][:50],
        "unknowns": ([s["text"][:600] for s in statements if s["class"] == "unknown"] + pending)[:50],
        "decisions": [], "approval_requests": [], "execution_receipt_ref": None,
        "limitations": ["Analysis content is coordinator-authored; the renderer adds no facts.",
                        "No trusted receipt: requester answers, approvals and captured command/browser outcomes are "
                        "coordinator-supplied evidence-pack items bound by digest only; they stay unverified until checked "
                        "with the requester and a trusted execution record.",
                        "Artifacts are drafts until a reviewer accepts them."]
        + (["Synthetic fixture data: not real research or customer evidence."] if b["data_classification"] == "synthetic" else [])
        + kind.limitations(a, ctx),
    }
    if pending and not result["unknowns"]:
        result["unknowns"] = pending[:50]
    errs = cr.validate_result(result, ctx.registry)
    if errs:
        raise WorkflowError("internal: produced result fails CapabilityResult v1: " + "; ".join(errs))
    out["result.json"] = (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return out


def _result_source(it: dict) -> dict:
    kind = {"interview-notes": "document"}.get(it["kind"], it["kind"])
    src = {"source_id": it["source_id"], "kind": kind, "ref": it["ref"]}
    if "retrieved_at" in it:
        src["retrieved_at"] = it["retrieved_at"]
    if "digest" in it:
        src["digest"] = it["digest"]
    return src


# ---------------------------------------------------------------- bounded output directory

def _check_no_symlink_components(path: Path) -> None:
    cur = Path(path.anchor)
    for part in path.parts[1:]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            raise WorkflowError(f"{cur}: missing") from None
        if stat.S_ISLNK(st.st_mode):
            raise WorkflowError(f"{cur}: refusing a symlinked path component")
        if not stat.S_ISDIR(st.st_mode):
            raise WorkflowError(f"{cur}: not a directory")


def write_fresh_dir(out_dir: Path, files: Dict[str, bytes]) -> List[str]:
    """Create ``out_dir`` (must not exist; parent must be a real, non-symlinked directory) with mode
    0700 and write each fixed-name file with O_EXCL|O_NOFOLLOW relative to the directory fd.
    Never overwrites, follows links or writes outside the directory."""
    out_dir = Path(os.path.abspath(out_dir))
    if os.path.lexists(out_dir):
        raise WorkflowError(f"{out_dir}: output directory already exists (outputs are never overwritten)")
    _check_no_symlink_components(out_dir.parent)
    total = sum(len(v) for v in files.values())
    if total > MAX_OUTPUT_BYTES:
        raise WorkflowError(f"outputs total {total} bytes, over the {MAX_OUTPUT_BYTES}-byte bound")
    for name in files:
        if not OUTPUT_NAME_RE.match(name) or name in (".", ".."):
            raise WorkflowError(f"refusing output name {name!r}")
    os.mkdir(out_dir, 0o700)
    dfd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    written = []
    try:
        st = os.fstat(dfd)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise WorkflowError(f"{out_dir}: not a private directory owned by this user")
        for name in sorted(files):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dfd)
            with os.fdopen(fd, "wb") as fh:
                fh.write(files[name])
            written.append(name)
    finally:
        os.close(dfd)
    return written


# ---------------------------------------------------------------- CLI

def _context(args: Any, registry: dict, analysis: Any = None) -> Tuple[Context, List[str]]:
    ctx = Context(registry=registry, extra={})
    _, _, errs = analysis_gate(analysis, registry)
    if errs:  # unknown, malformed, private or out-of-scope: refused before the pack or index is opened
        return ctx, errs
    binding = analysis["binding"]
    if args.evidence:
        # the pack binding is checked against the analysis binding before any captured file is opened
        pack, e = load_pack(Path(args.evidence), binding=binding)
        errs += e
        if pack is not None:
            ctx.pack = pack
            ctx.extra["pack_raw"] = pack.raw  # the bytes that were digested, not a second read
    if errs:
        return ctx, errs
    if args.artifacts:
        root = Path(args.artifact_root) if args.artifact_root else Path(os.path.abspath(args.artifacts)).parent
        # bound to the task before any content read
        store, e = load_artifacts(Path(args.artifacts), root, binding)
        errs += e
        ctx.store = store
    if args.today:
        try:
            ctx.extra["today"] = _dt.date.fromisoformat(args.today).isoformat()
        except ValueError:
            errs.append(f"--today {args.today!r} is not a YYYY-MM-DD date")
    if args.snapshot_dir:
        ctx.extra["snapshots"] = load_snapshots(Path(args.snapshot_dir))
    return ctx, errs


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Validate a coordinator-authored workflow analysis and render its draft artifacts "
                                             "(no network, no shell, no repository writes)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "render"):
        p = sub.add_parser(name)
        p.add_argument("--analysis", required=True)
        p.add_argument("--evidence")
        p.add_argument("--artifacts", help="Artifact v1 index of accepted inputs (for reuse checks)")
        p.add_argument("--artifact-root")
        p.add_argument("--today", help="YYYY-MM-DD: refuse research whose cited sources are past their re-verify date")
        p.add_argument("--snapshot-dir", help="directory of full retrieved snapshots; derived excerpts are re-checked against them")
        if name == "render":
            p.add_argument("--out", required=True, help="fresh output directory (must not exist)")
            p.add_argument("--run-ref", default="run:local-render")
    args = ap.parse_args(argv)
    try:
        registry, _ = cr.load_registry()
        raw = _read_bytes(Path(args.analysis), cr.MAX_JSON_BYTES)
        analysis = cr.parse_json(raw.decode("utf-8"))
        ctx, errs = _context(args, registry, analysis)
        if not errs:
            if args.cmd == "validate":
                _, errs = validate_analysis(analysis, ctx)
            else:
                files, errs = render_analysis(analysis, ctx)
        if errs:
            print(json.dumps({"ok": False, "errors": errs}, indent=2))
            return 1
        if args.cmd == "validate":
            print(json.dumps({"ok": True, "kind": analysis["kind"], "errors": []}, indent=2))
            return 0
        out = build_outputs(analysis, ctx, files, raw, run_ref=args.run_ref)
        written = write_fresh_dir(Path(args.out), out)
        result = json.loads(out["result.json"])
        print(json.dumps({"ok": True, "kind": analysis["kind"], "out": str(Path(os.path.abspath(args.out))),
                          "files": written, "status": result["status"]}, indent=2))
        return 0
    except (cr.RegistryError, UnicodeDecodeError) as exc:
        print(f"capability_workflow: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"capability_workflow: {exc.filename or 'input'}: {exc.strerror or type(exc).__name__}", file=sys.stderr)
        return 2
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, RecursionError, MemoryError) as exc:
        # last resort: malformed input that slipped past a contract check is reported, never a traceback
        print(f"capability_workflow: malformed input ({type(exc).__name__}: {str(exc)[:200]})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
