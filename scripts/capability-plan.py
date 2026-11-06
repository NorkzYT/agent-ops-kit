#!/usr/bin/env python3
"""Print the smallest capability plan for one task. Read-only: writes nothing, runs nothing.

Input is a TaskEnvelope plus a closed-ID selection: either a selection record
(``--selection``, e.g. the model-router seam's output) or explicit offline
``--capability`` flags, labeled "explicit offline evaluation input" in the plan.
Nothing here classifies task text. Tool needs and approvals in the plan are
requirements for the trusted policy layer, never grants.

Usage:
  python3 scripts/capability-plan.py --task task.json --capability pm.lean-prd [--mode pm.lean-prd=lean]
  python3 scripts/capability-plan.py --task task.json --selection selection.json \
      [--artifacts index.json [--artifact-root DIR]] [--evidence evidence.json] \
      [--project project.json] [--company company.json ...] [--format json|summary]

Without --artifact-root every reuse decision is labelled content_check "unverified" (it rests on
index-declared digests only); with it, matching bytes relabel them "verified" and any mismatch
yields an invalid plan with no stages or reuse decisions. Only artifacts of the task's own
company/project are checked; other tenants' entries in a shared index are not read or named.

Exit: 0 plan printed (planned, needs_input or blocked), 2 invalid input, 3 refused.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def _registry():
    if "capability_registry" not in sys.modules:
        spec = importlib.util.spec_from_file_location("capability_registry", SCRIPTS / "capability_registry.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["capability_registry"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return sys.modules["capability_registry"]


def summary(plan: dict) -> str:
    prov = plan["selection_provenance"]
    lines = [f"task {plan['task_id']}  status {plan['status']}  executable_now {plan['executable_now']}",
             f"selection: {prov['label']} (outcome {prov['outcome']})"]
    for r in plan["reasons"]:
        lines.append(f"  reason: {r}")
    for e in plan["errors"]:
        lines.append(f"  error: {e}")
    for i, st in enumerate(plan["stages"], 1):
        dims = st["readiness"]["dimensions"]
        lines.append(f"{i}. {st['capability_id']} [{st['mode']}] {st['action']} ({st['added_because']}"
                     f"{' for ' + ','.join(st['required_by']) if st['required_by'] else ''})"
                     f"{'  after ' + ','.join(st['after']) if st['after'] else ''}")
        lines.append("   readiness " + " ".join(f"{d}={v['state']}" for d, v in dims.items()))
        lines.append(f"   needs {','.join(st['tool_needs']['required']) or '-'} (optional {','.join(st['tool_needs']['optional']) or '-'}); "
                     f"{st['tool_needs']['note']}")
        if st["approvals_required"]:
            lines.append("   approvals " + "; ".join(f"{a['action']} ({a['when']})" for a in st["approvals_required"]))
        for b in st["blockers"]:
            lines.append(f"   blocked: {b}")
    for n in plan["needs_input"]:
        lines.append(f"needs input: {n['capability_id']}.{n['input']} {n['kinds']}")
    for a in plan["reused_artifacts"]:
        lines.append(f"reuses {a['artifact_id']} ({a['kind']}) for {a['used_by']}; content {a['content_check']}"
                     + (" (index digest only; pass --artifact-root to check bytes)" if a["content_check"] != "verified" else ""))
    for a in plan["stale_artifacts"]:
        lines.append(f"stale {a['artifact_id']}: {a['reason']}")
    for p in plan["advisory_profiles"]:
        lines.append(f"advisory {p['profile']} ({p['execution']}, knowledge {p['knowledge_status']})")
    lines.append(f"permission effect: {plan['permission_effect']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only capability plan for one task")
    ap.add_argument("--task", required=True, help="TaskEnvelope v1 JSON")
    ap.add_argument("--selection", help="selection v1 JSON (closed capability IDs)")
    ap.add_argument("--capability", action="append", default=[], help="explicit offline capability ID (repeatable)")
    ap.add_argument("--mode", action="append", default=[], help="ID=MODE for an explicit capability")
    ap.add_argument("--artifacts", help="Artifact v1 index JSON")
    ap.add_argument("--artifact-root", help="verify artifact bytes under this directory")
    ap.add_argument("--evidence", help="Evidence v1 index JSON (treated as reported, never observed)")
    ap.add_argument("--project", help="project manifest JSON")
    ap.add_argument("--company", action="append", default=[], help="company manifest JSON (repeatable)")
    ap.add_argument("--format", choices=("json", "summary"), default="json")
    args = ap.parse_args(argv)
    cr = _registry()

    if bool(args.selection) == bool(args.capability):
        print("capability-plan: give exactly one of --selection or --capability", file=sys.stderr)
        return 2
    try:
        registry, lock = cr.load_registry()
        reg_errs = cr.validate_registry(registry, lock)
        if reg_errs:
            print("capability-plan: the registry is invalid:\n  " + "\n  ".join(reg_errs), file=sys.stderr)
            return 2
        envelope = cr.load_json(Path(args.task))
        if args.selection:
            selection = cr.load_json(Path(args.selection))
        else:
            modes = {}
            for m in args.mode:
                cid, sep, mode = m.partition("=")
                if not sep:
                    print(f"capability-plan: --mode needs ID=MODE, got {m!r}", file=sys.stderr)
                    return 2
                modes[cid] = mode
            task_id = envelope.get("task_id") if isinstance(envelope, dict) else None
            selection = {"schema_version": 1, "selection_id": "explicit-offline", "task_id": task_id,
                         "selector": {"kind": "explicit-offline", "outcome": "ok"}, "capability_ids": list(args.capability)}
            if modes:
                selection["modes"] = modes
        artifacts = cr.load_json(Path(args.artifacts)) if args.artifacts else None
        evidence = cr.load_json(Path(args.evidence)) if args.evidence else None
        project = cr.load_json(Path(args.project)) if args.project else None
        companies = [cr.load_json(Path(c)) for c in args.company] or None
    except cr.RegistryError as exc:
        print(f"capability-plan: {exc}", file=sys.stderr)
        return 2

    reported = []
    if evidence is not None:
        errs = cr.schema_errors(evidence, "evidence.schema.json", "evidence")
        if errs:
            print("capability-plan: " + "\n  ".join(errs), file=sys.stderr)
            return 2
        reported = evidence["evidence"]
    plan = cr.build_plan(registry, selection, envelope, artifacts, reported=reported, project=project, companies=companies)
    if args.artifact_root and artifacts is not None and plan["status"] != "invalid":
        # Only this task's company/project: another tenant's artifact in a shared index is neither
        # hashed nor named (a foreign explicit reference is already refused by build_plan).
        own = {"artifacts": [a for a in artifacts["artifacts"] if (a["company_id"], a["project_id"])
                             == (envelope["company_id"], envelope["project_id"])]}
        content_errs = cr.verify_artifact_contents(own, Path(args.artifact_root))
        if content_errs:
            # No decision may rest on bytes that failed the check: drop every stage and reuse decision.
            plan.update(status="invalid", errors=content_errs, reasons=["artifact contents do not match the index"],
                        stages=[], reused_artifacts=[], needs_input=[], executable_now=False)
        else:
            cr.mark_contents_verified(plan)
    print(json.dumps(plan, indent=2, sort_keys=True) if args.format == "json" else summary(plan))
    return {"invalid": 2, "refused": 3}.get(plan["status"], 0)


if __name__ == "__main__":
    sys.exit(main())
