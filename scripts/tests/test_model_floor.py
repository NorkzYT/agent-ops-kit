"""GPT model floor (6.1+, Terra or higher): shared rule parity, script gates and
the existing-install migration (scripts/model-floor-migrate.py).

Run: python3 -m unittest discover -s scripts/tests -p 'test_model_floor.py'

Offline: every Hermes home here is a throwaway directory; the synthetic key
values below are not credentials. No model, proxy or network is reached.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _loader import submodule  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MIGRATE = REPO / "scripts" / "model-floor-migrate.py"
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import model_floor as mf  # noqa: E402

cx = submodule("complexity")

SECRET = "synthetic-not-a-key-7f3a"
CORPUS = ["gpt-6.1-terra", "gpt-6.1-sol", "gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-astra", "gpt-7-terra",
          "gpt-6.10-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra", "gpt-6.0-sol",
          "gpt-6.1-luna", "gpt-6-1-terra", "GPT-6.1-TERRA", "gpt-6.1-terra-preview", "openai/gpt-6.1-terra",
          "gpt-4o", "luna", "terra", "sol", "astra", "opus", "fable", "qwen3.5:27b", "", " gpt-6.1-terra",
          "openrouter/openai/gpt-6.2-sol", "openrouter/openai/gpt-5.6-sol", "openrouter/openai/gpt-5.4-mini",
          "openai:gpt-5.6-sol", "azure:gpt-6.1-terra", "azure-gpt-4o", "azure/my-gpt-4o-deploy",
          "gpt-6.1-terra:latest", "gpt-latest", "chatgpt-4o-latest", "gpt-oss:20b", "ollama/gpt-oss:20b",
          "ollama/luna", "gpt-j-6b", "solar-10.7b", "//gpt-5.6-sol"]


def _scratch() -> Path:
    base = os.environ.get("TMPDIR") or "/tmp"
    return Path(tempfile.mkdtemp(prefix="model-floor-", dir=base))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(*args, check=True):
    r = subprocess.run([sys.executable, "-I", str(MIGRATE), *args], capture_output=True, text=True,
                       timeout=60, cwd=REPO)
    if check and r.returncode not in (0,):
        raise AssertionError(f"rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


STALE_CONFIG = f"""# rendered by an older kit
model:
  provider: custom
  default: gpt-5.6-luna   # starting model
  base_url: http://127.0.0.1:8317/v1
  api_key: ${{CLIPROXY_API_KEY}}

fallback_providers:
  - provider: custom
    model: opus
    base_url: http://127.0.0.1:3456/v1
    key_env: CLAUDE_MAX_PROXY_API_KEY
  - provider: custom
    model: gpt-5.6-sol
    base_url: http://127.0.0.1:8317/v1
    key_env: CLIPROXY_API_KEY
  - provider: custom
    model: qwen3.8:27b
    base_url: http://127.0.0.1:11434/v1

delegation:
  provider: custom
  model: opus
  fallback_providers:
  - provider: custom
    model: "gpt-6-astra"
    base_url: http://127.0.0.1:8317/v1

auxiliary:
  allow_cloud_fallback: false
  title_generation:
    provider: auto
    model: gpt-5.6-luna
    base_url: ''
    reasoning_effort: low
    fallback_chain: []
  compression:
    provider: custom
    model: gpt-6.1-terra
    base_url: http://127.0.0.1:8317/v1
    api_key: "{SECRET}"
    reasoning_effort: low
    fallback_chain: [{{provider: custom, model: "gpt-5.6-luna", base_url: "http://127.0.0.1:8317/v1", key_env: CLIPROXY_API_KEY}}]

plugins:
  enabled:
    - model-router
  entries:
    model-router:
      settings:
        complexity_routing: true
        astra_policy: task
        specialist_classifier_model: gpt-5.6-sol
        available_gpt_models: [gpt-5.6-luna, gpt-6.1-terra, gpt-6-astra, gpt-6.2-sol]
        ollama_routine_model: qwen3.5:27b
"""

CLEAN_CONFIG = """model:
  provider: custom
  default: gpt-6.1-terra
  base_url: http://127.0.0.1:8317/v1
