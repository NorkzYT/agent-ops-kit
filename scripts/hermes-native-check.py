#!/usr/bin/env python3
"""hermes-native-check — what Hermes itself resolves from a config.yaml.

Run with the Hermes venv python (it imports Hermes' own modules):

  ~/.hermes/hermes-agent/venv/bin/python scripts/hermes-native-check.py --config FILE

Prints JSON: the orchestrator and delegation fallback chains exactly as
Hermes normalizes them (``hermes_cli.fallback_config.get_fallback_chain``),
each auxiliary task's resolved (provider, model, base_url) from
``agent.auxiliary_client._resolve_task_provider_model`` plus its
``fallback_chain``, ``auxiliary.allow_cloud_fallback``, ``plugins.enabled``
and the model-router routing settings (``private_terms``/``private_paths``
are never printed).

Read-only: the config is parsed with PyYAML and handed to Hermes through a
patched ``load_config_readonly``; HERMES_HOME points at a throwaway directory
while Hermes modules are imported, so nothing under the real home is touched
and no provider is called. Environment: HERMES_AGENT_DIR (default
~/.hermes/hermes-agent).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

AUX_TASKS = ("title_generation", "compression")
ROUTER_KEYS = (
    "complexity_routing", "privacy_routing", "effort_routing", "astra_policy",
    "ollama_base_url", "ollama_model", "ollama_routine_model", "ollama_fallback_model",
    "ollama_agent_model", "specialist_routing", "specialist_max_profiles",
    "specialist_require_ready", "specialist_classifier", "specialist_classifier_timeout",
    "specialist_profiles",
)


def _route(entry: dict) -> dict:
    return {"provider": entry.get("provider"), "model": entry.get("model"),
            "base_url": entry.get("base_url") or ""}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    agent_dir = Path(os.environ.get("HERMES_AGENT_DIR") or Path.home() / ".hermes" / "hermes-agent")
    sys.path.insert(0, str(agent_dir))
    import yaml  # Hermes venv dependency

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["HERMES_HOME"] = scratch
        from hermes_cli import config as hconfig
        from hermes_cli.fallback_config import get_fallback_chain

        hconfig.load_config_readonly = lambda: cfg  # type: ignore[assignment]
        from agent import auxiliary_client as aux

        out = {
            "fallback_providers": [_route(e) for e in get_fallback_chain(cfg)],
            "delegation_fallback_providers": [
                _route(e) for e in get_fallback_chain(
                    {"fallback_providers": (cfg.get("delegation") or {}).get("fallback_providers")})],
            "auxiliary": {},
            "auxiliary_allow_cloud_fallback": bool((cfg.get("auxiliary") or {}).get("allow_cloud_fallback")),
            "plugins_enabled": list((cfg.get("plugins") or {}).get("enabled") or []),
        }
        for task in AUX_TASKS:
            provider, model, base_url, _key, _mode = aux._resolve_task_provider_model(task)
            chain = ((cfg.get("auxiliary") or {}).get(task) or {}).get("fallback_chain") or []
            out["auxiliary"][task] = {
                "provider": provider, "model": model, "base_url": base_url or "",
                "fallback_chain": [_route(e) for e in chain if isinstance(e, dict)],
            }
    settings = (((cfg.get("plugins") or {}).get("entries") or {}).get("model-router") or {}).get("settings") or {}
    out["model_router"] = {k: settings.get(k) for k in ROUTER_KEYS if k in settings}
    out["hosts"] = sorted({urlsplit(r["base_url"]).netloc for r in
                           out["fallback_providers"] + out["delegation_fallback_providers"] if r["base_url"]})
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
