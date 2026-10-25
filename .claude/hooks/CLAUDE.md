# Hooks Documentation

This directory contains hook implementations for Claude Code's event system.

## Hook Architecture

Hooks provide automated governance and safety controls:

```
User Prompt → PreToolUse → Tool Execution → PostToolUse → Response
                 ↓              ↓               ↓
              Block?        Monitor         Auto-format
```

## Available Hooks

### PreToolUse Hooks (Safety Valve)

| Hook | Matcher | Purpose |
|------|---------|---------|
| `protect_files.py` | Write, Edit, MultiEdit | Blocks edits to sensitive files (.env, secrets, certs) |
| `guard_bash.py` | Bash | Blocks dangerous commands (rm -rf, sudo, curl\|bash) |
| `log_bash.py` | Bash | Logs all bash commands for audit trail |

### PostToolUse Hooks (Auto-Chronicler)

| Hook | Matcher | Purpose |
|------|---------|---------|
| `format_if_configured.py` | Edit, MultiEdit, Write | Auto-formats edited files (Prettier/Black) |

### PostToolUseFailure Hooks

| Hook | Purpose |
|------|---------|
| `log_tool_failure.py` | Logs failed tool invocations for debugging |

### UserPromptSubmit Hooks (Context Injection)

| Hook | Purpose |
|------|---------|
| `log_prompt.py` | Logs user prompts for audit trail |
| `inject_context.py` | Dynamically injects relevant context based on prompt |

### Stop/SubagentStop Hooks (Session Cleanup)

| Hook | Purpose |
|------|---------|
| `log_assistant.py` | Logs assistant responses |
| `persist_session.py` | Saves session state to context directory |

### Notification Hooks

| Hook | Purpose |
|------|---------|
| `notify_linux.py` | Desktop notifications when Claude needs attention |

## Hook Configuration

Hooks are configured in `.claude/settings.local.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/protect_files.py\"",
            "timeout": 3
          }
        ]
      }
    ]
  }
}
```

## Hook Return Codes

- **Exit 0**: Allow the operation to proceed
- **Exit 2**: Block the operation (PreToolUse only)
- Any stderr output is shown to the user

## Creating New Hooks

Hooks receive JSON input on stdin with:
```json
{
  "tool_name": "Bash",
  "tool_input": {
    "command": "..."
  }
}
```

Example hook structure:

```python
#!/usr/bin/env python3
import json
import sys

data = json.load(sys.stdin)
tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})

# Your validation logic here

if should_block:
    print("BLOCKED: reason", file=sys.stderr)
    sys.exit(2)  # Exit 2 blocks the operation

sys.exit(0)  # Exit 0 allows the operation
```

## Sentinel Zone Integration

The `protect_files.py` hook enforces sentinel zones:
- Edit `PROTECTED_GLOBS` to add protected paths
- Edit `ALLOWED_PATTERNS` for safe exceptions
- Use `@sentinel` or `LEGACY_PROTECTED` comments in code for detection

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `CLAUDE_PROJECT_DIR` | Root of the project being worked on |
| `CLAUDE_SESSION_ID` | Current session identifier |
| `CLAUDE_ALLOW_PROTECTED_EDITS` | Override to allow protected file edits |

## Extending Guard Bash

To allowlist specific packages in `guard_bash.py`:

```python
ALLOWLISTED_NPX = [
    r"^npx\s+prettier\b",
    r"^npx\s+eslint\b",
]
```

