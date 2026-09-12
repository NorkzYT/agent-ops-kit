#!/usr/bin/env python3
"""
Sentinel zone enforcement hook.
Blocks edits to protected files and code marked with sentinel markers.

Sentinel Zones:
- Files matching PROTECTED_GLOBS patterns
- Code containing sentinel markers in comments (code files only)

See .claude/docs/sentinel-zones.md for full documentation.
"""
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

from typing import List, Optional, Tuple

# Temporary escape hatch (set before starting claude):
#   export CLAUDE_ALLOW_PROTECTED_EDITS=1
ALLOW_OVERRIDE_ENV = "CLAUDE_ALLOW_PROTECTED_EDITS"

# Sentinel markers in code that indicate protected sections
# These must appear in code comments to be recognized
SENTINEL_MARKER_PATTERNS = [
    r"LEGACY_PROTECTED\b",
    r"DO_NOT_MODIFY\b",
    r"SECURITY_CRITICAL\b",
]

# File extensions that support sentinel marker detection
# Only check code files, not documentation or config
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".scala",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt",
    ".sh", ".bash", ".zsh", ".fish",
}

# Allowlist: these patterns are safe to edit even if they match protected globs
ALLOWED_PATTERNS = [
    "**/.env.example",
    "**/.env.sample",
    "**/.env.template",
    # docker-compose prod files are safe to edit (tracked in git)
    "**/docker-compose.prod*.yml",
    "**/docker-compose.production*.yml",
]

# Conservative defaults. Tune to your org.
PROTECTED_GLOBS = [
    # .env and variants (but .env.example/.env.sample/.env.template are allowed above)
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


def to_project_relative(path_str: str, project_dir: str) -> str:
    """
    Convert an absolute path to project-relative (POSIX style) if possible.
    Keep as-is (normalized) if not under project dir.
    """
    p = Path(path_str)
    proj = Path(project_dir)

    try:
        rp = p.resolve()
        rproj = proj.resolve()
        if str(rp).startswith(str(rproj) + os.sep) or rp == rproj:
            rel = rp.relative_to(rproj).as_posix()
            return rel
    except Exception:
        pass

    # Fall back: normalize separators
    return p.as_posix().lstrip("./")


def is_allowed(rel_posix: str) -> bool:
    """Check if file matches an allowed pattern (takes precedence over protected)."""
    rel = PurePosixPath(rel_posix)
    for pat in ALLOWED_PATTERNS:
        if rel.match(pat):
            return True
    return False


def is_protected(rel_posix: str) -> bool:
    # Allowlist takes precedence
    if is_allowed(rel_posix):
        return False
    rel = PurePosixPath(rel_posix)
    for pat in PROTECTED_GLOBS:
        if rel.match(pat):
            return True
    return False


def is_code_file(file_path: str) -> bool:
    """Check if file is a code file that supports sentinel markers."""
    ext = Path(file_path).suffix.lower()
    return ext in CODE_EXTENSIONS


def has_sentinel_marker(file_path: str) -> Tuple[bool, Optional[str]]:
    """
    Check if code file contains sentinel markers in comments.
    Returns (is_protected, marker_found).
    Only checks code files (not markdown, config, etc).
    """
    # Only check code files
    if not is_code_file(file_path):
        return False, None

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(50000)  # Only check first 50KB

            # Look for markers that indicate truly protected code
            for marker_pattern in SENTINEL_MARKER_PATTERNS:
                match = re.search(marker_pattern, content, re.IGNORECASE)
                if match:
                    return True, match.group(0)
    except (OSError, IOError):
        pass  # File doesn't exist yet or can't be read
    return False, None


def extract_paths(tool_name: str, tool_input: dict) -> List[str]:
    paths: List[str] = []

    # Write/Edit have file_path.
    fp = tool_input.get("file_path")
    if isinstance(fp, str) and fp.strip():
        paths.append(fp.strip())

    # MultiEdit varies; handle common shapes defensively.
    # If Claude passes multiple edits with file paths.
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict):
                p = e.get("file_path") or e.get("path")
                if isinstance(p, str) and p.strip():
                    paths.append(p.strip())

    # Dedup
    out: List[str] = []
    seen: set[str] = set()
    for p in paths:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def main() -> int:
    if os.getenv(ALLOW_OVERRIDE_ENV) == "1":
        return 0

    project_dir = os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd()

    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        print(f"protect_files: invalid JSON input: {e}", file=sys.stderr)
        return 0  # don't break the session

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return 0

    if not isinstance(tool_input, dict):
        return 0

    paths = extract_paths(tool_name, tool_input)
    for p in paths:
        rel = to_project_relative(p, project_dir)

        # Check glob-based protection
        if is_protected(rel):
            print(
                "\n".join(
                    [
                        f"Blocked edit to protected file: {rel}",
                        "This kit blocks .env*, secret material, and common prod config paths.",
                        f"To override temporarily: export {ALLOW_OVERRIDE_ENV}=1 (then restart Claude).",
                        "Or edit .claude/hooks/protect_files.py to adjust PROTECTED_GLOBS.",
                    ]
                ),
                file=sys.stderr,
            )
            return 2  # Exit code 2 blocks PreToolUse tool call.

        # Check sentinel markers in existing code files
        abs_path = Path(project_dir) / rel
        has_marker, marker = has_sentinel_marker(str(abs_path))
        if has_marker:
            print(
                "\n".join(
                    [
                        f"Blocked edit to sentinel-protected file: {rel}",
                        f"File contains '{marker}' marker indicating protected code.",
                        "This code requires explicit approval before modification.",
                        f"To override temporarily: export {ALLOW_OVERRIDE_ENV}=1 (then restart Claude).",
                        "See .claude/docs/sentinel-zones.md for documentation.",
                    ]
                ),
                file=sys.stderr,
            )
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
