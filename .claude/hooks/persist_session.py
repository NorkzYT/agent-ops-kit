#!/usr/bin/env python3
"""
Session state persistence hook.
Triggered on Stop event to save session context to .claude/context/<task>/ directory.

Three-File Pattern:
- plan.md: High-level architectural plan (rarely updated)
- context.md: Key learnings, decisions, gotchas (updated each session)
- tasks.md: Granular checklist (updated frequently)
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def get_context_dir() -> Path:
    """Get or create the context directory for the current task."""
    project_dir = os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd()
    session_id = os.getenv("CLAUDE_SESSION_ID", "default")

    # Use session ID or a default task name
    task_name = os.getenv("CLAUDE_TASK_NAME", session_id[:8] if session_id != "default" else "current")

    context_dir = Path(project_dir) / ".claude" / "context" / task_name
    context_dir.mkdir(parents=True, exist_ok=True)

    return context_dir


def ensure_three_files(context_dir: Path) -> dict:
    """Ensure all three pattern files exist with templates."""
    files = {}

    # plan.md template
    plan_file = context_dir / "plan.md"
    if not plan_file.exists():
        plan_file.write_text("""# Plan

## Goal
[One-sentence objective]

## Approach
[Key technical decisions]

## Scope
- In scope: ...
- Out of scope: ...

## Milestones
1. [ ] ...
2. [ ] ...
3. [ ] ...
""")
    files["plan"] = plan_file

    # context.md template
    context_file = context_dir / "context.md"
    if not context_file.exists():
        context_file.write_text("""# Context

## Key Learnings
- ...

## Decisions Made
- ...

## Gotchas
- ...

## File Map
- ...

---
## Session History
""")
    files["context"] = context_file

    # tasks.md template
    tasks_file = context_dir / "tasks.md"
    if not tasks_file.exists():
        tasks_file.write_text("""# Tasks

## Current
- [ ] ...

## Blocked
- [ ] ...

## Completed
- [x] ...

## Deferred
- [ ] ...
""")
    files["tasks"] = tasks_file

    return files


def append_session_summary(context_file: Path, summary: str):
    """Append session summary to context.md."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    with open(context_file, "a") as f:
        f.write(f"\n### Session {timestamp}\n")
        f.write(summary + "\n")




def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # No input or invalid JSON - just ensure files exist
        payload = {}

    # Get context directory and ensure three-file pattern
    context_dir = get_context_dir()
    task_name = context_dir.name  # Extract task name from directory path
    files = ensure_three_files(context_dir)

    # Extract any session summary from payload
    # The Stop hook receives conversation_id and potentially other metadata
    conversation_id = payload.get("conversation_id", "")

    # If there's transcript or summary info, append to context
    transcript = payload.get("transcript", "")
    if transcript:
        # Extract a brief summary (first 500 chars or so)
        summary = transcript[:500] + "..." if len(transcript) > 500 else transcript
        append_session_summary(files["context"], f"Session ended. Brief: {summary}")
    else:
        # Just note the session end
        append_session_summary(files["context"], "Session ended.")

    # Log where state was persisted
    print(f"Session state persisted to: {context_dir}", file=sys.stderr)


    return 0


if __name__ == "__main__":
    raise SystemExit(main())
