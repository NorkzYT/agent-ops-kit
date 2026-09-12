#!/usr/bin/env python3
"""
Guard hook to block dangerous bash commands.
Matches deny patterns from .claude/settings.local.json

Supply-chain security: blocks npx, curl|bash, and other remote code execution
patterns that are common attack vectors in agent workflows.
See: https://www.aikido.dev/blog/agent-skills-spreading-hallucinated-npx-commands
"""
import json
import os
import re
import sys

data = json.load(sys.stdin)
cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""

# Autonomous mode: activated by AGENT_AUTONOMOUS=1 env var (set by cron/Hermes wrappers only)
# Selectively promotes specific commands from blocked to allowed
AUTONOMOUS_MODE = os.getenv("AGENT_AUTONOMOUS", "0") == "1"

# -----------------------------------------------------------------------------
# Allowlist: explicitly permitted npx/pip/npm commands
# Add packages here that you trust and want to allow
# -----------------------------------------------------------------------------
ALLOWLISTED_NPX = [
    # Example: r"^npx\s+prettier\b",
    # Example: r"^npx\s+eslint\b",
]

ALLOWLISTED_PIP = [
    # Example: r"^pip\s+install\s+pytest\b",
]

ALLOWLISTED_NPM = [
    # Example: r"^npm\s+install\s+--save-dev\s+typescript\b",
]

# -----------------------------------------------------------------------------
# Always allowed: safe commands that should never be blocked regardless of mode
# These are read-only or project-internal scripts with no side effects
# -----------------------------------------------------------------------------
ALWAYS_ALLOWED = [
    # Local workflow execution (project-internal scripts)
    r"^\s*bash\s+.*\.claude/scripts/local-workflow\.sh\b",
    r"^\s*bash\s+.*\.claude/bootstrap/analyze_repo\.sh\b",
    # CI/CD monitoring (read-only GitHub CLI)
    r"^\s*gh\s+run\s+(list|view|watch)\b",
    r"^\s*gh\s+pr\s+(view|list|checks)\b",
]

# Commands promoted from blocked to allowed in autonomous mode only
# These are safe for unattended operation on feature branches
AUTONOMOUS_PROMOTED = [
    # Git operations (feature branches only)
    r"^\s*git\s+add\b",
    r"^\s*git\s+stage\b",
    r"^\s*git\s+commit\b(?!.*--amend\s+(main|master))",
    # Network downloads (no pipe-to-interpreter)
    r"^\s*curl\s+(?!.*\|\s*(sh|bash|zsh|python))",
    r"^\s*wget\s+(?!.*\|\s*(sh|bash|zsh|python))",
    # Package managers for project setup
    r"^\s*npm\s+install\b",
    r"^\s*npm\s+i\b",
    r"^\s*pip3?\s+install\b",
    # Git push to feature branches only (block main/master)
    r"^\s*git\s+push\b(?!.*\b(main|master)\b)",
    # CI/CD monitoring (read-only)
    r"^\s*gh\s+run\s+(list|view|watch)\b",
    r"^\s*gh\s+pr\s+(create|view|list|checks)\b",
    # Local workflow execution
    r"^\s*bash\s+.*local-workflow\.sh\b",
    r"^\s*bash\s+.*analyze_repo\.sh\b",
]

# Patterns blocked even in autonomous mode (commit policy enforcement)
AUTONOMOUS_BLOCKED = [
    # NEVER allow Co-Authored-By in commit messages -- commits must appear as the user's own
    (r"Co-Authored-By", "Co-Authored-By in commit (policy: commits must appear as user's own)"),
    # No --author override
    (r"git\s+commit\b.*--author", "--author flag (policy: commits must appear as user's own)"),
    # No amend on main/master
    (r"git\s+commit\b.*--amend", "--amend (policy: no amending in autonomous mode)"),
    # No push to main/master
    (r"git\s+push\b.*\b(main|master)\b", "push to main/master (policy: feature branches only)"),
    # No force push
    (r"git\s+push\b.*--force", "force push (policy: never force push)"),
]


def is_always_allowed(cmd: str) -> bool:
    """Check if command matches an always-allowed pattern."""
    for pattern in ALWAYS_ALLOWED:
        if re.search(pattern, cmd, re.IGNORECASE):
            return True
    return False


def is_autonomous_promoted(cmd: str) -> bool:
    """Check if command matches an autonomous promoted pattern."""
    for pattern in AUTONOMOUS_PROMOTED:
        if re.search(pattern, cmd, re.IGNORECASE):
            return True
    return False


def is_allowlisted(cmd: str, allowlist: list) -> bool:
    """Check if command matches any allowlist pattern."""
    for pattern in allowlist:
        if re.search(pattern, cmd, re.IGNORECASE):
            return True
    return False

