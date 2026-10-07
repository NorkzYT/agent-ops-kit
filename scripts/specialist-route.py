#!/usr/bin/env python3
"""Dry-run the specialist-router pilot for one task. Offline; nothing is dispatched.

Prints the routing trace as JSON: selected profiles, reason labels, high-risk
and advisory flags, execution mode (kanban | short), knowledge readiness, and
what was dropped and why, then a one-screen summary of the same trace. Neither
contains the task text (only a 16-hex hash). Readiness is stat-only: it lists
file names under each profile's knowledge/ and skills/ folders and reads
SKILL.md frontmatter, never a book. Without a task it prints usage and exits 0.

Usage:
  python3 scripts/specialist-route.py "Plan pricing and a sales funnel"
  echo "task" | python3 scripts/specialist-route.py -
  TASK="..." make specialist-route            # same, via make
  make specialist-route TASK=- < prompt.txt   # task from a file
Options:
  --profiles-root DIR   profiles dir (default $HERMES_HOME/profiles or ~/.hermes/profiles)
  --max N               route cap (default 4, max 6)
  --allow a,b           allowlist of profiles (default: all book-backed + local-engineer)
  --require-ready       drop profiles whose /learn knowledge skill is missing
  --private-session     treat the session as already flagged private
  --plan                also print the note the orchestrator would receive
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes" / "plugins" / "model-router"
MODULE = "model_router_cli"


def _load(name: str):
    if MODULE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            MODULE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = MODULE
        mod.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
        sys.modules[MODULE] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return importlib.import_module(f"{MODULE}.{name}")


USAGE = """specialist-route: no task given; nothing routed (offline dry-run, nothing dispatched).

Usage:
  make specialist-route TASK="Plan pricing and a sales funnel for my course"
  make specialist-route TASK="..." PLAN=1           also print the orchestrator note
  make specialist-route TASK="..." REQUIRE_READY=1  drop profiles without a /learn skill
  make specialist-route TASK=- < prompt.txt         task text from a file or stdin
  python3 scripts/specialist-route.py [--plan] [--profiles-root DIR] "task text"

Output: the JSON trace, then a summary: selected profiles with execution mode,
knowledge readiness, reasons and HIGH-RISK/advisory flags; dropped profiles and
why; the task source (typed, or attachment-derived for a bare uploaded file).

Related:
  make specialist-status   plugin vs repo, specialist_* settings, readiness (read-only)
  make specialist-logs     recent live routing decisions (content-free; LINES=<n>)
"""


def summary(trace: dict) -> str:
    """One-screen, content-free reading of the trace (labels and names only)."""
    source = {"attachment": "attachment-derived", "sender": "typed"}.get(trace.get("task_source"), "-")
    lines = ["--- summary ---",
             f"task {trace.get('task', '-')}  source {source}  privacy {trace.get('privacy')}"
             f"  tier {trace.get('tier')}  classifier {trace.get('classifier')}"]
    routes = trace.get("routes") or []
    if routes:
        lines.append(f"selected {len(routes)}/{trace.get('max_profiles')}:")
        for r in routes:
            k = r["knowledge"]
            flags = ["HIGH-RISK advisory-only" if r.get("high_risk") else
                     "advisory" if r.get("advisory_only") else "implementer"]
            if r.get("local_only"):
                flags.append("local-only")
            lines.append(f"  {r['profile']:<26} {r['execution']:<6} knowledge {k['status']}"
                         f"{' (stale)' if k.get('stale') else ''}  reasons {','.join(r['reasons'])}"
                         f"  [{'; '.join(flags)}]")
    else:
        lines.append(f"no specialist selected ({trace.get('reason', '-')})")
    for d in trace.get("dropped") or []:
        lines.append(f"  dropped {d['profile']:<18} {d['reason']}")
    if trace.get("unknown_allowed"):
        lines.append(f"unknown allowlist names ignored: {','.join(trace['unknown_allowed'])}")
    lines.append(f"profiles root {trace.get('profiles_root')}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Specialist-router dry-run (offline)")
    ap.add_argument("task", nargs="?", default=None, help="task text, or - for stdin (default: $TASK)")
    ap.add_argument("--profiles-root", default=os.environ.get("PROFILES_ROOT") or None)
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--allow", default=os.environ.get("ALLOW", ""))
    ap.add_argument("--require-ready", action="store_true")
    ap.add_argument("--private-session", action="store_true")
    ap.add_argument("--plan", action="store_true")
    args = ap.parse_args(argv)

    text = args.task if args.task is not None else os.environ.get("TASK", "")
    if text == "-":
        text = sys.stdin.read()
    if not text.strip():
        print(USAGE, end="")
        return 0

    sp = _load("specialists")
    cfg = _load("router_config").load_settings(None)  # env-only: MODEL_ROUTER_* knobs
    known, unknown = sp.split_allowed(args.allow.split(",") if args.allow else cfg.specialist_profiles)
    settings = sp.SpecialistSettings(
        enabled=True,  # a dry-run always routes; the live hook needs specialist_routing: true
        max_profiles=sp.clamp_max(args.max if args.max is not None else cfg.specialist_max_profiles),
        allowed=known, unknown=unknown,
        require_ready=args.require_ready or cfg.specialist_require_ready,
        profiles_root=Path(args.profiles_root).expanduser() if args.profiles_root else None,
        private_terms=tuple(cfg.private_terms), private_paths=tuple(cfg.private_paths),
    )
    trace = sp.route_text(text, settings, session_private=args.private_session)
    trace["profiles_root"] = str(settings.profiles_root or sp.default_profiles_root())
    print(json.dumps(trace, indent=2, sort_keys=True))
    print(summary(trace))
    if args.plan:
        print(sp.render_plan(trace) or "(no specialist note: nothing routed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
