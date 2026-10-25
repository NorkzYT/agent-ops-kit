#!/usr/bin/env python3
"""Audit log of bash commands.

Hardening (F6):
  (a) Redact token/password/API-key-shaped substrings before writing, so
      secrets passed on the command line never land in the log.
  (b) Create the log directory 0700 and the log file 0600 (private), enforced
      in the hook itself regardless of how the kit was installed.
"""
import datetime
import json
import os
import re
import sys

# Patterns applied in order. Each keeps the identifying part and masks the
# secret value with <redacted>. Over-redaction is preferred to leakage.
_REDACTORS = [
    # Authorization header value (covers `Authorization: Bearer <x>` and
    # `-H "Authorization: ..."`). Stop at a closing quote or newline.
    (re.compile(r"(?i)(authorization:\s*)([^\"'\n]+)"), r"\1<redacted>"),
    # --password / --token / --secret / --api-key style flags.
    (re.compile(r"(?i)(--(?:password|passwd|token|secret|api[-_]?key)[=\s]+)(\S+)"),
     r"\1<redacted>"),
    # KEY= / TOKEN= / SECRET= / PASSWORD= assignments, incl. prefixed forms
    # like API_KEY=, AWS_SECRET_ACCESS_KEY=, MY_TOKEN=.
    (re.compile(r"(?i)\b((?:[A-Za-z0-9]+_)*(?:API_?KEY|ACCESS_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|KEY))=(\S+)"),
     r"\1=<redacted>"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _REDACTORS:
        text = pattern.sub(repl, text)
    return text


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # never break the session over a logging hook

    tool_input = data.get("tool_input", {}) or {}
    cmd = redact(tool_input.get("command", "") or "")
    desc = redact(tool_input.get("description", "") or "")

    project_dir = os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd()
    logs_dir = os.path.join(project_dir, ".claude", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    try:
        os.chmod(logs_dir, 0o700)  # private: only the owner may read the audit log
    except OSError:
        pass

    log_path = os.path.join(logs_dir, "bash.log")
    # Create with 0600 if missing; chmod covers the pre-existing-file case.
    fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(f"{ts} | {cmd} | {desc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
