#!/usr/bin/env python3
"""Installed-Hermes compatibility of the capability skills (read-only, source-only).

Reads the installed Hermes source as TEXT (``ast`` literals only: nothing is imported
or executed, no runtime is started) for the skill rules it enforces or lints:

  hard (tools/skill_manager_tool.py): name pattern and length, description length,
      SKILL.md size, supporting-file size, frontmatter with name + description and
      a body;
  advisory (tools/skill_linter.py, agent/skill_utils.py): prompt-index description
      width, name == directory, version/author/license metadata, metadata.hermes.tags,
      a "When to use" section, forbidden scaffolding files, dangling references.

Then checks the capability skills of this source tree against them, and reports name
collisions with skills already installed under the given skill roots and the installed
model-router plugin's drift from this source. A rule whose constant cannot be found is
reported ``unknown`` (never a pass).

Usage: python3 scripts/capability_compat.py --hermes-agent DIR [--skills-root DIR ...]
           [--installed-plugin DIR] [--skills NAME ...]
Exit: 0 compatible, 1 findings or unknown rules, 2 invalid input.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
MAX_READ = 2 * 1024 * 1024

# (file, constant) pairs read as literals from the installed Hermes source.
RULE_SOURCES = {
    "MAX_NAME_LENGTH": "tools/skill_manager_tool.py",
    "MAX_DESCRIPTION_LENGTH": "tools/skill_manager_tool.py",
    "MAX_SKILL_CONTENT_CHARS": "tools/skill_manager_tool.py",
    "MAX_SKILL_FILE_BYTES": "tools/skill_manager_tool.py",
    "VALID_NAME_RE": "tools/skill_manager_tool.py",
    "SKILL_PROMPT_DESC_LIMIT": "agent/skill_utils.py",
    "SKILL_SUPPORT_DIRS": "agent/skill_utils.py",
    "_FORBIDDEN_FILES": "tools/skill_linter.py",
    "_EXPECTED_SECTIONS": "tools/skill_linter.py",
    "_BODY_SOFT_BUDGET_CHARS": "tools/skill_linter.py",
    "LINTER_NAME_RE": "tools/skill_linter.py",
}


def _read(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_READ:
        raise OSError(f"{path}: not a regular bounded file")
    return path.read_text(encoding="utf-8", errors="replace")


def _literal(node: ast.AST) -> Any:
    """Literal value of an assignment, unwrapping re.compile("..."), frozenset((...)) and int
    arithmetic such as 1_048_576; anything else is None (unknown)."""
    if isinstance(node, ast.Call) and node.args:
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name in ("compile", "frozenset", "set", "tuple"):
            inner = _literal(node.args[0])
            return frozenset(inner) if name == "frozenset" and inner is not None else inner
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def installed_rules(agent_dir: Path) -> Dict[str, Any]:
    """``{constant: value or None}`` from the installed Hermes source text."""
    rules: Dict[str, Any] = {k: None for k in RULE_SOURCES}
    trees: Dict[str, Optional[ast.Module]] = {}
    for rel in sorted(set(RULE_SOURCES.values())):
        try:
            trees[rel] = ast.parse(_read(agent_dir / rel))
        except (OSError, SyntaxError, ValueError):
            trees[rel] = None
    for key, rel in RULE_SOURCES.items():
        tree = trees[rel]
        if tree is None:
            continue
        if key == "LINTER_NAME_RE":  # re.fullmatch(r"...", name) inside _check_frontmatter
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "fullmatch" and node.args
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    rules[key] = node.args[0].value
                    break
            continue
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            if any(isinstance(t, ast.Name) and t.id == key for t in targets) and node.value is not None:
                rules[key] = _literal(node.value)
    return rules


def _frontmatter(text: str) -> Tuple[Optional[dict], str, str]:
    """(mapping or None, body, error)."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return None, "", "SKILL.md must start with YAML frontmatter"
    m = re.search(r"\n---\s*\n", text[3:])
    if not m:
        return None, "", "frontmatter is not closed"
    try:
        import yaml  # the same parser family Hermes uses (yaml.safe_load)
    except ImportError:
        return None, "", "PyYAML unavailable: frontmatter not checked"
    try:
        data = yaml.safe_load(text[3:m.start() + 3])
    except yaml.YAMLError as exc:
        return None, "", f"YAML error: {type(exc).__name__}"
    if not isinstance(data, dict):
        return None, "", "frontmatter is not a mapping"
    return data, text[m.end() + 3:], ""


