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
import shlex
import sys

from _pathguard import is_protected, to_project_relative

data = json.load(sys.stdin)
cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""

PROJECT_DIR = os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd()

# Same escape hatch as protect_files.py: allow writes to protected paths.
ALLOW_PROTECTED_ENV = "CLAUDE_ALLOW_PROTECTED_EDITS"

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
# CRITICAL patterns: matched against the WHOLE command and NEVER promoted, even
# in autonomous mode. These are multi-segment attacks (download-then-execute,
# pipe-to-interpreter) or irreversible destructive operations. They must be
# checked on the full command because splitting on ; && || | would hide the
# relationship between the two halves (e.g. `curl ... | sh`).
# -----------------------------------------------------------------------------
CRITICAL_BLOCKED = [
    # Remote code execution (supply-chain attacks)
    (r"\bcurl\b.*\|\s*(sh|bash|zsh|python|python3|perl|ruby)\b", "curl pipe to interpreter"),
    (r"\bwget\b.*\|\s*(sh|bash|zsh|python|python3|perl|ruby)\b", "wget pipe to interpreter"),
    # download-then-execute via redirect: curl ... > f && sh
    (r"\bcurl\b.*>\s*[^|]+\s*(?:;|&&|\|\|)\s*(sh|bash|chmod\s+\+x)", "curl download and execute"),
    (r"\bwget\b.*(?:;|&&|\|\|)\s*(sh|bash|chmod\s+\+x)", "wget download and execute"),
    # download-then-execute via -o/-O FILE ... (; or &&) sh FILE
    (r"\bcurl\b[^\n]*\s-o\s+(\S+)[^\n]*(?:;|&&|\|\|)\s*(?:sh|bash|zsh)\s+\1\b",
     "curl download then execute file"),
    (r"\bwget\b[^\n]*\s-O\s+(\S+)[^\n]*(?:;|&&|\|\|)\s*(?:sh|bash|zsh)\s+\1\b",
     "wget download then execute file"),
    # Base64 decoding to shell (obfuscation technique)
    (r"base64\s+-d.*\|\s*(sh|bash)", "base64 decode to shell"),
    (r"echo\s+.*\|\s*base64\s+-d\s*\|\s*(sh|bash)", "echo base64 to shell"),
    # Irreversible / destructive
    (r"\bmkfs\b", "mkfs"),
    (r"\bdd\s+.*of=/dev/", "dd to device"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&?\s*;?\s*\}\s*;\s*:", "fork bomb"),
    (r"\bfind\b.*\s-delete\b", "find -delete"),
    (r"\bfind\b.*-exec\s+rm\b", "find -exec rm"),
    (r"\bfind\b.*-execdir\s+rm\b", "find -execdir rm"),
    (r"\bfind\b.*\|\s*xargs\s+rm\b", "find | xargs rm"),
]

# -----------------------------------------------------------------------------
# SEGMENT patterns: matched against each individual command segment (the command
# is split on ; && || | newline and $()/backtick boundaries). This closes the
# F4 chaining bypass -- `echo ok; sudo whoami` now blocks because the `sudo`
# segment is tested on its own. In autonomous mode a matching segment may still
# be promoted (see AUTONOMOUS_PROMOTED).
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
]


def split_segments(command: str) -> list:
    """Split a command into individual command segments.

    Splits on shell command separators (; && || | newline) and command
    substitution boundaries ($( ) and backticks) so that each dangerous
    sub-command is tested on its own, defeating chaining/substitution bypasses.
    """
    parts = re.split(r"\|\||&&|\$\(|[;&|\n()`]", command)
    return [p.strip() for p in parts if p.strip()]


# Leading `VAR=value` environment assignments and benign command wrappers are
# stripped so a dangerous command cannot hide behind them, e.g.
# `FOO=bar sudo whoami` or `env FOO=bar curl ...`. The wrapper/assignment itself
# is harmless; what matters is the command it ultimately runs, so segments are
# tested both raw and with the prefix removed (the raw check keeps prior matches
# intact). Wrappers here only ever consume assignments or dash-flags before the
# real command, so stripping them never drops a real argument.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_COMMAND_WRAPPERS = {
    "env", "command", "nohup", "setsid", "stdbuf", "ionice",
    "eval", "exec", "xargs",
}


