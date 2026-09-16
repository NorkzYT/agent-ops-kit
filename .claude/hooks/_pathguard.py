#!/usr/bin/env python3
"""Shared path-protection logic for protect_files.py and guard_bash.py.

Provides robust glob matching for protected/allowed path patterns. This exists
because ``PurePath.match()`` on Python 3.12 treats ``**`` as a single path
segment (recursive matching via ``full_match()`` only landed in 3.13). As a
result ``PurePosixPath('.env').match('**/.env')`` is ``False`` on 3.12, so
top-level files like ``./.env`` or ``mysecret.txt`` were never blocked.

We translate globs to regexes ourselves so ``**/`` matches zero or more leading
path segments (top-level AND nested) on every supported Python version.
"""
import os
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import List

# -----------------------------------------------------------------------------
# Allowlist: safe to edit even if they match a protected glob (takes precedence).
# -----------------------------------------------------------------------------
ALLOWED_PATTERNS: List[str] = [
    "**/.env.example",
    "**/.env.sample",
    "**/.env.template",
    # docker-compose prod files are safe to edit (tracked in git)
    "**/docker-compose.prod*.yml",
    "**/docker-compose.production*.yml",
]

# -----------------------------------------------------------------------------
# Protected globs. Conservative defaults; tune to your org.
# -----------------------------------------------------------------------------
PROTECTED_GLOBS: List[str] = [
    # .env and variants (but .env.example/.env.sample/.env.template are allowed)
    "**/.env",
    "**/.env.*",

    # Common key/cert material
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/id_rsa",
    "**/id_rsa.*",
    "**/id_ed25519",
    "**/id_ed25519.*",

    # Common secret files
    "**/*secret*",
    "**/*secrets*",
    "**/.aws/**",
    "**/.ssh/**",
    "**/*kubeconfig*",

    # Common prod config patterns
    "**/docker-compose.prod*.yml",
    "**/docker-compose.production*.yml",
    "**/.github/workflows/*deploy*.yml",
    "**/infra/prod/**",
    "**/k8s/prod/**",
    "**/terraform/prod/**",
    "**/config/prod/**",
    "**/config/production/**",

    # Agent runtime credentials (Hermes, proxies)
    "**/.hermes/.env",
    "**/.hermes/auth.json",
    "**/.hermes/honcho.json",
    "**/data/cliproxyapi/**",
]


@lru_cache(maxsize=1024)
def _compile_glob(pattern: str) -> "re.Pattern[str]":
    """Translate a glob into an anchored regex with recursive ``**`` support.

    - ``**/`` matches zero or more leading path segments (so it matches both a
      top-level file and any nested one).
    - ``**``  matches anything, including ``/``.
    - ``*``   matches anything except ``/``.
    - ``?``   matches a single character except ``/``.
    """
    i, n = 0, len(pattern)
    out = ["^"]
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")  # **/  -> zero or more segments
                    i += 3
                else:
                    out.append(".*")        # **   -> anything incl. /
                    i += 2
            else:
                out.append("[^/]*")         # *    -> within a segment
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def normalize(rel_posix: str) -> str:
    """Normalize a candidate path to POSIX form, dropping a leading ``./``."""
    s = str(rel_posix).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def match_glob(rel_posix: str, pattern: str) -> bool:
    return _compile_glob(pattern).match(normalize(rel_posix)) is not None


def is_allowed(rel_posix: str) -> bool:
    """True if the path matches an allow pattern (takes precedence over protected)."""
    return any(match_glob(rel_posix, p) for p in ALLOWED_PATTERNS)


def is_protected(rel_posix: str) -> bool:
    """True if the path is protected (and not explicitly allowed)."""
    if is_allowed(rel_posix):
        return False
    return any(match_glob(rel_posix, p) for p in PROTECTED_GLOBS)


def to_project_relative(path_str: str, project_dir: str) -> str:
    """Convert a path to a project-relative POSIX string when possible.

    Falls back to a normalized POSIX form (preserving any ``..`` traversal) when
    the path is not under the project directory or cannot be resolved.
    """
    p = Path(path_str)
    proj = Path(project_dir)
    try:
        rp = p.resolve()
        rproj = proj.resolve()
        if rp == rproj or str(rp).startswith(str(rproj) + os.sep):
            return rp.relative_to(rproj).as_posix()
    except Exception:
        pass
    return normalize(PurePosixPath(path_str).as_posix())
