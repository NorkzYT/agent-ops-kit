#!/usr/bin/env python3
"""
Cross-platform notification hook for Claude Code.

Supports multiple notification backends (configure via environment variables):

1. ntfy.sh (DEFAULT - always used, free, recommended):
   export CLAUDE_NTFY_TOPIC="your-unique-topic-name"
   # If not set, defaults to "claude-code-{hostname}"
   # Subscribe at https://ntfy.sh/your-topic-name or install the app

2. Pushover (paid, reliable):
   export CLAUDE_PUSHOVER_USER="your-user-key"
   export CLAUDE_PUSHOVER_TOKEN="your-app-token"

3. Discord webhook:
   export CLAUDE_DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."

4. Slack webhook:
   export CLAUDE_SLACK_WEBHOOK="https://hooks.slack.com/services/..."

Note: Desktop notifications (notify-send) are disabled by default.
      Set CLAUDE_NOTIFY_DESKTOP=1 to enable them.

Set CLAUDE_NOTIFY_DISABLE=1 to disable all notifications.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path


def get_default_ntfy_topic() -> str:
    """Generate a default ntfy topic based on hostname."""
    hostname = platform.node() or "unknown"
    # Sanitize hostname for use as ntfy topic (alphanumeric and hyphens only)
    sanitized = "".join(c if c.isalnum() or c == "-" else "-" for c in hostname.lower())
    return f"claude-code-{sanitized}"


def ensure_logs_dir(project_dir: str) -> Path:
    logs_dir = Path(project_dir) / ".claude" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def send_ntfy(topic: str, title: str, body: str, logs_dir: Path = None) -> bool:
    """Send notification via ntfy.sh"""
    try:
        url = f"https://ntfy.sh/{topic}"
        data = body.encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Title": title,
                "Priority": "high" if "Permission" in title else "default",
                "Tags": "robot",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        if logs_dir:
            with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().isoformat()}Z ntfy error: {e}\n")
        return False


def send_pushover(user: str, token: str, title: str, body: str, logs_dir: Path = None) -> bool:
    """Send notification via Pushover"""
    try:
        data = urllib.parse.urlencode({
            "token": token,
            "user": user,
            "title": title,
            "message": body,
            "priority": 1 if "Permission" in title else 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=data,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        if logs_dir:
            with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().isoformat()}Z pushover error: {e}\n")
        return False


def send_discord(webhook_url: str, title: str, body: str, logs_dir: Path = None) -> bool:
    """Send notification via Discord webhook"""
    try:
        payload = json.dumps({
            "embeds": [{
                "title": title,
                "description": body,
                "color": 0xFF6600 if "Permission" in title else 0x00FF00,
            }]
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        if logs_dir:
            with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().isoformat()}Z discord error: {e}\n")
        return False


def send_slack(webhook_url: str, title: str, body: str, logs_dir: Path = None) -> bool:
    """Send notification via Slack webhook"""
    try:
        payload = json.dumps({
            "text": f"*{title}*\n{body}",
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        if logs_dir:
            with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().isoformat()}Z slack error: {e}\n")
        return False


def is_display_available() -> bool:
    """Check if X11 DISPLAY or Wayland display is available for GUI notifications."""
    # Check for X11 display
    display = os.getenv("DISPLAY")
    if display:
        return True
    # Check for Wayland display
    wayland_display = os.getenv("WAYLAND_DISPLAY")
    if wayland_display:
        return True
    return False


def send_terminal_bell() -> bool:
    """Send a terminal bell as a fallback notification."""
    try:
        # Send BEL character to stderr (works in most terminals)
        sys.stderr.write("\a")
        sys.stderr.flush()
        return True
    except Exception:
        return False


def send_notify_send(title: str, body: str, logs_dir: Path = None) -> bool:
    """Send notification via Linux notify-send"""
    # Check if display is available (X11 or Wayland)
    if not is_display_available():
        if logs_dir:
            with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().isoformat()}Z notify-send: skipped (no DISPLAY/WAYLAND_DISPLAY - headless environment)\n")
        # Try terminal bell as fallback
        send_terminal_bell()
        return False

    notify_send = shutil.which("notify-send")
    if not notify_send:
        if logs_dir:
            with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().isoformat()}Z notify-send: not found\n")
        return False
    try:
        result = subprocess.run(
            [notify_send, title, body],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if logs_dir:
                with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
                    f.write(f"{datetime.utcnow().isoformat()}Z notify-send failed: {result.stderr.strip()}\n")
            return False
        return True
    except Exception as e:
        if logs_dir:
            with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().isoformat()}Z notify-send error: {e}\n")
        return False


def main() -> int:
    # Disable all notifications if requested
    if os.getenv("CLAUDE_NOTIFY_DISABLE", "").lower() in ("1", "true"):
        return 0

    project_dir = os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd()
    logs_dir = ensure_logs_dir(project_dir)

    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.utcnow().isoformat()}Z notify: invalid JSON: {e}\n")
        return 0

    notif_type = payload.get("notification_type", "")
    message = payload.get("message", "")
    cwd = payload.get("cwd", "")

    # Build terminal identifier for multi-terminal disambiguation
    session_id = os.getenv("CLAUDE_SESSION_ID", "")
    short_session = session_id[:8] if session_id else ""

    # Try to get memorable terminal name from terminal identity hook
    terminal_name = ""
    identity_path = Path(project_dir) / ".claude" / "terminal-identity.local.json"
    if identity_path.exists():
        try:
            identity_data = json.loads(identity_path.read_text(encoding="utf-8"))
            terminal_name = identity_data.get("name", "")
        except Exception:
            pass

    task_label = ""

    # Prefer terminal_name over short_session for the tag
    terminal_tag = ""
    if terminal_name:
        terminal_tag = f" [{terminal_name}]"
    elif short_session:
        terminal_tag = f" [{short_session}]"
    if task_label:
        terminal_tag = f" [{terminal_name or short_session}] {task_label[:40]}"

    # Only notify for important events
    if notif_type == "permission_prompt":
        title = f"Claude Code: Permission required{terminal_tag}"
    elif notif_type == "idle_prompt":
        title = f"Claude Code: Waiting for input{terminal_tag}"
    else:
        return 0

    body = message.strip()
    if cwd:
        # Shorten path for readability
        short_cwd = cwd.replace(os.path.expanduser("~"), "~")
        body = f"{body}\n({short_cwd})"
    if terminal_name and task_label:
        body = f"Terminal: {terminal_name} | Task: {task_label}\n{body}"
    elif terminal_name:
        body = f"Terminal: {terminal_name}\n{body}"
    elif short_session and task_label:
        body = f"Session: {short_session} | Task: {task_label}\n{body}"
    elif short_session:
        body = f"Session: {short_session}\n{body}"

    # Log regardless of notification success
    with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().isoformat()}Z [{notif_type}] {body}\n")

    # Try notification backends in order of preference
    sent = False
    backends_tried = []

    # 1. ntfy.sh (DEFAULT - always used)
    # Check env var first, then config file, then use default based on hostname
    ntfy_topic = os.getenv("CLAUDE_NTFY_TOPIC")
    if not ntfy_topic:
        ntfy_config = Path.home() / ".config" / "claude-code" / "ntfy_topic"
        if ntfy_config.exists():
            ntfy_topic = ntfy_config.read_text().strip()
    if not ntfy_topic:
        # Use hostname-based default topic
        ntfy_topic = get_default_ntfy_topic()

    backends_tried.append("ntfy")
    sent = send_ntfy(ntfy_topic, title, body, logs_dir)

    # 2. Pushover
    pushover_user = os.getenv("CLAUDE_PUSHOVER_USER")
    pushover_token = os.getenv("CLAUDE_PUSHOVER_TOKEN")
    if pushover_user and pushover_token and not sent:
        backends_tried.append("pushover")
        sent = send_pushover(pushover_user, pushover_token, title, body, logs_dir)

    # 3. Discord webhook
    discord_webhook = os.getenv("CLAUDE_DISCORD_WEBHOOK")
    if discord_webhook and not sent:
        backends_tried.append("discord")
        sent = send_discord(discord_webhook, title, body, logs_dir)

    # 4. Slack webhook
    slack_webhook = os.getenv("CLAUDE_SLACK_WEBHOOK")
    if slack_webhook and not sent:
        backends_tried.append("slack")
        sent = send_slack(slack_webhook, title, body, logs_dir)

    # 5. Linux desktop (opt-in only - disabled by default)
    enable_desktop = os.getenv("CLAUDE_NOTIFY_DESKTOP", "").lower() in ("1", "true")
    if enable_desktop and not sent:
        backends_tried.append("notify-send")
        sent = send_notify_send(title, body, logs_dir)

    # Log warning if no notification was sent
    if not sent:
        with (logs_dir / "notifications.log").open("a", encoding="utf-8") as f:
            if not backends_tried:
                f.write(f"{datetime.utcnow().isoformat()}Z WARNING: No notification backend configured. "
                        "Set CLAUDE_NTFY_TOPIC or run: bash .claude/bootstrap/linux_devtools.sh\n")
            else:
                f.write(f"{datetime.utcnow().isoformat()}Z WARNING: Notification failed via: {', '.join(backends_tried)}\n")

    return 0


if __name__ == "__main__":
    # Import urllib.parse here to avoid issues if not needed
    import urllib.parse
    raise SystemExit(main())