def capability_skills(repo: Path = REPO) -> List[str]:
    """Exposed skill names of the registry's non-existing capabilities with files in this tree."""
    reg = json.loads(_read(repo / "hermes/capabilities/registry.json"))
    return [c["exposed_skill_name"] for c in reg["capabilities"]
            if c["exposed_skill_name"] and (repo / "hermes/skills" / c["exposed_skill_name"] / "SKILL.md").is_file()]


def check_skill(skill_dir: Path, rules: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    def add(level: str, rule: str, msg: str) -> None:
        out.append({"skill": skill_dir.name, "level": level, "rule": rule, "message": msg})

    try:
        text = _read(skill_dir / "SKILL.md")
    except OSError as exc:
        add("error", "skill-md", str(exc))
        return out
    fm, body, err = _frontmatter(text)
    if fm is None:
        add("error", "frontmatter", err)
        return out
    name, desc = str(fm.get("name", "")).strip(), str(fm.get("description", "")).strip().strip("'\"")
    if not name or not desc:
        add("error", "frontmatter", "name and description are required")
    if not body.strip():
        add("error", "body", "SKILL.md needs content after the frontmatter")

    def need(key: str) -> Any:
        if rules.get(key) is None:
            add("unknown", key, f"installed Hermes rule {key} not found; not checked")
        return rules.get(key)

    if need("VALID_NAME_RE") is not None and not re.match(rules["VALID_NAME_RE"], name):
        add("error", "name-format", f"{name!r} fails the Hermes name pattern")
    if need("MAX_NAME_LENGTH") is not None and len(name) > rules["MAX_NAME_LENGTH"]:
        add("error", "name-length", f"{len(name)} > {rules['MAX_NAME_LENGTH']}")
    if need("MAX_DESCRIPTION_LENGTH") is not None and len(desc) > rules["MAX_DESCRIPTION_LENGTH"]:
        add("error", "description-length", f"{len(desc)} > {rules['MAX_DESCRIPTION_LENGTH']}")
    if need("MAX_SKILL_CONTENT_CHARS") is not None and len(text) > rules["MAX_SKILL_CONTENT_CHARS"]:
        add("error", "content-size", f"{len(text)} chars > {rules['MAX_SKILL_CONTENT_CHARS']}")
    if need("MAX_SKILL_FILE_BYTES") is not None:
        for f in sorted(skill_dir.rglob("*")):
            if f.is_symlink():
                add("error", "symlink", f"{f.relative_to(skill_dir)} is a symlink")
            elif f.is_file() and f.stat().st_size > rules["MAX_SKILL_FILE_BYTES"]:
                add("error", "file-size", f"{f.relative_to(skill_dir)} exceeds {rules['MAX_SKILL_FILE_BYTES']} bytes")
    if need("LINTER_NAME_RE") is not None and not re.fullmatch(rules["LINTER_NAME_RE"], name):
        add("advisory", "name-format", f"{name!r} fails the linter name pattern")
    if name != skill_dir.name:
        add("advisory", "name-dir-mismatch", f"name {name!r} != directory {skill_dir.name!r}")
    if need("SKILL_PROMPT_DESC_LIMIT") is not None and len(desc) > rules["SKILL_PROMPT_DESC_LIMIT"]:
        add("advisory", "description-width",
            f"{len(desc)} chars; the prompt index clips at {rules['SKILL_PROMPT_DESC_LIMIT']} and loses routing signal")
    for key in ("version", "author", "license"):
        if key not in fm:
            add("advisory", "missing-metadata", f"frontmatter lacks {key!r}")
    meta = fm.get("metadata")
    if not (isinstance(meta, dict) and isinstance(meta.get("hermes"), dict) and "tags" in meta["hermes"]):
        add("advisory", "missing-metadata", "metadata.hermes.tags missing")
    sections = need("_EXPECTED_SECTIONS")
    if sections is not None and not any(re.search(rf"^#+\s+{re.escape(s)}", body, re.M) for s in sections):
        add("advisory", "missing-section", "no 'When to use' section")
    budget = need("_BODY_SOFT_BUDGET_CHARS")
    if budget is not None and len(body) > budget:
        add("advisory", "oversized-body", f"body {len(body)} chars > {budget}")
    forbidden = need("_FORBIDDEN_FILES")
    for fname in forbidden or ():
        if (skill_dir / fname).exists():
            add("advisory", "forbidden-file", fname)
    for rel in sorted({m.group(0) for m in re.finditer(r"(references|templates|assets)/[\w./-]+", body)}):
        if "*" not in rel and not rel.endswith("/") and not (skill_dir / rel).exists():
            add("advisory", "dangling-reference", rel)
    return out


def installed_skill_names(roots: Iterable[Path], exclude: Iterable[Path] = ()) -> Dict[str, List[str]]:
    """``{name or directory name: [SKILL.md paths]}`` under the installed skill roots (stat + bounded
    frontmatter reads; support dirs are not skills)."""
    skip = {Path(p).resolve() for p in exclude}
    out: Dict[str, List[str]] = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for md in sorted(root.rglob("SKILL.md"))[:5000]:
            if any(part in ("references", "templates", "assets", "scripts") or part.startswith(".")
                   for part in md.relative_to(root).parts[:-1]) or md.parent.resolve() in skip:
                continue
            try:
                fm, _, _ = _frontmatter(_read(md))
            except OSError:
                fm = None
            for alias in {md.parent.name, str((fm or {}).get("name", "")).strip()} - {""}:
                out.setdefault(alias, []).append(str(md))
    return out


def _tree_digest(d: Path) -> Dict[str, str]:
    return {str(f.relative_to(d)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(d.rglob("*")) if f.is_file() and "__pycache__" not in f.parts}


def report(agent_dir: Path, skills: Sequence[str], skills_roots: Sequence[Path],
           installed_plugin: Optional[Path], repo: Path = REPO) -> Dict[str, Any]:
    rules = installed_rules(agent_dir)
    findings: List[Dict[str, str]] = []
    for name in skills:
        findings += check_skill(repo / "hermes/skills" / name, rules)
    installed = installed_skill_names(skills_roots)
    collisions = []
    for name in skills:
        for path in installed.get(name, []):
            same = (Path(path).read_bytes() == (repo / "hermes/skills" / name / "SKILL.md").read_bytes())
            collisions.append({"skill": name, "installed": path, "identical": same})
    out: Dict[str, Any] = {
        "hermes_agent": str(agent_dir), "rules": {k: (sorted(v) if isinstance(v, frozenset) else v) for k, v in rules.items()},
        "skills_checked": list(skills), "findings": findings, "collisions": collisions,
        "note": "static, read-only: Hermes source parsed as text; nothing imported, executed, installed or restarted"}
    if installed_plugin is not None:
        src, live = _tree_digest(repo / "hermes/plugins/model-router"), (
            _tree_digest(installed_plugin) if installed_plugin.is_dir() else {})
        out["model_router_plugin"] = {
            "installed": str(installed_plugin), "present": installed_plugin.is_dir(),
            "differs": sorted(k for k in set(src) | set(live) if src.get(k) != live.get(k)),
            "has_capability_seam": "capability_catalog" in (
                (installed_plugin / "specialists.py").read_text(errors="replace")
                if (installed_plugin / "specialists.py").is_file() else "")}
    out["compatible"] = not any(f["level"] in ("error", "unknown") for f in findings) and not any(
        not c["identical"] for c in collisions)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only installed-Hermes compatibility of the capability skills")
    ap.add_argument("--hermes-agent", required=True, help="installed hermes-agent source directory (read only)")
    ap.add_argument("--skills-root", action="append", default=[], help="installed skill root to scan for collisions")
    ap.add_argument("--installed-plugin", help="installed model-router plugin directory (read only)")
    ap.add_argument("--skills", nargs="*", help="skill names (default: the registry's capability skills)")
    args = ap.parse_args(argv)
    agent = Path(args.hermes_agent)
    if not agent.is_dir():
        print(f"capability_compat: {agent} is not a directory", file=sys.stderr)
        return 2
    rep = report(agent, args.skills or capability_skills(), [Path(p) for p in args.skills_root],
                 Path(args.installed_plugin) if args.installed_plugin else None)
    print(json.dumps(rep, indent=2, sort_keys=True))
    advisory = sum(1 for f in rep["findings"] if f["level"] == "advisory")
    return 0 if rep["compatible"] and not advisory else 1


if __name__ == "__main__":
    sys.exit(main())
