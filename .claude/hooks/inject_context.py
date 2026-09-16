#!/usr/bin/env python3
"""
Dynamic context injection hook.
Triggered on UserPromptSubmit to inject relevant context based on the prompt.

Implements Just-in-Time context loading:
- Detects task type from prompt keywords
- Injects relevant reference pointers
- Loads session state if continuing a task
"""
import json
import os
import sys
from pathlib import Path
from typing import Optional


# Keyword to context mapping
CONTEXT_TRIGGERS = {
    # Security-related prompts
    "security": [
        "For security tasks, spawn `security-auditor` agent.",
        "Check `.claude/docs/sentinel-zones.md` for protected areas.",
    ],
    "auth": [
        "Authentication code is in sentinel zones - requires approval to modify.",
        "Spawn `security-auditor` for auth changes.",
    ],
    "secret": [
        "Secret/credential files are protected. See `.claude/docs/sentinel-zones.md`.",
    ],

    # Testing-related prompts
    "test": [
        "For test implementation, spawn `test-automator` or `tdd-orchestrator` agent.",
    ],
    "debug": [
        "For debugging, spawn `triage` or `debugger` agent.",
        "Check `.claude/logs/` for recent activity.",
    ],

    # Architecture-related prompts
    "architect": [
        "For architecture decisions, spawn `architect-review` or `backend-architect` agent.",
    ],
    "refactor": [
        "For refactoring, spawn `legacy-modernizer` agent.",
        "Check for `@sentinel` markers before modifying.",
    ],

    # Performance-related prompts
    "performance": [
        "For performance work, spawn `performance-engineer` agent.",
    ],
    "optimize": [
        "For optimization, first profile to identify bottlenecks.",
        "Spawn `performance-engineer` for systematic optimization.",
    ],

    # Review-related prompts
    "review": [
        "For code review, spawn `surgical-reviewer` or `code-reviewer` agent.",
    ],
    "pr": [
        "For PR preparation, spawn `closer` agent.",
        "Use the three-file pattern in `.claude/context/` for session state.",
    ],

    # Browser and automation prompts
    "scrape": [
        "For scraping tasks, read `.claude/skills/browser-automation/SKILL.md`.",
        "Browser work runs through Hermes browser tools; follow the login and read-only rules there.",
    ],
    "browser": [
        "For browser tasks, read `.claude/skills/browser-automation/SKILL.md`.",
    ],
    "verify": [
        "For data verification, compare stored data against the live page with the browser tools; read `.claude/skills/browser-automation/SKILL.md`.",
    ],
    "har": [
        "For API discovery, capture network traffic with the browser tools and document endpoints in context.md (see hermes/skills/autonomous-engineering).",
    ],
    "autonomous": [
        "Autonomous mode docs: `hermes/skills/autonomous-engineering/SKILL.md`.",
        "Report results via Discord. Persist session state to context files.",
        "Git policy: feature branches only. NEVER include Co-Authored-By lines -- commits must appear as the user's own.",
    ],
    "extension": [
        "For extension testing, run a headed browser session (browser.headed: true in Hermes) and load the unpacked extension.",
    ],
}

# Task continuation patterns
CONTINUATION_KEYWORDS = [
    "continue",
    "resume",
    "pick up",
    "where we left",
    "last time",
    "previous session",
]


def detect_context_needs(prompt: str) -> list:
    """Detect which context snippets to inject based on prompt keywords."""
    prompt_lower = prompt.lower()
    snippets = []

    for keyword, context_lines in CONTEXT_TRIGGERS.items():
        if keyword in prompt_lower:
            snippets.extend(context_lines)

    return list(set(snippets))  # Dedupe


def detect_task_continuation(prompt: str) -> Optional[str]:
    """Check if user is continuing a previous task and find context."""
    prompt_lower = prompt.lower()

    for keyword in CONTINUATION_KEYWORDS:
        if keyword in prompt_lower:
            # Look for existing context directories
            project_dir = os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd()
            context_base = Path(project_dir) / ".claude" / "context"

            if context_base.exists():
                # Find most recently modified context directory
                contexts = list(context_base.iterdir())
                if contexts:
                    latest = max(contexts, key=lambda p: p.stat().st_mtime)
                    return str(latest)

    return None


def build_injection(prompt: str) -> Optional[str]:
    """Build context injection based on prompt analysis."""
    snippets = detect_context_needs(prompt)
    continuation_dir = detect_task_continuation(prompt)

    if not snippets and not continuation_dir:
        return None

    injection_parts = []

    # Add context snippets
    if snippets:
        injection_parts.append("**Relevant Context:**")
        for snippet in snippets[:5]:  # Limit to 5 snippets
            injection_parts.append(f"- {snippet}")

    # Add task continuation info
    if continuation_dir:
        injection_parts.append("")
        injection_parts.append(f"**Previous session state found at:** `{continuation_dir}`")
        injection_parts.append("Read plan.md, context.md, and tasks.md to resume.")

    if injection_parts:
        return "\n".join(injection_parts)

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # No input, nothing to inject

    prompt = payload.get("prompt", "")
    if not prompt:
        return 0

    injection = build_injection(prompt)

    if injection:
        # Output plain text to stdout - gets added as context
        print(injection)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
