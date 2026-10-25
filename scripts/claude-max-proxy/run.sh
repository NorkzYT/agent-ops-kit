#!/usr/bin/env bash
# run.sh — start claude-max-proxy on the host. ExecStart of the systemd user
# unit installed by scripts/claude-max-proxy/install.sh; runs with the
# environment from data/claude-max-proxy/proxy.env and REPOS_DIR as cwd (the
# Claude Code sessions the proxy spawns inherit that cwd).
#
# 1. No credentials yet.
#    The proxy runs `claude auth status` at boot and exits 1 when nothing is
#    logged in. Under Restart=always that is a restart loop, so with no token
#    and no credentials file we stay up and idle with a clear message.
#    `make auth-claude-proxy` stores the token and restarts the unit.
#
# 2. Uptime watchdog (CLAUDE_PROXY_MAX_UPTIME_HOURS).
#    Long-running Claude CLI subprocesses slowly leak memory and the OAuth
#    refresh can wedge after many hours. Upstream does not implement the
#    variable, so the kit does it here: after N hours the watchdog waits for
#    the proxy to be idle (no active or queued requests on /ops/snapshot), then
#    sends SIGTERM. The proxy drains, exits, and systemd starts it fresh.
#    Empty or 0 disables.
set -euo pipefail

PROXY_DIR="${1:?usage: run.sh <proxy checkout dir>}"
PORT="${CLAUDE_MAX_PROXY_PORT:-3456}"
creds="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"

[[ -f "$PROXY_DIR/dist/server/standalone.js" ]] || {
  echo "[claude-max-proxy] $PROXY_DIR is not built. Run: make claude-proxy-install" >&2
  exit 1
}

if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && ! -s "$creds" ]]; then
  echo "[claude-max-proxy] no Claude Max credentials yet; the proxy is NOT running."
  echo "[claude-max-proxy] Run:  make auth-claude-proxy"
  while [[ ! -s "$creds" ]]; do sleep 15; done
  echo "[claude-max-proxy] credentials found; starting."
fi

node "$PROXY_DIR/dist/server/standalone.js" "$PORT" &
proxy_pid=$!

# Forward stop signals so the proxy can drain in-flight work.
trap 'kill -TERM "$proxy_pid" 2>/dev/null' TERM INT

hours="${CLAUDE_PROXY_MAX_UPTIME_HOURS:-}"
case "$hours" in
  ''|0|*[!0-9]*) ;;
  *)
    (
      sleep "$((hours * 3600))"
      echo "[claude-max-proxy] uptime limit of ${hours}h reached; restarting once idle."
      # Wait until nothing is running or queued (checked every 30s, max 2h).
      i=0
      while [[ "$i" -lt 240 ]]; do
        snap="$(curl -fsS -m 5 "http://127.0.0.1:${PORT}/ops/snapshot?conversationLimit=1&logLimit=1" 2>/dev/null || echo '')"
        busy="$(printf '%s' "$snap" | node -e '
          let d = ""; process.stdin.on("data", c => d += c).on("end", () => {
            try { const q = JSON.parse(d).queue || {};
              const a = (q.activeRequests || []).length, w = q.queuedConversations || 0;
              process.stdout.write(a + w > 0 ? "1" : "0"); }
            catch { process.stdout.write("0"); } });' 2>/dev/null || echo 0)"
        [[ "$busy" == "0" ]] && break
        i=$((i + 1)); sleep 30
      done
      echo "[claude-max-proxy] restarting now."
      kill -TERM "$proxy_pid" 2>/dev/null
    ) &
    ;;
esac

set +e
wait "$proxy_pid"
rc=$?
# 0 or 143 (SIGTERM from the watchdog) both restart under Restart=always.
exit "$rc"
