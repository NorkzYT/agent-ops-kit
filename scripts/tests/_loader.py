"""Load the in-repo `model-router` plugin package the way Hermes loads it.

Hermes imports a plugin directory via `spec_from_file_location` with
`submodule_search_locations` set, so the package uses relative imports. We mirror
that here under a valid module name so the plugin can be unit-tested with plain
`python3 -m unittest` and no Hermes runtime.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "hermes" / "plugins" / "model-router"
MODULE_NAME = "model_router_under_test"


def load_plugin() -> types.ModuleType:
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    init = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, init, submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = MODULE_NAME
    module.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def submodule(name: str) -> types.ModuleType:
    load_plugin()
    return importlib.import_module(f"{MODULE_NAME}.{name}")
