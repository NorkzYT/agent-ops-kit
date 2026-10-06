#!/usr/bin/env python3
"""Dry-run the specialist-router pilot for one task. Offline; nothing is dispatched.

Prints the routing trace as JSON: selected profiles, reason labels, high-risk
and advisory flags, execution mode (kanban | short), knowledge readiness, and
what was dropped and why. The trace never contains the task text (only a
16-hex hash). Readiness is stat-only: it lists file names under each profile's
knowledge/ and skills/ folders and reads SKILL.md frontmatter, never a book.

Usage:
  python3 scripts/specialist-route.py "Plan pricing and a sales funnel"
  echo "task" | python3 scripts/specialist-route.py -
  TASK="..." make specialist-route            # same, via make
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
        ap.error("no task text (pass it as an argument, '-' for stdin, or TASK=...)")

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
    if args.plan:
        print(sp.render_plan(trace) or "(no specialist note: nothing routed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
