#!/usr/bin/env python3
"""Quick functional test for guard_bash.py ALWAYS_ALLOWED + blocked patterns."""
import subprocess, json, sys, os

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "guard_bash.py")

tests = [
    # (command, should_be_blocked)
    # Dangerous commands - must be blocked
    ("sudo apt install foo", True),
    # Always-allowed commands - must pass
    ("bash .claude/bootstrap/analyze_repo.sh /tmp/repo", False),
    ("bash .claude/scripts/local-workflow.sh --repo /tmp", False),
    ("gh run list --branch feat --limit 1", False),
    ("gh pr view 123", False),
    ("gh pr checks 42", False),
    ("gh run view 12345 --log-failed", False),
    ("gh run watch 99", False),
    ("gh pr list --state open", False),
]

passed = 0
failed = 0
for cmd, should_block in tests:
    inp = json.dumps({"tool_input": {"command": cmd}})
    r = subprocess.run(
        ["python3", GUARD],
        input=inp, capture_output=True, text=True
    )
    blocked = r.returncode != 0
    ok = blocked == should_block
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {status}: {'BLOCK' if blocked else 'ALLOW'} cmd={cmd!r}")

print(f"\nResults: {passed} passed, {failed} failed")
sys.exit(1 if failed > 0 else 0)
