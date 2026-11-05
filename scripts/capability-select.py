#!/usr/bin/env python3
"""Select capabilities for one current task through the existing model-router seam.

Prints a content-free report (selection v1 record, router labels, drops, evaluation
label) or, with --selection-only, just the selection for ``capability-plan.py
--selection``. Read-only: writes nothing, dispatches nothing, grants nothing.

Classifier (exactly one):
  --live      the configured model-router classifier from the router config
              (read-only; default $HERMES_HOME/config.yaml). Without its key or
              endpoint the outcome is "unconfigured" and nothing is selected.
              The capability seam runs in ai selection mode for this call.
  --fixture   a routing fixture-cases JSON (coordinator-authored raw answers keyed
              by exact task text). Offline evaluation only; labeled router-fixture.

Usage:
  python3 scripts/capability-select.py --task task.json --text-file prompt.txt --fixture cases.json
  python3 scripts/capability-select.py --task task.json --text-file - --live [--router-config CONFIG]

Exit: 0 selector ok, 4 selector failed/unconfigured/skipped (record still printed),
2 invalid input.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def _module(name: str, file: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return sys.modules[name]


SETTINGS = "plugins.entries.model-router.settings"


def _live_settings(cs, sp, config: Path):
    """RouterSettings from the router config (read-only), the way make specialist-classify-probe reads it."""
    if not config.is_file():
        raise cs.cr.RegistryError(f"{config}: router config not found")
    text = config.read_text(encoding="utf-8")
    lookup = _module("config_get_for_select", "config-get.py").lookup

    def getter(key, default):
        found, value = lookup(text, f"{SETTINGS}.{key}")
        return value if found and value is not None else default

    return cs.router_config(sp).load_settings(getter)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Current-task capability selection via the model-router seam (read-only)")
    ap.add_argument("--task", required=True, help="TaskEnvelope v1 JSON")
    ap.add_argument("--text-file", required=True, help="the current user turn as received, or - for stdin")
    how = ap.add_mutually_exclusive_group(required=True)
    how.add_argument("--live", action="store_true", help="use the configured live classifier")
    how.add_argument("--fixture", help="routing fixture-cases JSON (offline evaluation)")
    ap.add_argument("--router-config", help="router config for --live (default $HERMES_HOME/config.yaml)")
    ap.add_argument("--router-plugin", help="model-router plugin directory (default: this source tree's, "
                    "else $HERMES_HOME/plugins/model-router)")
    ap.add_argument("--profiles-root", help="installed profiles root for specialist readiness "
                    "(--fixture default: none, so no profile is eligible)")
    ap.add_argument("--selection-id", default="sel-1")
    ap.add_argument("--selection-only", action="store_true", help="print only the selection record")
    args = ap.parse_args(argv)
    cs = _module("capability_selection", "capability_selection.py")
    cr = cs.cr
    try:
        registry, lock = cr.load_registry()
        errs = cr.validate_registry(registry, lock)
        if errs:
            raise cr.RegistryError("the registry is invalid: " + "; ".join(errs))
        recipes = cs.load_recipes(registry)
        envelope = cr.load_json(Path(args.task))
        text = sys.stdin.read() if args.text_file == "-" else Path(args.text_file).read_text(encoding="utf-8")
        sp, hook = cs.load_router(Path(args.router_plugin) if args.router_plugin else None)
        if args.fixture:
            settings = sp.SpecialistSettings(enabled=True, classifier="cliproxy", selection_mode="ai",
                                             profiles_root=Path(args.profiles_root) if args.profiles_root
                                             else Path(os.devnull))
            classifier = cs.FixtureClassifier(cs.fixture_answers(cr.load_json(Path(args.fixture))), text, hook)
            kind, cloud = "fixture", False
        else:
            home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
            cfg = _live_settings(cs, sp, Path(args.router_config) if args.router_config else home / "config.yaml")
            settings = hook.specialist_settings(cfg)
            settings.enabled, settings.selection_mode = True, "ai"
            if args.profiles_root:
                settings.profiles_root = Path(args.profiles_root)
            if settings.classifier == "cliproxy":
                classifier, cloud = hook.cliproxy_classifier(cfg), True
            elif settings.classifier == "local":
                classifier, cloud = hook.local_classifier(cfg), False
            else:
                classifier, cloud = None, True
            kind = "live"
        report = cs.select(text, envelope, registry, sp=sp, settings=settings, classifier=classifier,
                           classifier_kind=kind, cloud=cloud, selection_id=args.selection_id, recipes=recipes)
    except (cr.RegistryError, OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"capability-select: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report["selection"] if args.selection_only else report, indent=2, sort_keys=True))
    return 0 if report["selection"]["selector"]["outcome"] == "ok" else 4


if __name__ == "__main__":
    sys.exit(main())
