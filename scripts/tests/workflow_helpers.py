"""Shared helpers for the capability workflow tests (synthetic evals, temp dirs, offline)."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
EVALS = REPO / "hermes" / "capabilities" / "evals"


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


cr = _load("capability_registry")
wf = _load("capability_workflow")
REG, LOCK = cr.load_registry()


def scenario(name: str, analysis: str):
    """(analysis dict, Pack) for one eval scenario."""
    d = EVALS / name
    pack, errs = wf.load_pack(d / "evidence-pack.json")
    assert not errs, errs
    return cr.load_json(d / f"{analysis}.json"), pack


def ctx(pack=None, store=None):
    c = wf.Context(registry=REG, pack=pack, store=store, extra={})
    if pack is not None:
        c.extra["pack_raw"] = (pack.root / "evidence-pack.json").read_bytes()
    return c


def errors(a, pack=None, store=None):
    return wf.validate_analysis(a, ctx(pack, store))[1]


def statement(a, sid):
    return next(s for s in a["statements"] if s["id"] == sid)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def render(self, a, pack=None, store=None, name="out"):
        c = ctx(pack, store)
        files, errs = wf.render_analysis(a, c)
        self.assertEqual(errs, [])
        raw = (json.dumps(a, indent=2) + "\n").encode()
        out = wf.build_outputs(a, c, files, raw, run_ref="run:test")
        wf.write_fresh_dir(self.tmp / name, out)
        return self.tmp / name

    def assert_error(self, errs, pattern):
        self.assertTrue(any(re.search(pattern, e) for e in errs), f"no error matching {pattern!r} in {errs}")

    def run_cli(self, *args, env_extra=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(env_extra or {})
        return subprocess.run([sys.executable, *map(str, args)], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)


def mutate(a, fn):
    b = copy.deepcopy(a)
    fn(b)
    return b


def repack(tmp: Path, scenario_dir: Path, a: dict, fn):
    """Copy a scenario's evidence pack (and captured files) into ``tmp``, let ``fn`` edit the pack dict,
    and return (analysis bound to the new pack digest, loaded Pack)."""
    import hashlib
    import shutil
    dst = tmp / f"pack-{len(list(tmp.glob('pack-*')))}"
    shutil.copytree(scenario_dir, dst, ignore=shutil.ignore_patterns("inputs"))
    doc = json.loads((dst / "evidence-pack.json").read_text())
    fn(doc)
    raw = (json.dumps(doc, indent=2) + "\n").encode()
    (dst / "evidence-pack.json").write_bytes(raw)
    pack, errs = wf.load_pack(dst / "evidence-pack.json")
    assert not errs, errs
    b = copy.deepcopy(a)
    b["evidence"]["digest"] = hashlib.sha256(raw).hexdigest()
    return b, pack
