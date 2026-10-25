#!/usr/bin/env python3
"""
Browser safety hook for browser CLI commands run from Claude Code (agent-browser, browser-use).
Blocks navigation to payment/checkout/billing URLs.
Blocks form submissions to sensitive pages.
Allows read-only navigation, screenshots, and cookie operations.
"""
import json
import re
import sys

data = json.load(sys.stdin)
tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {}) or {}
cmd = tool_input.get("command", "") or ""

# Only check browser-related commands
BROWSER_CLI = r"(agent-browser|browser-use)"
if not re.search(BROWSER_CLI, cmd) and "browser" not in tool_name:
    sys.exit(0)

# URLs that indicate payment/checkout/billing pages
BLOCKED_URL_PATTERNS = [
    r"checkout",
    r"payment",
    r"billing",
    r"pay\..*\.(com|net|org)",
    r"stripe\.com",
    r"paypal\.com",
    r"/cart/",
    r"/order/",
    r"/purchase",
    r"/subscribe",
    r"/upgrade",
    r"bank\.",
    r"wallet\.",
]

# Blocked browser actions on sensitive pages
BLOCKED_ACTIONS = [
    (r"(agent-browser|browser-use)\s+(type|fill)\b.*password", "typing passwords via CLI (use the browser profile or vault instead)"),
    (r"(agent-browser|browser-use)\s+submit\b", "form submission (use click on specific buttons instead)"),
]

# Check for blocked URLs in navigate commands
if re.search(r"(agent-browser|browser-use)\s+(navigate|open|goto)\b", cmd, re.IGNORECASE):
    for pattern in BLOCKED_URL_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            print(f"BLOCKED: Navigation to payment/checkout URL detected. Pattern: {pattern}", file=sys.stderr)
            sys.exit(2)

# Check for blocked actions
for pattern, reason in BLOCKED_ACTIONS:
    if re.search(pattern, cmd, re.IGNORECASE):
        print(f"BLOCKED: {reason}", file=sys.stderr)
        sys.exit(2)

# Allow all other browser operations (snapshot, screenshot, cookie import/export, etc.)
sys.exit(0)
