#!/usr/bin/env python3
"""Pure helpers for the gstack method adapters (spec, plan reviews, review, qa).

Adapted method ideas from gstack 622e8b1f76d293b647eca7b33d9dd060ab618b21 (MIT, see
hermes/skills/gstack-*/references/gstack-license.md): the spec redaction gate, the review
checklist's PR-type rule and the QA command-capture contract. No gstack code is copied
and nothing here runs a command, reads the network or writes a file; every function
works on text the caller already read.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------- redaction gate (spec)

SECRET_RE = re.compile(
    r"AKIA[0-9A-Z]{16}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bsk-[A-Za-z0-9-]{16,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,}", re.I)


def secret_paths(value: Any, path: str = "") -> List[str]:
    """JSON paths ('a/b/0') of string values that look like credentials."""
    out: List[str] = []
    if isinstance(value, str):
        if SECRET_RE.search(value):
            out.append(path or "<root>")
    elif isinstance(value, dict):
        for k, v in value.items():
            out += secret_paths(v, f"{path}/{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out += secret_paths(v, f"{path}/{i}" if path else str(i))
    return out


# ---------------------------------------------------------------- dependency graphs

def find_cycle(edges: Dict[str, Sequence[str]]) -> Optional[List[str]]:
    """A cycle in ``node -> depends_on`` as a node list, or None (unknown targets ignored)."""
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(n: str) -> Optional[List[str]]:
        state[n] = 1
        stack.append(n)
        for m in edges.get(n, ()):
            if m not in edges:
                continue
            if state.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if m not in state:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        state[n] = 2
        return None

    for n in sorted(edges):
        if n not in state:
            found = visit(n)
            if found:
                return found
    return None


# ---------------------------------------------------------------- review: PR type and unified diffs

_DOC_NAME = re.compile(r"^(README|CHANGELOG)(\.[A-Za-z]+)?$", re.I)
_CI = re.compile(r"^(\.github/|\.circleci/|\.gitlab-ci\.yml$|Jenkinsfile$|\.buildkite/|charts/|helm/|k8s/|kubernetes/)"
                 r"|(^|/)(Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|compose[^/]*\.ya?ml)$|\.tf$")
_TOOLING = re.compile(r"\.(sh|bash|bat|cmd|ps1)$|(^|/)Makefile$|(^|/)(\.eslintrc[^/]*|\.prettierrc[^/]*|\.flake8|ruff\.toml|"
                      r"\.editorconfig|\.markdownlint[^/]*|\.shellcheckrc)$")
_PROMPTISH = re.compile(r"(^|/)SKILL\.md$|\.tmpl$|(^|/)(prompts?|templates?|skills?)/", re.I)


def path_type(path: str) -> str:
    """One changed path's PR type under the gstack review checklist rule (application when in doubt)."""
    name = path.rsplit("/", 1)[-1]
    if not _PROMPTISH.search(path) and (
            (path.startswith("docs/") and name.lower().endswith((".md", ".txt"))) or _DOC_NAME.match(name)):
        return "docs-only"
    if _CI.search(path):
        return "ci-infra"
    if _TOOLING.search(path):
        return "scripts-tooling"
    return "application"


def pr_type(paths: Iterable[str]) -> str:
    types = {path_type(p) for p in paths}
    if not types:
        return "application"
    return types.pop() if len(types) == 1 else "mixed"


ALWAYS_RUN_CATEGORIES = ("gate-integrity", "shell-injection", "completeness-gaps", "distribution-ci", "repo-conventions")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff(text: str) -> Dict[str, Dict[str, Any]]:
    """Unified diff -> {new path: {"lines": {new line no: text}, "added": {line nos}, "deleted": bool}}.
    Context and added lines are indexed by their line number in the new file; text before the first
    'diff --git' (a handoff preamble) is ignored."""
    files: Dict[str, Dict[str, Any]] = {}
    cur: Optional[Dict[str, Any]] = None
    old_path: Optional[str] = None
    line = left = 0  # left: new-file lines the hunk header still promises
    in_hunk = False
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            cur, in_hunk, old_path = None, False, None
            continue
        if not in_hunk and raw.startswith("--- "):
            old_path = raw[4:].strip()
            continue
        if not in_hunk and raw.startswith("+++ "):
            new = raw[4:].strip()
            deleted = new == "/dev/null"
            path = (old_path or "") if deleted else new
            path = path[2:] if path.startswith(("a/", "b/")) else path
            cur = files.setdefault(path, {"lines": {}, "added": set(), "deleted": deleted})
            continue
        m = _HUNK.match(raw)
        if m and cur is not None:
            line, in_hunk = int(m.group(1)), True
            left = int(m.group(2)) if m.group(2) is not None else 1
            continue
        if not in_hunk or cur is None:
            continue
        if raw.startswith("+"):
            cur["lines"][line] = raw[1:]
            cur["added"].add(line)
            line, left = line + 1, left - 1
        elif raw.startswith(" ") or (raw == "" and left > 0):  # an empty context line whose space was stripped
            cur["lines"][line] = raw[1:]
            line, left = line + 1, left - 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            in_hunk = False
    return files


# ---------------------------------------------------------------- qa: command captures

def parse_capture(text: str) -> Optional[Dict[str, Any]]:
    """A command capture: '$ <command>', '--- stdout', ..., '--- stderr', ..., 'exit=<n>' (last line)."""
    lines = text.rstrip("\n").split("\n")
    if len(lines) < 4 or not lines[0].startswith("$ ") or lines[1] != "--- stdout":
        return None
    m = re.fullmatch(r"exit=(-?\d{1,3})", lines[-1])
    if not m or "--- stderr" not in lines[2:-1]:
        return None
    split = lines.index("--- stderr", 2)
    return {"command": lines[0][2:], "stdout": "\n".join(lines[2:split]), "stderr": "\n".join(lines[split + 1:-1]),
            "exit": int(m.group(1))}