# -----------------------------------------------------------------------------
# Blocked patterns
# -----------------------------------------------------------------------------
blocked = [
    # Destructive file operations
    (r"\brm\s+-rf\b", "rm -rf"),
    (r"\brm\s+-r\b", "rm -r"),
    (r"^\s*rm\s+", "rm"),
    (r"\bdel\b", "del"),
    (r"\brmdir\b", "rmdir"),

    # Privilege escalation
    (r"^\s*sudo\s+", "sudo"),
    (r"^\s*doas\s+", "doas"),

    # Network/remote commands (potential data exfiltration)
    (r"^\s*curl\s+", "curl"),
    (r"^\s*wget\s+", "wget"),
    (r"^\s*ssh\s+", "ssh"),
    (r"^\s*scp\s+", "scp"),
    (r"^\s*rsync\s+", "rsync"),

    # Git commit (prevent auto-commits)
#   (r"\bgit\s+commit\b", "git commit"),

    # Git staging (prevent auto-staging - files should not be staged automatically)
#   (r"\bgit\s+add\b", "git add"),
#   (r"\bgit\s+stage\b", "git stage"),

    # Windows shells
    (r"\bpowershell\b", "powershell"),
    (r"\bcmd\.exe\b", "cmd.exe"),

    # Destructive find commands
    (r"\bfind\b.*\s-delete\b", "find -delete"),
    (r"\bfind\b.*-exec\s+rm\b", "find -exec rm"),
    (r"\bfind\b.*-execdir\s+rm\b", "find -execdir rm"),
    (r"\bfind\b.*\|\s*xargs\s+rm\b", "find | xargs rm"),

    # Additional dangerous patterns
    (r"\bmkfs\b", "mkfs"),
    (r"\bdd\s+.*of=/dev/", "dd to device"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*;\s*\}\s*;\s*:", "fork bomb"),

    # Remote code execution patterns (supply-chain attacks)
    (r"\bcurl\b.*\|\s*(sh|bash|zsh|python|python3|perl|ruby)\b", "curl pipe to interpreter"),
    (r"\bwget\b.*\|\s*(sh|bash|zsh|python|python3|perl|ruby)\b", "wget pipe to interpreter"),
    (r"\bcurl\b.*>\s*[^|]+\s*&&\s*(sh|bash|chmod\s+\+x)", "curl download and execute"),
    (r"\bwget\b.*&&\s*(sh|bash|chmod\s+\+x)", "wget download and execute"),

    # Base64 decoding to shell (obfuscation technique)
    (r"base64\s+-d.*\|\s*(sh|bash)", "base64 decode to shell"),
    (r"echo\s+.*\|\s*base64\s+-d\s*\|\s*(sh|bash)", "echo base64 to shell"),
]

# Supply-chain: npx commands (hallucinated package attacks)
# Block by default unless explicitly allowlisted
blocked_supply_chain = [
    (r"^\s*npx\s+", "npx (supply-chain risk)", ALLOWLISTED_NPX),
    (r"\|\s*npx\s+", "pipe to npx (supply-chain risk)", ALLOWLISTED_NPX),

    # npm install with unknown packages (can execute postinstall scripts)
    (r"^\s*npm\s+install\s+(?!--save-dev\s+@types/)", "npm install (postinstall risk)", ALLOWLISTED_NPM),
    (r"^\s*npm\s+i\s+", "npm i (postinstall risk)", ALLOWLISTED_NPM),

    # pip install from URLs or git repos (arbitrary code execution)
    (r"^\s*pip\s+install\s+(https?://|git\+)", "pip install from URL/git", ALLOWLISTED_PIP),
    (r"^\s*pip3?\s+install\s+(https?://|git\+)", "pip install from URL/git", ALLOWLISTED_PIP),

    # pip install without version pinning (can pull malicious versions)
    (r"^\s*pip3?\s+install\s+(?!-r\s+requirements)(?!-e\s+\.)(?!--upgrade\s+pip)", "pip install (unvetted)", ALLOWLISTED_PIP),
]

# Check autonomous mode policy violations first (always blocked regardless)
for pattern, reason in [
    (r"Co-Authored-By", "Co-Authored-By in commit (policy: commits must appear as user's own)"),
    (r"git\s+commit\b.*--author", "--author flag (policy: commits must appear as user's own)"),
]:
    if re.search(pattern, cmd, re.IGNORECASE):
        print(f"BLOCKED: {reason}", file=sys.stderr)
        sys.exit(2)

if AUTONOMOUS_MODE:
    for pattern, reason in AUTONOMOUS_BLOCKED:
        if re.search(pattern, cmd, re.IGNORECASE):
            print(f"BLOCKED: {reason}", file=sys.stderr)
            sys.exit(2)

# Check always-allowed patterns first (bypass all other checks)
if is_always_allowed(cmd):
    sys.exit(0)

# Check standard blocked patterns
for pattern, name in blocked:
    if re.search(pattern, cmd, re.IGNORECASE):
        # In autonomous mode, check if this command is promoted
        if AUTONOMOUS_MODE and is_autonomous_promoted(cmd):
            continue
        print(f"BLOCKED: '{name}' command not allowed. Pattern: {pattern}", file=sys.stderr)
        sys.exit(2)

# Check supply-chain patterns (with allowlist support)
for pattern, name, allowlist in blocked_supply_chain:
    if re.search(pattern, cmd, re.IGNORECASE):
        if not is_allowlisted(cmd, allowlist):
            # In autonomous mode, npm install and pip install are promoted
            if AUTONOMOUS_MODE and is_autonomous_promoted(cmd):
                continue
            print(f"BLOCKED: '{name}' - add to allowlist in guard_bash.py if trusted", file=sys.stderr)
            sys.exit(2)

sys.exit(0)
