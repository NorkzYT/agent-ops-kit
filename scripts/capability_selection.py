#!/usr/bin/env python3
"""Current-task capability selection through the EXISTING model-router seam (library).

One natural-language current task goes through the model-router plugin's own
``specialists.route_text`` (current-task extraction, runtime-envelope and status
gates, ``#privacy`` precedence, the ai-mode classifier and its closed catalog);
the registry's capability IDs ride the same single classifier call
(``route_text(capabilities=...)``). The validated closed IDs become a selection v1
record for ``capability-plan.py``. Nothing here classifies text itself: there is no
keyword fallback and no second model call. A failed, unconfigured or skipped
selector selects nothing.

Classifier provenance is explicit in ``selector.decision_ref``:
  router-live:<task hash>     the configured live classifier (cliproxy or local)
  router-fixture:<task hash>  a coordinator-authored fixture answer, offline; the
                              parser, gates, catalog checks and planner are real,
                              the "model reasoning" is not.

Read-only; no network unless the caller passes the live classifier.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
PLUGIN_DIR = REPO / "hermes" / "plugins" / "model-router"
RECIPES = REPO / "hermes" / "capabilities" / "routing-recipes.json"
CATALOG = REPO / "hermes" / "capabilities" / "routing-catalog.json"
# Files the routing catalog is generated from, relative to the kit/capability root. The
# model-router hook re-hashes them and offers nothing when any differs (drift).
CATALOG_SOURCES = ("hermes/capabilities/registry.json", "hermes/capabilities/routing-recipes.json")
CATALOG_KIND = "agent-ops-kit/capability-routing-catalog"
MODULE = "capability_selection_router"
CLASSIFIER_LABELS = {"live": "router-live", "fixture": "router-fixture"}
_HASH_RE = re.compile(r"^[0-9a-f]{16}$")


def registry_module():
    if "capability_registry" not in sys.modules:
        spec = importlib.util.spec_from_file_location("capability_registry", SCRIPTS / "capability_registry.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["capability_registry"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return sys.modules["capability_registry"]


cr = registry_module()


def default_plugin_dir() -> Path:
    """This source tree's model-router plugin; from an activated capability root (which ships no
    plugin) the installed one under ``$HERMES_HOME/plugins``."""
    if PLUGIN_DIR.is_dir():
        return PLUGIN_DIR
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    return home / "plugins" / "model-router"


def router_config(sp: Any) -> Any:
    """The ``router_config`` module of the plugin package ``sp`` was loaded from."""
    return importlib.import_module(sp.__name__.rsplit(".", 1)[0] + ".router_config")


def load_router(plugin_dir: Optional[Path] = None) -> Tuple[Any, Any]:
    """``(specialists, specialist_hook)`` from a model-router plugin directory, loaded the
    way Hermes loads plugins (package with relative imports). Import only: no hook is
    registered and nothing runs."""
    plugin_dir = Path(plugin_dir if plugin_dir is not None else default_plugin_dir()).resolve()
    name = f"{MODULE}_{hashlib.sha256(str(plugin_dir).encode()).hexdigest()[:12]}"
    if name not in sys.modules:
        init = plugin_dir / "__init__.py"
        if not init.is_file():
            raise cr.RegistryError(f"{plugin_dir}: not a model-router plugin directory")
        spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=[str(plugin_dir)])
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = name
        mod.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    sp = importlib.import_module(f"{name}.specialists")
    if not hasattr(sp, "capability_catalog"):
        raise cr.RegistryError(f"{plugin_dir}: model-router has no capability seam (needs the D source change)")
    return sp, importlib.import_module(f"{name}.specialist_hook")


# ---------------------------------------------------------------- catalog and recipes

def offered_catalog(registry: dict) -> Dict[str, str]:
    """``{id: one-line purpose}`` for every registry capability. Planned ones are offered
    with that label so a real need is visible; the planner then reports them blocked."""
    out: Dict[str, str] = {}
    for c in registry["capabilities"]:
        purpose = " ".join(c["purpose"].split())
        if c["lifecycle"] == "planned":
            purpose = "(planned, not implemented) " + purpose
        if len(purpose) > 300:
            purpose = purpose[:297].rsplit(" ", 1)[0] + " ..."
        out[c["id"]] = purpose
    return out


def load_recipes(registry: dict, path: Path = RECIPES) -> dict:
    data = cr.load_json(path)
    errs = cr.schema_errors(data, "routing-recipes.schema.json", "recipes") or recipe_errors(data, registry)
    if errs:
        raise cr.RegistryError("routing recipes invalid: " + "; ".join(errs))
    return data


def recipe_errors(recipes: dict, registry: dict) -> List[str]:
    known = {c["id"] for c in registry["capabilities"]}
    errs, seen = [], set()
    for r in recipes["recipes"]:
        if r["recipe_id"] in seen:
            errs.append(f"recipe {r['recipe_id']}: duplicate id")
        seen.add(r["recipe_id"])
        for cid in r["capability_ids"] + r["never"]:
            if cid not in known:
                errs.append(f"recipe {r['recipe_id']}: unknown capability {cid}")
        both = set(r["capability_ids"]) & set(r["never"])
        if both:
            errs.append(f"recipe {r['recipe_id']}: {sorted(both)} both selected and forbidden")
    return errs


def guidance(recipes: Optional[dict]) -> Optional[List[str]]:
    if not recipes:
        return None
    return [f"{r['outcome']}: {', '.join(r['capability_ids'])}" for r in recipes["recipes"]]


def catalog_document(registry: dict, recipes: dict, root: Path = REPO) -> dict:
    """The generated routing catalog the model-router ``pre_llm_call`` hook reads when the
    operator opts in (``specialist_capability_root`` + pinned SHA-256): the same offered
    IDs/purposes and recipe guidance as :func:`select`, each ID's skill (source-implemented
    only), and the SHA-256 of the files it came from. Data only: no ID grants anything."""
    caps = []
    offered = offered_catalog(registry)
    for c in registry["capabilities"]:
        skill = c.get("exposed_skill_name") if c["lifecycle"] == "source-implemented" else None
        caps.append({"id": c["id"], "purpose": offered[c["id"]], "lifecycle": c["lifecycle"], "skill": skill})
    return {"schema_version": 1, "kind": CATALOG_KIND,
            "sources": {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in CATALOG_SOURCES},
            "capabilities": caps, "guidance": guidance(recipes) or []}


def catalog_bytes(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


# ---------------------------------------------------------------- classifiers

class FixtureClassifier:
    """Offline stand-in for the model call: returns the coordinator-authored raw answer
    recorded for this exact task text (sha256), parsed by the real hook parser and caps.
    A missing answer is a classifier failure, never a guess."""

    def __init__(self, answers: Mapping[str, str], raw_text: str, hook: Any) -> None:
        self.answer = answers.get(hashlib.sha256(raw_text.encode("utf-8")).hexdigest())
        self.hook = hook
        self.calls = 0

    def __call__(self, text: Any, pool: Sequence[str]) -> List[str]:
        self.calls += 1
        meta = {"model": "fixture", "effort": "-", "attempt": 1}
        if self.answer is None:
            return self.hook.Picks([], dict(meta, outcome="unavailable:fixture-miss"))
        return self.hook.Picks(self.hook.cap_names(self.hook.answer_names(self.answer, pool), text),
                               dict(meta, outcome="ok"))


def fixture_answers(fixture: dict) -> Dict[str, str]:
    """``{sha256(task_text): raw answer}`` from a routing fixture-cases file."""
    if not isinstance(fixture, dict) or fixture.get("schema_version") != 1 or not isinstance(fixture.get("cases"), list):
        raise cr.RegistryError("fixture: expected {schema_version: 1, cases: [...]}")
    out = {}
    for c in fixture["cases"]:
        if not isinstance(c, dict) or not isinstance(c.get("task_text"), str) or not isinstance(c.get("fixture_answer"), str):
            raise cr.RegistryError("fixture: each case needs task_text and fixture_answer strings")
        out[hashlib.sha256(c["task_text"].encode("utf-8")).hexdigest()] = c["fixture_answer"]
    return out


# ---------------------------------------------------------------- the seam

def select(text: str, envelope: Any, registry: dict, *, sp: Any, settings: Any,
           classifier: Optional[Callable[..., Any]], classifier_kind: str, cloud: bool,
           selection_id: str, recipes: Optional[dict] = None,
           readiness: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Route ``text`` (the current user turn) through the model-router seam and return
    ``{"selection", "router", "dropped", "evaluation"}``. ``classifier_kind`` is ``live`` or
    ``fixture``; ``cloud`` says whether the classifier sends text off-host.

    Gates before any call: the envelope must validate, and a company-private or
    egress_class=deny envelope never reaches a cloud classifier (the router's own #privacy gate also applies). The router's
    trace is content-free; it is returned trimmed to routing labels."""
    if classifier_kind not in CLASSIFIER_LABELS:
        raise cr.RegistryError(f"classifier kind must be one of {sorted(CLASSIFIER_LABELS)}")
    errs = cr.schema_errors(envelope, "task-envelope.schema.json", "task")
    if errs:
        raise cr.RegistryError("; ".join(errs))
    catalog = offered_catalog(registry)
    # Fail closed: a cloud classifier sends the task text off-host, so it needs both a
    # non-private data class and an envelope that permits egress (egress_class != deny).
    blocked = ("data-class:company-private" if envelope["data_classification"] == "company-private"
               else "egress:deny" if envelope["egress_class"] == "deny" else None)
    if cloud and blocked and classifier is not None:
        trace = {"capability_selection": {"outcome": "skipped", "reason": blocked,
                                          "offered": len(catalog), "picks": [], "dropped": []},
                 "classifier": "skipped:" + blocked.split(":")[0], "routes": [], "dropped": [], "task": "-"}
    else:
        kw = {"readiness": readiness} if readiness is not None else {}
        trace = sp.route_text(text, settings, classifier=classifier, capabilities=catalog,
                              capability_guidance=guidance(recipes), **kw)
    cs = trace["capability_selection"]
    task_hash = trace.get("task") if _HASH_RE.match(str(trace.get("task", ""))) else None
    ref = f"{CLASSIFIER_LABELS[classifier_kind]}:{task_hash or 'no-task'}"
    selection, dropped = cr.selection_from_picks(envelope["task_id"], selection_id, cs["picks"], registry=registry,
                                                 selector_kind="model-router", outcome=cs["outcome"],
                                                 eligible=list(catalog), max_picks=sp.MAX_CAPABILITY_PICKS,
                                                 decision_ref=ref)
    if task_hash:
        selection["task_hash"] = task_hash
    routes = [{"profile": r["profile"], "execution": r["execution"], "advisory_only": bool(r["advisory_only"]),
               "knowledge_status": r["knowledge"]["status"]} for r in trace.get("routes") or []]
    if routes:
        selection["specialist_routes"] = routes
    errs = cr.schema_errors(selection, "selection.schema.json", "selection")
    if errs:
        raise cr.RegistryError("; ".join(errs))
    router = {k: trace[k] for k in ("task", "privacy", "privacy_reasons", "tier", "selection_mode", "classifier",
                                    "classifier_detail", "reason", "task_source", "capability_selection")
              if k in trace}
    router["routes"] = [r["profile"] for r in routes]
    router["dropped_profiles"] = trace.get("dropped") or []
    evaluation = ("FIXTURE classifier answer (offline, coordinator-authored): parser, router gates, catalog and "
                  "planner are real; no model reasoning or live natural-language selection was exercised"
                  if classifier_kind == "fixture" else
                  "live model-router classifier" + (" (cloud)" if cloud else " (local)"))
    return {"selection": selection, "router": router, "dropped": cs["dropped"] + dropped, "evaluation": evaluation}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``catalog`` prints the routing catalog; ``check`` exits 1 when the committed one drifted.
    Both print its SHA-256 (the value to pin as ``specialist_capability_catalog_sha256``) on stderr."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["catalog"], ["check"]):
        print("usage: capability_selection.py catalog|check", file=sys.stderr)
        return 2
    try:
        registry, lock = cr.load_registry()
        errs = cr.validate_registry(registry, lock)
        if errs:
            raise cr.RegistryError("the registry is invalid: " + "; ".join(errs))
        data = catalog_bytes(catalog_document(registry, load_recipes(registry)))
    except (cr.RegistryError, OSError, ValueError) as exc:
        print(f"capability_selection: {exc}", file=sys.stderr)
        return 2
    print(f"sha256 {hashlib.sha256(data).hexdigest()}", file=sys.stderr)
    if args == ["catalog"]:
        sys.stdout.write(data.decode("ascii"))
        return 0
    if not CATALOG.is_file() or CATALOG.read_bytes() != data:
        print(f"capability_selection: {CATALOG.relative_to(REPO)} is stale; regenerate with "
              "`python3 scripts/capability_selection.py catalog > hermes/capabilities/routing-catalog.json`",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
