#!/usr/bin/env python3
"""local-route — status, model catalog, classifier probe and readback for the
local (Ollama) model roles of the model-router plugin.

  status    Read-only. Live plugin settings (local roles, privacy, specialist
            knobs), the native fallback chains and auxiliary routes as Hermes
            resolves them, and whether each role model exists on the endpoint.
  catalog   Read-only. Models on the local endpoint: capabilities (tools,
            thinking), size, quantization, which one is loaded, which role uses it.
  probe     Dry-run of the specialist router (live mode: ai by default) for one task with the LIVE
            classifier (local, or --classifier cliproxy). Mode ai: the
            classifier selects from the eligible installed catalog; hybrid:
            keywords first, classifier only for weak/no matches (--mode
            overrides the live mode). Prints the content-free trace (no task
            text) and the classifier model/outcome. Nothing is dispatched or written.
            --no-call shows the gate without calling.
  readback  Renders what the installer would write now (hermes-install.sh
            --render-only, temp file) and compares it with the live config as
            Hermes resolves both (hermes-native-check.py in the Hermes venv),
            then checks the local-only invariants (an auxiliary task left on
            provider auto must follow the main CLIProxyAPI route). Exit 1 on
            any difference.

Never reads secrets, never prints private_terms/private_paths, never restarts
anything. Metadata calls (/v1/models, /api/tags, /api/ps, /api/show) go only to
the configured local endpoint; probe sends the task text only to that endpoint
(or, with --classifier cliproxy, to CLIProxyAPI using CLIPROXY_API_KEY from the
shell environment) and only when the router allows the classifier.

Environment: HERMES_HOME (default ~/.hermes), HERMES_AGENT_DIR (default
$HERMES_HOME/hermes-agent), ENV_FILE (default .env, read for readback only
through the installer).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
SETTINGS = "plugins.entries.model-router.settings"
ROLES = ("routine", "fallback", "agent")
COMPARE = ("main", "fallback_providers", "delegation_fallback_providers", "auxiliary",
           "auxiliary_allow_cloud_fallback", "plugins_enabled", "model_router")


def out(msg: str = "") -> None:
    print(msg, flush=True)


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def plugin(name: str):
    return _script("specialist-route")._load(name)


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def agent_python(home: Path) -> Optional[Path]:
    agent = Path(os.environ.get("HERMES_AGENT_DIR") or home / "hermes-agent")
    py = agent / "venv" / "bin" / "python"
    return py if py.exists() else None


# --------------------------------------------------------------------------- #
# Config reading
# --------------------------------------------------------------------------- #
def native(config: Path, home: Path) -> Optional[Dict[str, Any]]:
    """What Hermes resolves from ``config`` (None when the Hermes venv is absent)."""
    py = agent_python(home)
    if py is None:
        return None
    env = dict(os.environ, HERMES_AGENT_DIR=str(py.parents[2]))
    r = subprocess.run([str(py), str(REPO / "scripts" / "hermes-native-check.py"), "--config", str(config)],
                       capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        raise SystemExit(f"error: hermes-native-check failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return json.loads(r.stdout)


def setting(config: Path, key: str) -> Optional[str]:
    found, value = _script("config-get").lookup(config.read_text(encoding="utf-8"), f"{SETTINGS}.{key}")
    return value if found else None


def router_settings(config: Path):
    """RouterSettings from the live plugin settings (env fallback for unset keys)."""
    rc = plugin("router_config")
    text = config.read_text(encoding="utf-8")
    lookup = _script("config-get").lookup

    def getter(key: str, default: Any) -> Any:
        found, value = lookup(text, f"{SETTINGS}.{key}")
        return value if found and value is not None else default

    return rc.load_settings(getter)


# --------------------------------------------------------------------------- #
# Local endpoint metadata (read-only)
# --------------------------------------------------------------------------- #
def _get(url: str, body: Optional[dict] = None, timeout: float = 10.0) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:  # nosec B310 - configured local endpoint
        return json.loads(fh.read().decode("utf-8"))


def catalog_of(base_url: str) -> Tuple[Optional[Dict[str, Dict[str, Any]]], str]:
    """{model: info} from the endpoint, or (None, error label)."""
    root = base_url.rstrip("/")
    native_root = root[:-3] if root.endswith("/v1") else root
    try:
        ids = [m["id"] for m in _get(f"{root}/models").get("data", [])]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        return None, type(exc).__name__
    models: Dict[str, Dict[str, Any]] = {i: {"loaded": False} for i in ids}
    try:
        for m in _get(f"{native_root}/api/ps").get("models", []):
            models.setdefault(m.get("name"), {})["loaded"] = True
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        pass
    for name, info in models.items():
        try:
            show = _get(f"{native_root}/api/show", {"model": name})
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            continue
        details = show.get("details") or {}
        ctx = next((v for k, v in (show.get("model_info") or {}).items() if k.endswith(".context_length")), None)
        info.update(capabilities=show.get("capabilities") or [], size=details.get("parameter_size"),
                    quant=details.get("quantization_level"), context=ctx)
    return models, ""


def role_models(cfg) -> Dict[str, str]:
    return {r: cfg.local_model(r) for r in ROLES}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _route_line(entries: List[Dict[str, Any]]) -> str:
    return " -> ".join(f"{e.get('model')}@{urlsplit(e.get('base_url') or '').netloc or e.get('provider')}"
                       for e in entries) or "(none)"


def _require_config(home: Path) -> Path:
    cfg = home / "config.yaml"
    if not cfg.is_file():
        raise SystemExit(f"error: {cfg} not found (run: make hermes-install)")
    return cfg


def status(args) -> int:
    home = hermes_home()
    config = _require_config(home)
    lb = plugin("local_backend")
    cfg = router_settings(config)
    out(f"local route status  (HERMES_HOME={home})")
    out(f"  privacy_routing : {cfg.privacy_routing}"
        + ("" if cfg.privacy_routing else "   WARN: #privacy turns are NOT kept local "
           "(set MODEL_ROUTER_PRIVACY=true)"))
    url = cfg.ollama_base_url
    out(f"  local endpoint  : {url or '(unset)'}  local={'yes' if lb.is_local_endpoint(url) else 'NO'}")
    models, err = catalog_of(url) if url else (None, "unset")
    out(f"  reachable       : " + (f"yes, {len(models)} models" if models is not None else f"no ({err})"))
    for role, model in role_models(cfg).items():
        info = (models or {}).get(model)
        state = ("not configured" if not model else "unknown" if models is None else
                 "MISSING on endpoint" if info is None else
                 ", ".join(filter(None, ["present", "tools" if "tools" in info.get("capabilities", []) else
                                         "NO tools", "loaded" if info.get("loaded") else ""])))
        out(f"  role {role:<10} : {model or '(none)'}  [{state}]")
    nat = native(config, home)
    if nat is None:
        out("  native routes   : Hermes venv not found; fallback chains not resolved")
    else:
        out(f"  fallback chain  : {_route_line(nat['fallback_providers'])}")
        out(f"  delegation      : {_route_line(nat['delegation_fallback_providers'])}")
        for task, r in nat["auxiliary"].items():
            where = "MAIN provider (cloud)" if r["provider"] == "auto" else r["base_url"]
            out(f"  aux {task:<16}: {r['provider']} {r['model'] or '(main model)'} @ {where}"
                + (f"; fallback {_route_line(r['fallback_chain'])}" if r["fallback_chain"] else ""))
    out(f"  specialists     : routing={cfg.specialist_routing} mode={cfg.specialist_selection_mode} "
        f"max={cfg.specialist_max_profiles} classifier={cfg.specialist_classifier} "
        f"timeout={cfg.specialist_classifier_timeout:g}s")
    loaded = [m for m, i in (models or {}).items() if i.get("loaded")]
    if loaded:
        out(f"  loaded now      : {', '.join(loaded)}  (switching to another 27b role reloads it)")
    return 0


def catalog(args) -> int:
    home = hermes_home()
    url = args.base_url
    cfg = None
    if not url:
        config = _require_config(home)
        cfg = router_settings(config)
        url = cfg.ollama_base_url
    if not url:
        raise SystemExit("error: no local endpoint (ollama_base_url unset; pass --base-url)")
    models, err = catalog_of(url)
    if models is None:
        out(f"{url}: unreachable ({err})")
        return 1
    roles: Dict[str, List[str]] = {}
    for role, model in (role_models(cfg) if cfg else {}).items():
        roles.setdefault(model, []).append(role)
    if args.json:
        print(json.dumps({"endpoint": url, "models": models, "roles": roles}, indent=2, sort_keys=True))
        return 0
    out(f"local models at {url}")
    out(f"  {'model':<24} {'size':<7} {'quant':<8} {'ctx':<7} {'loaded':<6} {'role':<16} capabilities")
    for name in sorted(models):
        i = models[name]
        out(f"  {name:<24} {str(i.get('size') or '-'):<7} {str(i.get('quant') or '-'):<8} "
            f"{str(i.get('context') or '-'):<7} {'yes' if i.get('loaded') else '':<6} "
            f"{','.join(roles.get(name, [])) or '-':<16} {','.join(i.get('capabilities', [])) or '-'}")
    for model, rs in roles.items():
        if model and model not in models:
            out(f"  MISSING: {model} ({','.join(rs)}) is not on the endpoint")
    return 0


def probe(args) -> int:
    text = args.task if args.task is not None else os.environ.get("TASK", "")
    if text == "-":
        text = sys.stdin.read()
    if not text.strip():
        out('usage: make specialist-classify-probe TASK="..." [NO_CALL=1]   (dry-run; nothing dispatched)')
        return 0
    home = hermes_home()
    cfg = router_settings(_require_config(home))
    # Pre-activation: try the planned role models without touching the config.
    if args.model:
        cfg.ollama_routine_model = args.model
    if args.fallback_model is not None:
        cfg.ollama_fallback_model = args.fallback_model
    if args.timeout:
        cfg.specialist_classifier_timeout = args.timeout
    kind = args.classifier or ("cliproxy" if cfg.specialist_classifier == "cliproxy" else "local")
    sp = plugin("specialists")
    hook = plugin("specialist_hook")
    if args.mode:
        cfg.specialist_selection_mode = args.mode
    settings = hook.specialist_settings(cfg)
    settings.enabled, settings.classifier = True, kind
    if args.no_call:
        classifier = lambda visible, pool: hook.Picks([], {"model": "-", "attempt": 0, "outcome": "not-called"})  # noqa: E731
    elif kind == "cliproxy":
        classifier = hook.cliproxy_classifier(cfg)
    else:
        classifier = hook.local_classifier(cfg)
    started = time.monotonic()
    trace = sp.route_text(text, settings, classifier=classifier)
    elapsed = int((time.monotonic() - started) * 1000)
    trace["classifier_ready"] = classifier is not None
    trace["live_specialist_routing"] = cfg.specialist_routing
    trace["live_selection_mode"] = cfg.specialist_selection_mode
    trace["live_classifier"] = cfg.specialist_classifier
    trace["local_models"] = role_models(cfg)
    trace["elapsed_ms"] = elapsed
    print(json.dumps(trace, indent=2, sort_keys=True))
    detail = trace.get("classifier_detail") or {}
    out(f"--- summary ---\ntask {trace.get('task')}  mode {trace.get('selection_mode')}  privacy {trace.get('privacy')}  "
        f"gate {trace.get('classifier_gate', '-')}  classifier {trace.get('classifier')}  "
        f"model {detail.get('model', '-')}  outcome {detail.get('outcome', '-')}  {elapsed} ms")
    out("routes: " + (", ".join(f"{r['profile']}({','.join(r['reasons'])})" for r in trace["routes"]) or "none"))
    if not classifier:
        out("classifier unavailable: " + ("needs CLIPROXY_API_KEY in this shell's environment"
                                          if kind == "cliproxy" else
                                          "needs ollama_base_url on a local endpoint and a routine model"))
    return 0


def _follows_main(r: Dict[str, Any]) -> bool:
    """provider auto with no model/base_url: Hermes uses the main provider and
    model (an empty YAML model comes back as the string "None")."""
    return r["provider"] == "auto" and r["model"] in (None, "", "None") and not r["base_url"]


def _invariants(nat: Dict[str, Any], lb, cliproxy_url: str) -> List[str]:
    """Local-only invariants. ``cliproxy_url`` is the configured CLIProxyAPI base
    URL (the main route the installer renders): an auxiliary task that follows
    the main provider is intentional only when the main route is that proxy."""
    bad = []
    mr = nat.get("model_router") or {}
    url = str(mr.get("ollama_base_url") or "")
    if url and not lb.is_local_endpoint(url):
        bad.append(f"ollama_base_url {url} is not a local endpoint")
    for r in nat["fallback_providers"][1:] + nat["delegation_fallback_providers"]:
        if r["base_url"] and ":3456" not in r["base_url"] and not lb.is_local_endpoint(r["base_url"]):
            bad.append(f"fallback hop {r['model']}@{r['base_url']} is not local")
    main = nat.get("main") or {}
    for task, r in nat["auxiliary"].items():
        if _follows_main(r):
            if main.get("provider") != "custom" or not cliproxy_url or \
                    (main.get("base_url") or "").rstrip("/") != cliproxy_url.rstrip("/"):
                bad.append(f"auxiliary.{task}: follows the main provider {main.get('provider')} "
                           f"@ {main.get('base_url') or '(none)'}, not the configured CLIProxyAPI {cliproxy_url}")
        elif r["provider"] == "auto" and r["model"]:
            bad.append(f"auxiliary.{task}: provider auto with model {r['model']} resolves to the MAIN "
                       "(cloud) provider, not the local endpoint")
        for f in r["fallback_chain"]:
            if f["base_url"] and not lb.is_local_endpoint(f["base_url"]):
                bad.append(f"auxiliary.{task}.fallback_chain hop {f['model']} is not local")
    if "model-router" not in nat.get("plugins_enabled", []):
        bad.append("model-router is not in plugins.enabled")
    return bad


def readback(args) -> int:
    home = hermes_home()
    config = _require_config(home)
    lb = plugin("local_backend")
    live = native(config, home)
    if live is None:
        raise SystemExit("error: Hermes venv not found ($HERMES_AGENT_DIR/venv/bin/python); "
                         "readback needs Hermes' own resolver")
    with tempfile.TemporaryDirectory() as tmp:
        expected_cfg = Path(tmp) / "config.yaml"
        r = subprocess.run(["bash", str(REPO / "scripts" / "hermes-install.sh"), "--render-only", str(expected_cfg)],
                           cwd=REPO, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise SystemExit(f"error: render failed: {(r.stderr or r.stdout).strip()[-400:]}")
        expected = native(expected_cfg, home)
    out(f"local route readback  (live {config} vs. what hermes-install.sh --force renders now)")
    diffs = 0
    for key in COMPARE:
        same = live.get(key) == expected.get(key)
        diffs += not same
        out(f"  {'ok  ' if same else 'DIFF'} {key}")
        if not same:
            out(f"       live    : {json.dumps(live.get(key), sort_keys=True)}")
            out(f"       expected: {json.dumps(expected.get(key), sort_keys=True)}")
    bad = _invariants(live, lb, (expected.get("main") or {}).get("base_url") or "")
    main = live.get("main") or {}
    for task, r in live["auxiliary"].items():
        if _follows_main(r) and not any(b.startswith(f"auxiliary.{task}:") for b in bad):
            out(f"  ok   auxiliary.{task} follows main {main.get('model')} @ "
                f"{urlsplit(main.get('base_url') or '').netloc} (CLIProxyAPI, cloud by design)")
    for b in bad:
        out(f"  FAIL invariant: {b}")
    mr = live.get("model_router") or {}
    if mr.get("privacy_routing") is False:
        out("  WARN privacy_routing is false: private turns are NOT kept local (MODEL_ROUTER_PRIVACY=true)")
    if diffs or bad:
        out(f"  result          : {diffs} difference(s), {len(bad)} invariant failure(s)")
        if diffs:
            out("  next            : HERMES_SKIP_GATEWAY=1 bash scripts/hermes-install.sh --force, "
                "then make hermes-restart from your own terminal")
        return 1
    out("  result          : live config matches the render; local-only invariants hold")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Local model roles: status, catalog, probe, readback")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    c = sub.add_parser("catalog")
    c.add_argument("--base-url", default="")
    c.add_argument("--json", action="store_true")
    p = sub.add_parser("probe")
    p.add_argument("task", nargs="?", default=None, help="task text, - for stdin (default $TASK)")
    p.add_argument("--classifier", choices=("local", "cliproxy"), default="",
                   help="classifier to probe (default: cliproxy when that is live, else local)")
    p.add_argument("--no-call", action="store_true", help="show the gate; do not call the classifier")
    p.add_argument("--model", default="", help="routine model for this probe only")
    p.add_argument("--fallback-model", default=None, help="fallback model for this probe only ('' = none)")
    p.add_argument("--timeout", type=float, default=0.0, help="classifier budget in seconds for this probe")
    p.add_argument("--mode", choices=("ai", "hybrid", "keywords"), default="",
                   help="selection mode for this probe (default: the live specialist_selection_mode)")
    sub.add_parser("readback")
    args = ap.parse_args(argv)
    return {"status": status, "catalog": catalog, "probe": probe, "readback": readback}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