plugins:
  entries:
    model-router:
      settings:
        available_gpt_models:
        - gpt-6.1-terra
        - gpt-6.2-sol
"""

STALE_ENV = f"""DISCORD_BOT_TOKEN={SECRET}
CLIPROXY_API_KEY={SECRET}
export HERMES_MODEL="gpt-5.6-luna"
HONCHO_MODEL=gpt-5.6-terra
AUX_COMPRESSION_MODEL=gpt-5.6-terra # pre-floor
AUX_COMPRESSION_FALLBACK_MODEL=gpt-5.6-luna
MODEL_ROUTER_SPECIALIST_CLASSIFIER_MODEL=gpt-5.6-sol
MODEL_ROUTER_GPT_MODELS=gpt-5.6-luna,gpt-6.1-terra,gpt-6.2-sol
HERMES_CODING_MODEL=opus
PRIVATE_HONCHO_MODEL=qwen3.5:27b
"""


class RuleParity(unittest.TestCase):
    def test_script_and_plugin_rules_agree(self):
        for model in CORPUS:
            with self.subTest(model=model):
                self.assertEqual(mf.eligible(model), cx.gpt_model_eligible(model))
                self.assertEqual(mf.below_floor(model), cx.gpt_below_floor(model))
        self.assertEqual(mf.MIN_VERSION, cx.GPT_MIN_VERSION)
        self.assertEqual(mf.ELIGIBLE_FAMILIES, cx.GPT_ELIGIBLE_FAMILIES)
        self.assertEqual((mf.DEFAULT_MODEL, mf.STATIC_SOL, mf.STATIC_ASTRA),
                         (cx.GPT_TIER_MODELS["medium"], cx.GPT_TIER_MODELS["high"], cx.GPT_TIER_MODELS["max"]))

    def test_windows_worker_guard_matches_rule(self):
        ps1 = (REPO / "scripts/windows-vm/install-worker.ps1").read_text(encoding="utf-8-sig")
        pattern = re.search(r"\[regex\]::Match\(\$Model, '([^']+)'\)", ps1).group(1)
        for model in CORPUS:
            m = re.match(pattern, model)
            ok = bool(m) and (int(m.group(1)), int(m.group(2) or 0)) >= (6, 1)
            self.assertEqual(ok, mf.eligible(model), model)

    def test_floor_status_classification(self):
        cases = {
            "eligible": ("gpt-6.1-terra", "gpt-6.2-sol", "gpt-7-astra", "openai/gpt-6.1-terra",
                         "openrouter/gpt-6.2-sol", "openrouter/openai/gpt-6.2-sol", "azure:gpt-6.1-terra",
                         "openai:gpt-6.1-terra"),
            "below": ("gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra", "gpt-6.1-luna", "luna", "Luna",
                      "openai/gpt-5.6-sol", "openrouter/openai/gpt-5.6-sol", "openai:gpt-5.6-sol",
                      "azure:gpt-6-astra"),
            # GPT-shaped but not a strict family id behind plain wrappers:
            # refused, never rewritten, nothing inferred about the provider.
            "unknown": ("gpt-6-1-terra", "GPT-6.1-TERRA", "gpt-4o", "gpt-6.1-terra-preview",
                        " gpt-6.1-terra", "openai/GPT-6.1-terra", "Gpt-5.6-luna",
                        "openrouter/openai/gpt-5.4-mini", "openai:gpt-4o", "azure-gpt-4o",
                        "azure/my-gpt-4o-deploy", "my_gpt-5_deploy", "gpt-6.1-terra:latest",
                        "gpt-6.1-terra/gpt-5.6-luna", "//gpt-5.6-sol", "openai//gpt-6.1-terra",
                        "gpt-latest", "GPT-Terra", "gpt5", "chatgpt-4o-latest", " luna"),
            # Neutral local names (Claude aliases, Ollama tags, names that only
            # start with "gpt", bare family words, wrapped "luna").
            "other": ("", "opus", "claude-opus-5-5", "qwen3.5:27b", "gpt4all", "gpt4all-13b-snoozy",
                      "ollama/gpt4all", "gpt-oss:20b", "ollama/gpt-oss:20b", "gptq-llama", "gpt-j-6b",
                      "gpt-neox-20b", "terra", "sol", "solar-10.7b", "ollama/luna", "azure/prod-deploy",
                      " qwen3.5:27b"),
        }
        for want, ids in cases.items():
            for model in ids:
                with self.subTest(model=model):
                    self.assertEqual(mf.floor_status(model), want)
                    self.assertEqual(mf.below_floor(model), want in ("below", "unknown"))
                    self.assertEqual(cx.gpt_below_floor(model), want in ("below", "unknown"))
        # Literal ids are never rewritten by the classifier (no case folding).
        self.assertEqual(mf.provider_split("openai/gpt-6.1-terra"), ("openai", "gpt-6.1-terra"))
        self.assertEqual(mf.provider_split("gpt-6.1-terra"), ("", "gpt-6.1-terra"))
        self.assertEqual(mf.provider_split("openrouter/openai/gpt-6.2-sol"), ("openrouter/openai", "gpt-6.2-sol"))
        self.assertEqual(mf.provider_split("azure:gpt-5.6-sol"), ("azure", "gpt-5.6-sol"))
        for model in ("openrouter/openai/gpt-6.2-sol", "azure:gpt-6.1-terra"):
            with self.subTest(model=model):
                # Eligible wrapped ids are not canonical kit ids or tier aliases.
                self.assertFalse(mf.eligible(model))
                self.assertFalse(cx.gpt_model_eligible(model))
                self.assertIsNone(cx.alias_to_tier(model))

    def test_user_typed_spelling_is_not_an_alias(self):
        # `gpt-6-1-terra` is not the canonical id and is never normalized to it.
        self.assertFalse(mf.eligible("gpt-6-1-terra"))
        self.assertIsNone(cx.alias_to_tier("gpt-6-1-terra"))
        self.assertTrue(cx.gpt_below_floor("gpt-6-1-terra"))

    def test_cli_check_names_key_only(self):
        r = subprocess.run([sys.executable, "-I", str(REPO / "scripts/lib/model_floor.py"), "check",
                            "HERMES_MODEL=gpt-6.1-terra", "AUX_MODEL=", "HONCHO_MODEL=qwen3.5:27b",
                            "MODEL_ROUTER_GPT_MODELS=gpt-6.1-terra,gpt-5.6-sol"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MODEL_ROUTER_GPT_MODELS=gpt-5.6-sol", r.stderr)
        self.assertNotIn("HERMES_MODEL", r.stderr)
        ok = subprocess.run([sys.executable, "-I", str(REPO / "scripts/lib/model_floor.py"), "check",
                             "HERMES_MODEL=gpt-6.1-terra", "HONCHO_MODEL=gpt-6.2-sol"], capture_output=True)
        self.assertEqual(ok.returncode, 0)
        wrapped = subprocess.run([sys.executable, "-I", str(REPO / "scripts/lib/model_floor.py"), "check",
                                  "HERMES_MODEL=openrouter/openai/gpt-6.2-sol", "AUX_MODEL=azure-gpt-4o",
                                  "HONCHO_MODEL=openai:gpt-5.6-sol", "PRIVATE_MODEL=gpt-oss:20b"],
                                 capture_output=True, text=True)
        self.assertEqual(wrapped.returncode, 1)
        self.assertIn("AUX_MODEL=azure-gpt-4o", wrapped.stderr)
        self.assertIn("HONCHO_MODEL=openai:gpt-5.6-sol", wrapped.stderr)
        self.assertNotIn("HERMES_MODEL", wrapped.stderr)
        self.assertNotIn("PRIVATE_MODEL", wrapped.stderr)


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = _scratch()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "hermes"
        (self.home / "profiles").mkdir(parents=True)
        self.files = {}
        self._put(self.home / "config.yaml", STALE_CONFIG)
        self._put(self.home / ".env", STALE_ENV)
        self._put(self.home / "profiles/coder/config.yaml", STALE_CONFIG)
        self._put(self.home / "profiles/coder/.env", f"CLIPROXY_API_KEY={SECRET}\n")
        self._put(self.home / "profiles/research/config.yaml", CLEAN_CONFIG)
        # Hidden, deleted-looking and symlinked profile directories are never touched.
        for name in (".trash", "old.deleted", "draft~"):
            self._put(self.home / "profiles" / name / "config.yaml", STALE_CONFIG)
        outside = self.tmp / "elsewhere"
        self._put(outside / "config.yaml", STALE_CONFIG)
        (self.home / "profiles/linked").symlink_to(outside, target_is_directory=True)
        self.kit_env = self.tmp / "kit.env"
        self._put(self.kit_env, STALE_ENV + f"HERMES_HOME={self.home}\n")
        self.args = ["--env-file", str(self.kit_env), "--hermes-home", str(self.home)]

    def _put(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)

    def _hashes(self):
        return {p: _sha(p) for p in sorted(self.tmp.rglob("*")) if p.is_file() and not p.is_symlink()}

    def test_dry_run_is_read_only_and_private(self):
        before = self._hashes()
        r = _run(*self.args)
        self.assertEqual(self._hashes(), before, "dry-run changed a file")
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"plan digest: [0-9a-f]{16}")
        for skipped in (".trash", "old.deleted", "draft~", "linked"):
            self.assertIn(f"profiles/{skipped}", r.stdout)
        for line in ("profile:coder:config.yaml model.default: gpt-5.6-luna -> gpt-6.1-terra",
                     "env-file:kit.env HERMES_MODEL: gpt-5.6-luna -> gpt-6.1-terra",
                     "env-file:kit.env AUX_COMPRESSION_MODEL: gpt-5.6-terra -> gpt-6.1-terra",
                     "home:config.yaml fallback_providers: gpt-5.6-sol -> (removed)"):
            self.assertIn(line, r.stdout)
        self.assertNotIn("profile:research", r.stdout)

    def test_apply_requires_invocation_digest(self):
        before = self._hashes()
        for extra in ([], ["--approve", "0" * 16]):
            r = _run(*self.args, "--apply", *extra, check=False)
            self.assertEqual(r.returncode, 3)
        env = dict(os.environ, MODEL_FLOOR_APPROVE="yes", APPROVE="1")
        r = subprocess.run([sys.executable, "-I", str(MIGRATE), *self.args, "--apply"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 3, "approval must not come from the environment")
        self.assertEqual(self._hashes(), before)

    def test_apply_backs_up_rewrites_verifies_and_is_idempotent(self):
        plan = json.loads(_run(*self.args, "--json").stdout)
        untouched = {p: _sha(p) for p in (self.home / "profiles/research/config.yaml",
                                           self.home / "profiles/coder/.env",
                                           self.home / "profiles/.trash/config.yaml",
                                           self.home / "profiles/old.deleted/config.yaml",
                                           self.tmp / "elsewhere/config.yaml")}
        r = _run(*self.args, "--apply", "--approve", plan["digest"])
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertEqual({p: _sha(p) for p in untouched}, untouched)

        cfg = (self.home / "profiles/coder/config.yaml").read_text()
        self.assertFalse([m for m in re.findall(r"gpt[-\w.]+", cfg) if mf.below_floor(m)], cfg)
        self.assertIn("  default: gpt-6.1-terra   # starting model\n", cfg)  # comment kept
        self.assertIn("    model: opus\n", cfg)
        self.assertIn("    model: qwen3.8:27b\n", cfg)
        self.assertIn("    fallback_chain: []\n", cfg)
        self.assertIn("        specialist_classifier_model: gpt-6.1-terra\n", cfg)
        self.assertIn("        available_gpt_models: [gpt-6.1-terra, gpt-6.2-sol]\n", cfg)
        self.assertIn(f'    api_key: "{SECRET}"\n', cfg)
        self.assertIn("        astra_policy: task\n", cfg)
        self.assertNotIn("gpt-6.2-astra", cfg, "never an automatic upgrade to Astra")
        try:
            import yaml  # optional: structural readback when PyYAML is present
        except ImportError:
            yaml = None
        if yaml is not None:
            before, after = yaml.safe_load(STALE_CONFIG), yaml.safe_load(cfg)
            self.assertEqual([e["model"] for e in after["fallback_providers"]], ["opus", "qwen3.8:27b"])
            self.assertEqual(after["delegation"]["fallback_providers"], [])
            self.assertEqual(after["delegation"]["model"], "opus")
            for key in ("delegation", "plugins", "auxiliary", "model"):
                self.assertEqual(set(before[key]), set(after[key]))

        env = self.kit_env.read_text()
        for want in ('export HERMES_MODEL="gpt-6.1-terra"\n', "HONCHO_MODEL=gpt-6.1-terra\n",
                     "AUX_COMPRESSION_MODEL=gpt-6.1-terra # pre-floor\n", "AUX_COMPRESSION_FALLBACK_MODEL=none\n",
                     "MODEL_ROUTER_SPECIALIST_CLASSIFIER_MODEL=gpt-6.1-terra\n",
                     "MODEL_ROUTER_GPT_MODELS=gpt-6.1-terra,gpt-6.2-sol\n", "HERMES_CODING_MODEL=opus\n",
                     "PRIVATE_HONCHO_MODEL=qwen3.5:27b\n", f"CLIPROXY_API_KEY={SECRET}\n"):
            self.assertIn(want, env)

        backups = sorted(p for p in self.tmp.rglob("*.bak.model-floor.*"))
        self.assertEqual(len(backups), 4)  # kit .env, home config+.env, coder config
        for b in backups:
            self.assertEqual(stat.S_IMODE(b.stat().st_mode), 0o600)
            original = b.name.split(".bak.model-floor.")[0]
            self.assertEqual(stat.S_IMODE((b.parent / original).stat().st_mode), 0o600)
        self.assertIn(STALE_CONFIG, [b.read_text() for b in backups])

        again = _run(*self.args)
        self.assertIn("model floor: nothing to change", again.stdout)
        self.assertEqual(_run(*self.args, "--apply", "--approve", "").returncode, 0)
        self.assertEqual(len(list(self.tmp.rglob("*.bak.model-floor.*"))), 4, "no second backup")

    def test_changed_plan_refuses_old_digest(self):
        digest = json.loads(_run(*self.args, "--json").stdout)["digest"]
        (self.home / "profiles/research/config.yaml").write_text(STALE_CONFIG)
        r = _run(*self.args, "--apply", "--approve", digest, check=False)
        self.assertEqual(r.returncode, 3)

    def test_named_missing_profile_is_skipped(self):
        r = _run("--hermes-home", str(self.home), "--profile", "coder", "--profile", "ghost",
                 "--profile", "..", "--json")
        out = json.loads(r.stdout)
        self.assertIn({"target": "profiles/ghost", "reason": "profile not found"}, out["skipped"])
        self.assertIn({"target": "profiles/..", "reason": "hidden or non-canonical profile directory"},
                      out["skipped"])
        files = {c["file"] for c in out["changes"]}
        self.assertTrue(any(f.startswith("profile:coder") for f in files))
        self.assertFalse(any("ghost" in f for f in files))

    def test_symlinked_file_is_not_followed(self):
        target = self.tmp / "real.env"
        self._put(target, STALE_ENV)
        (self.home / "profiles/coder/.env").unlink()
        (self.home / "profiles/coder/.env").symlink_to(target)
        out = json.loads(_run(*self.args, "--json").stdout)
        self.assertIn({"target": "profile:coder:.env", "reason": "symlink (not followed)"}, out["skipped"])

    def test_missing_home_and_env_only(self):
        r = _run("--hermes-home", str(self.tmp / "absent"), "--json")
        self.assertEqual(json.loads(r.stdout)["skipped"][0]["reason"], "hermes home not found")
        r = _run("--env-file", str(self.kit_env), "--no-home", "--json")
        self.assertEqual({c["file"] for c in json.loads(r.stdout)["changes"]}, {"env-file:kit.env"})


class MigrationReview(unittest.TestCase):
    """Structural parsing, refusal of unclear settings, precedence and clobber
    protection (review round 2)."""

    def setUp(self):
        self.tmp = _scratch()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "hermes"
        self.home.mkdir()
        self.cfg = self.home / "config.yaml"

    def _plan(self, text, *extra, env=None):
        self.cfg.write_text(text)
        r = subprocess.run([sys.executable, "-I", str(MIGRATE), "--hermes-home", str(self.home), "--json",
                            *extra], capture_output=True, text=True, timeout=60, env=env)
        return r.returncode, json.loads(r.stdout)

    def _apply(self, text):
        rc, plan = self._plan(text)
        self.assertEqual(rc, 0, plan)
        r = _run("--hermes-home", str(self.home), "--apply", "--approve", plan["digest"])
        self.assertNotIn("NOT clean", r.stdout)
        return self.cfg.read_text()

    def test_known_core_paths_and_flow_items(self):
        out = self._apply(
            "model:\n  default: gpt-6.1-terra\n"
            "delegation:\n  model: gpt-5.6-sol  # child\n  fallback_providers:\n"
            "  - {provider: custom, model: gpt-5.6-luna, base_url: 'http://127.0.0.1:8317/v1'}\n"
            "  - {provider: custom, model: opus}\n"
            "fallback_model:\n  provider: custom\n  model: gpt-6-astra\n"
            "compression:\n  summary_model: 'gpt-5.6-luna'\n"
            "smart_model_routing:\n  enabled: false\n  cheap_model:\n    provider: custom\n    model: gpt-5.6-luna\n"
            "auxiliary:\n  vision:\n    model: gpt-5.6-terra\n    fallback_chain:\n"
            "      - provider: custom\n        model: gpt-5.6-luna\n    timeout: 30\n")
        self.assertIn("  model: gpt-6.1-terra  # child\n", out)
        self.assertIn("  - {provider: custom, model: opus}\n", out)
        self.assertNotIn("gpt-5.6", out)
        self.assertNotIn("gpt-6-astra", out)
        self.assertIn("fallback_model: {}\n", out)
        self.assertIn("  summary_model: 'gpt-6.1-terra'\n", out)
        self.assertIn("    fallback_chain: []\n    timeout: 30\n", out)
        import yaml
        data = yaml.safe_load(out)
        self.assertEqual(data["delegation"]["fallback_providers"], [{"provider": "custom", "model": "opus"}])
        self.assertEqual(data["smart_model_routing"]["cheap_model"]["model"], "gpt-6.1-terra")
        self.assertEqual(data["auxiliary"]["vision"]["model"], "gpt-6.1-terra")
        rc, again = self._plan(out, "--check")
        self.assertEqual((rc, again["changes"], again["unresolved"]), (0, [], []))

    def test_unknown_and_unclear_ids_are_refused_not_rewritten(self):
        for text, key in (("model:\n  default: gpt-6-1-terra\n", "model.default"),
                          ("model:\n  default: GPT-5.6-LUNA\n", "model.default"),
                          ("model:\n  default: openai/gpt-5.6-sol\n", "model.default"),
                          ("model:\n  default: openrouter/openai/gpt-5.6-sol\n", "model.default"),
                          ("model:\n  default: openai:gpt-5.6-luna\n", "model.default"),
                          ("model:\n  default: azure-gpt-4o\n", "model.default"),
                          ("model:\n  default: openrouter/openai/gpt-5.4-mini\n", "model.default"),
                          ("fallback_providers:\n- provider: x\n  model: gpt-4o\n", "fallback_providers"),
                          ("custom_providers:\n- name: x\n  model: gpt-5.6-sol\n", "custom_providers.0.model"),
                          ("m: &m gpt-5.6-luna\nmodel:\n  default: *m\n", "m"),
                          ("model: {default: [unclosed\n", "(file)")):
            with self.subTest(text=text):
                rc, plan = self._plan(text)
                self.assertEqual(rc, 5, plan)
                self.assertIn(key, [u["key"] for u in plan["unresolved"]])
                before = self.cfg.read_bytes()
                r = _run("--hermes-home", str(self.home), "--apply", "--approve", plan["digest"] or "x",
                         check=False)
                self.assertEqual(r.returncode, 5)
                self.assertIn("NOT clean", r.stdout)
                self.assertNotIn("nothing to change", r.stdout)
                self.assertEqual(self.cfg.read_bytes(), before)
                self.assertEqual(list(self.home.glob("*.bak.model-floor.*")), [])

    def test_literal_ids_outside_the_floor_are_kept(self):
        text = ("model:\n  default: openrouter/openai/gpt-6.2-sol\n"
                "fallback_providers:\n- {provider: ollama, model: gpt4all}\n- {provider: ollama, model: gpt-oss:20b}\n"
                "- {provider: azure, model: 'azure:gpt-6.1-terra'}\n- {provider: ollama, model: ollama/luna}\n"
                "delegation:\n  model: claude-opus-5-5\n")
        rc, plan = self._plan(text, "--check")
        self.assertEqual((rc, plan["changes"], plan["unresolved"]), (0, [], []))

    def test_env_inline_comments_quotes_and_unclear_syntax(self):
        envf = self.tmp / "kit.env"
        envf.write_text("HONCHO_MODEL=gpt-5.6-luna   # old default\nAUX_MODEL='gpt-5.6-sol' # q\n"
                        "HERMES_MODEL=gpt-6.1-terra # current\n")
        plan = json.loads(_run("--env-file", str(envf), "--no-home", "--json").stdout)
        _run("--env-file", str(envf), "--no-home", "--apply", "--approve", plan["digest"])
        self.assertEqual(envf.read_text(), "HONCHO_MODEL=gpt-6.1-terra   # old default\n"
                                           "AUX_MODEL='gpt-6.1-terra' # q\nHERMES_MODEL=gpt-6.1-terra # current\n")
        for bad in ('HONCHO_MODEL="gpt-5.6-luna\n', "HONCHO_MODEL=${OLD:-gpt-5.6-luna}\n",
                    "HONCHO_MODEL=gpt-5.6-luna extra\n"):
            with self.subTest(line=bad):
                envf.write_text(bad)
                r = _run("--env-file", str(envf), "--no-home", "--json", check=False)
                self.assertEqual(r.returncode, 5)
                self.assertEqual(json.loads(r.stdout)["unresolved"][0]["key"], "HONCHO_MODEL")

    def test_home_and_compose_precedence(self):
        envf = self.tmp / "kit.env"
        envf.write_text(f"HERMES_HOME={self.tmp}/first\nHERMES_HOME={self.home}\nHONCHO_MODEL=gpt-6.1-terra\n")
        self.cfg.write_text("model:\n  default: gpt-5.6-luna\n")
        env = dict(os.environ, HERMES_HOME=str(self.tmp / "elsewhere"), HONCHO_MODEL="gpt-5.6-luna",
                   HOME=str(self.tmp))
        r = subprocess.run([sys.executable, "-I", str(MIGRATE), "--env-file", str(envf), "--json"],
                           capture_output=True, text=True, env=env)
        out = json.loads(r.stdout)
        self.assertEqual(r.returncode, 5)
        self.assertIn("home:config.yaml", {c["file"] for c in out["changes"]})  # last HERMES_HOME wins
        self.assertTrue(any("elsewhere" in n for n in out["notes"]), out["notes"])
        self.assertIn({"file": "process-environment", "key": "HONCHO_MODEL", "model": "gpt-5.6-luna",
                       "reason": "exported in this shell; docker compose prefers it over .env: unset HONCHO_MODEL"},
                      out["unresolved"])
        # No HERMES_HOME in the kit .env: the installer default (~/.hermes), not $HERMES_HOME.
        envf.write_text("HONCHO_MODEL=gpt-6.1-terra\n")
        env.pop("HONCHO_MODEL")
        out = json.loads(subprocess.run([sys.executable, "-I", str(MIGRATE), "--env-file", str(envf), "--json"],
                                        capture_output=True, text=True, env=env).stdout)
        self.assertIn({"target": str(self.tmp / ".hermes"), "reason": "hermes home not found"}, out["skipped"])

    def test_swap_to_symlink_after_plan_is_refused(self):
        rc, plan = self._plan("model:\n  default: gpt-5.6-luna\n")
        outside = self.tmp / "outside.yaml"
        outside.write_text(self.cfg.read_text())
        self.cfg.unlink()
        self.cfg.symlink_to(outside)
        r = _run("--hermes-home", str(self.home), "--apply", "--approve", plan["digest"], check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(outside.read_text(), "model:\n  default: gpt-5.6-luna\n")
        self.assertTrue(self.cfg.is_symlink())
        rc, chk = self._plan_check_only()
        self.assertEqual(rc, 5)

    def _plan_check_only(self):
        r = subprocess.run([sys.executable, "-I", str(MIGRATE), "--hermes-home", str(self.home), "--json",
                            "--check"], capture_output=True, text=True)
        return r.returncode, json.loads(r.stdout)

    def test_check_fails_closed(self):
        self.cfg.write_text("model:\n  default: gpt-6.1-terra\n")
        self.assertEqual(self._plan_check_only()[0], 0)
        self.cfg.write_text("model:\n  default: gpt-5.6-luna\n")
        self.assertEqual(self._plan_check_only()[0], 5)
        r = subprocess.run([sys.executable, "-I", str(MIGRATE), "--hermes-home", str(self.tmp / "absent"),
                            "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 5)


class InstalledConfigMigration(unittest.TestCase):
    """A config rendered by this kit's installer, then edited back to the
    pre-floor values an older installer wrote, migrates to a config the
    current installer would accept (no below-floor id left, nothing else moved)."""

    def test_rendered_template_round_trip(self):
        tmp = _scratch()
        self.addCleanup(shutil.rmtree, tmp, True)
        home = tmp / "hermes"
        home.mkdir()
        envf = tmp / "fixture.env"
        envf.write_text(f"CLIPROXY_PORT=8317\nCLIPROXY_API_KEY={SECRET}\nCLAUDE_MAX_PROXY_PORT=3456\n"
                        f"CLAUDE_MAX_PROXY_API_KEY={SECRET}\nHERMES_HOME={home}\nREPOS_DIR={tmp}/repos\n")
        out = home / "config.yaml"
        r = subprocess.run(["bash", "scripts/hermes-install.sh", "--render-only", str(out)], cwd=REPO,
                           env=dict(HOME=str(tmp), PATH="/usr/bin:/bin", ENV_FILE=str(envf),
                                    HERMES_SKIP_INSTALL="1", HERMES_SKIP_GATEWAY="1"),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        current = out.read_text()
        self.assertIn("  default: gpt-6.1-terra\n", current)
        old = current.replace("  default: gpt-6.1-terra\n", "  default: gpt-5.6-luna\n")
        old = re.sub(r"(  compression:\n(?:    .*\n)*?    fallback_chain: )\[\]",
                     r'\1[{provider: custom, model: "gpt-5.6-luna", base_url: "http://127.0.0.1:8317/v1", '
                     r'key_env: CLIPROXY_API_KEY}]', old)
        self.assertNotEqual(old, current)
        out.write_text(old)
        digest = json.loads(_run("--hermes-home", str(home), "--json").stdout)["digest"]
        _run("--hermes-home", str(home), "--apply", "--approve", digest)
        self.assertEqual(out.read_text(), current)


class ScriptDefaults(unittest.TestCase):
    """Executable defaults never select a below-floor GPT model."""

    def _load(self, name):
        spec = importlib.util.spec_from_file_location(name.replace("-", "_"), REPO / "scripts" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_knowledge_tools(self):
        kc = self._load("knowledge-classify")
        self.assertTrue(mf.eligible(kc.DEFAULT_MODEL))
        kb = self._load("knowledge-benchmark")
        self.assertTrue(all(mf.eligible(m) for m in kb.DEFAULT_MODELS), kb.DEFAULT_MODELS)

    def test_tracked_executables_default_to_eligible_models(self):
        files = subprocess.run(["git", "ls-files", "scripts/*.sh", "scripts/*.py", "scripts/windows-vm/*.ps1",
                                "hermes/plugins/*.py", ".env.example", "docker-compose.yml",
                                "hermes/config.yaml.tmpl", "Makefile"],
                               cwd=REPO, capture_output=True, text=True).stdout.split()
        # Lines that name a below-floor id only to say it is refused/ignored.
        allowed_mentions = re.compile(r"[Bb]elow[- ]floor|[Rr]efus|never chosen|rejects|older than|"
                                      r"version 6\.0|non-canonical|not a family|v1 benchmark")
        offenders = []
        for f in files:
            if "/tests/" in f or Path(f).name.startswith("test_"):
                continue
            for n, line in enumerate((REPO / f).read_text(errors="replace").splitlines(), 1):
                for m in (x.rstrip(".-") for x in re.findall(r"gpt-[0-9][\w.-]*", line)):
                    if mf.below_floor(m) and not allowed_mentions.search(line):
                        offenders.append(f"{f}:{n}: {m}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
