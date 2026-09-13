#!/usr/bin/env python3
"""
Context checkpoint hook (Stop hook).

After every N assistant responses (default 10), outputs a paste-ready
continuation prompt for /clear to stderr. Does NOT block exit (exit 0).

State tracked in: .claude/checkpoint-state.local.json
Threshold: CLAUDE_CHECKPOINT_INTERVAL env var (default 10)
"""
import json
import os
import sys
from pathlib import Path


CHECKPOINT_STATE_FILE = ".claude/checkpoint-state.local.json"
DEFAULT_INTERVAL = 10


def _read_checkpoint_state(path: Path) -> dict:
    """Read or initialize checkpoint state."""
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"round_count": 0}


def _write_checkpoint_state(path: Path, state: dict):
    """Persist checkpoint state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _read_file_safe(path: Path) -> str:
    """Read file contents or return empty string."""
    try:
        return path.read_text() if path.exists() else ""
    except OSError:
        return ""


def _find_context_dir(project_dir: Path) -> Path | None:
    """Find the most recent context task directory."""
    ctx_dir = project_dir / ".claude" / "context"
    if not ctx_dir.is_dir():
        return None
    # Pick most recently modified subdirectory
    subdirs = [d for d in ctx_dir.iterdir() if d.is_dir()]
    if not subdirs:
        return None
    return max(subdirs, key=lambda d: d.stat().st_mtime)


def _extract_tasks(content: str) -> tuple[list[str], list[str]]:
    """Extract checked and unchecked items from markdown task list."""
    checked = []
    unchecked = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
            checked.append(stripped[5:].strip())
        elif stripped.startswith("- [ ]"):
            unchecked.append(stripped[5:].strip())
    return checked, unchecked


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    project_dir = Path(os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd())
    interval = int(os.getenv("CLAUDE_CHECKPOINT_INTERVAL", str(DEFAULT_INTERVAL)))

    state_path = project_dir / CHECKPOINT_STATE_FILE
    state = _read_checkpoint_state(state_path)

    # Increment round count
    state["round_count"] = state.get("round_count", 0) + 1
    count = state["round_count"]

    if count < interval:
        _write_checkpoint_state(state_path, state)
        return 0

    # Threshold reached -- generate checkpoint output then reset
    state["round_count"] = 0
    _write_checkpoint_state(state_path, state)

    # Gather context
    ctx_dir = _find_context_dir(project_dir)
    goal = ""
    key_context = ""
    completed = []
    remaining = []

    if ctx_dir:
        plan_content = _read_file_safe(ctx_dir / "plan.md")
        context_content = _read_file_safe(ctx_dir / "context.md")
        tasks_content = _read_file_safe(ctx_dir / "tasks.md")

        # Extract goal from first non-empty line of plan
        for line in plan_content.split("\n"):
            line = line.strip().lstrip("#").strip()
            if line:
                goal = line
                break

        key_context = context_content.strip()[:500] if context_content else ""
        completed, remaining = _extract_tasks(tasks_content)

    # Build output
    lines = [
        "",
        "=" * 50,
        f" CONTEXT CHECKPOINT (round {count})",
        "=" * 50,
    ]
    if goal:
        lines.append(f"TASK: {goal}")
    if completed:
        lines.append(f"COMPLETED: {'; '.join(completed[:5])}")
    if remaining:
        lines.append(f"REMAINING: {'; '.join(remaining[:5])}")
    if key_context:
        lines.append(f"KEY CONTEXT: {key_context[:200]}")

    lines.append("")
    lines.append("-" * 30 + " PASTE AFTER /clear " + "-" * 30)
    lines.append("Use the autopilot subagent.")
    lines.append("")
    if goal:
        lines.append(f"1) GOAL: {goal}")
    if remaining:
        lines.append(f"2) DEFINITION OF DONE: {'; '.join(remaining[:10])}")
    if key_context:
        lines.append(f"3) CONTEXT: {key_context[:300]}")
    if completed:
        lines.append(f"4) DETAILS: Already done: {'; '.join(completed[:5])}")
    lines.append("-" * 30 + " END " + "-" * 30)
    lines.append("")

    print("\n".join(lines), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