def strip_command_prefix(segment: str) -> str:
    """Drop leading env-assignments and benign wrappers from a segment."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        toks = segment.split()
    i = 0
    changed = False
    while i < len(toks):
        tok = toks[i]
        if _ENV_ASSIGN_RE.match(tok):
            i += 1
            changed = True
            continue
        if os.path.basename(tok) in _COMMAND_WRAPPERS:
            i += 1
            changed = True
            # wrappers take dash-flags (env -i, ionice -c2) before the command
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
            continue
        break
    if not changed:
        return segment
    return " ".join(toks[i:])


def segment_variants(segment: str) -> list:
    """Return the raw segment plus its prefix-stripped form (when different)."""
    variants = [segment]
    stripped = strip_command_prefix(segment)
    if stripped and stripped != segment:
        variants.append(stripped)
    return variants


def _strip_quotes(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok


def extract_write_targets(command: str) -> list:
    """Return candidate write destinations from a bash command.

    Detects: > / >> redirection, `tee`, `dd of=`, `sed -i`, and the destination
    of `cp` / `mv`. Used to block Bash writes to protected paths (F8).
    """
    targets = []
    for seg in re.split(r"\|\||&&|[;&|\n]", command):
        seg = seg.strip()
        if not seg:
            continue

        # Output redirection: > file, >> file (skip fd dups like >&1 / 2>&1)
        for m in re.finditer(r">>?\s*([^\s;&|<>]+)", seg):
            tgt = _strip_quotes(m.group(1))
            if tgt and not tgt.startswith("&"):
                targets.append(tgt)

        # dd of=FILE
        for m in re.finditer(r"\bof=([^\s;&|]+)", seg):
            targets.append(_strip_quotes(m.group(1)))

        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        if not toks:
            continue

        for i, tok in enumerate(toks):
            base = os.path.basename(tok)
            rest = toks[i + 1:]
            if base == "tee":
                # every non-flag argument to tee is a write destination
                targets.extend(x for x in rest if not x.startswith("-"))
            elif base == "sed":
                if any(a == "-i" or a.startswith("-i") or a.startswith("--in-place")
                       for a in rest):
                    # in-place edit: any positional (incl. the script) is checked;
                    # only real protected paths will match is_protected().
                    targets.extend(x for x in rest if not x.startswith("-"))
            elif base in ("cp", "mv"):
                positionals = [x for x in rest if not x.startswith("-")]
                if len(positionals) >= 2:
                    targets.append(positionals[-1])  # destination

    return targets


def bash_write_to_protected(command: str):
    """Return the offending target if the command writes to a protected path."""
    if os.getenv(ALLOW_PROTECTED_ENV) == "1":
        return None
    for tgt in extract_write_targets(command):
        rel = to_project_relative(tgt, PROJECT_DIR)
        if is_protected(rel):
            return tgt
    return None

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

# Commit-authorship policy: always blocked regardless of mode.
for pattern, reason in [
    (r"Co-Authored-By", "Co-Authored-By in commit (policy: commits must appear as user's own)"),
    (r"git\s+commit\b.*--author", "--author flag (policy: commits must appear as user's own)"),
]:
    if re.search(pattern, cmd, re.IGNORECASE):
        print(f"BLOCKED: {reason}", file=sys.stderr)
        sys.exit(2)

# CRITICAL patterns are checked against the whole command and never promoted.
for pattern, name in CRITICAL_BLOCKED:
    if re.search(pattern, cmd, re.IGNORECASE):
        print(f"BLOCKED: '{name}' command not allowed. Pattern: {pattern}", file=sys.stderr)
        sys.exit(2)

# Autonomous commit-policy violations (whole command).
if AUTONOMOUS_MODE:
    for pattern, reason in AUTONOMOUS_BLOCKED:
        if re.search(pattern, cmd, re.IGNORECASE):
            print(f"BLOCKED: {reason}", file=sys.stderr)
            sys.exit(2)

# F8: block Bash writes (redirection/tee/dd/sed -i/cp/mv) to protected paths.
offending = bash_write_to_protected(cmd)
if offending is not None:
    print(
        f"BLOCKED: Bash write to protected path: {offending}\n"
        "This kit blocks writes to .env*, secret material, and prod config paths.\n"
        f"To override temporarily: export {ALLOW_PROTECTED_ENV}=1 (then restart Claude).",
        file=sys.stderr,
    )
    sys.exit(2)

# Per-segment checks: split on ; && || | newline and substitution boundaries so
# a dangerous sub-command cannot hide behind a benign leading command (F4).
for segment in split_segments(cmd):
    # An always-allowed segment is exempt (but only that segment).
    if is_always_allowed(segment):
        continue

    # Test the raw segment and its prefix-stripped form so a dangerous command
    # cannot hide behind leading env-assignments or wrappers (FOO=bar sudo ...).
    variants = segment_variants(segment)

    for pattern, name in blocked:
        if any(re.search(pattern, v, re.IGNORECASE) for v in variants):
            if AUTONOMOUS_MODE and any(is_autonomous_promoted(v) for v in variants):
                break
            print(f"BLOCKED: '{name}' command not allowed. Pattern: {pattern}", file=sys.stderr)
            sys.exit(2)

    for pattern, name, allowlist in blocked_supply_chain:
        if any(re.search(pattern, v, re.IGNORECASE) for v in variants):
            if any(is_allowlisted(v, allowlist) for v in variants):
                continue
            if AUTONOMOUS_MODE and any(is_autonomous_promoted(v) for v in variants):
                continue
            print(f"BLOCKED: '{name}' - add to allowlist in guard_bash.py if trusted", file=sys.stderr)
            sys.exit(2)

sys.exit(0)
