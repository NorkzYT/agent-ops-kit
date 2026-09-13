#!/bin/sh
# claude-max-proxy entrypoint for agent-ops-kit.
#
# 1. First boot without credentials.
#    The proxy runs `claude auth status` at boot and exits 1 when nothing is
#    logged in. Under `restart: unless-stopped` that is a crash loop, and a
#    crash-looping container cannot be `exec`ed into to log in. So: if there
#    are no credentials yet, stay up and idle with a clear message until either
#      - CLAUDE_CODE_OAUTH_TOKEN is set in .env (make auth-claude-proxy), which
#        recreates the container, or
#      - a `claude auth login` run inside the container writes .credentials.json.
#
# 2. Uptime watchdog (CLAUDE_PROXY_MAX_UPTIME_HOURS).
#    Long-running Claude CLI subprocesses slowly leak memory and the OAuth
#    refresh can wedge after many hours. Upstream `main` does not implement
#    the variable, so the kit does it here: after N hours the watchdog waits
#    for the proxy to be idle (no active or queued requests on /ops/snapshot),
#    then sends SIGTERM. The proxy shuts down gracefully and Docker's
#    `restart: unless-stopped` starts it fresh. Empty or 0 disables.
set -eu

creds="${HOME:-/home/node}/.claude/.credentials.json"

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ ! -s "$creds" ]; then
  echo "[claude-max-proxy] no Claude Max credentials yet; the proxy is NOT running."
  echo "[claude-max-proxy] On the host run:  make auth-claude-proxy"
  while [ ! -s "$creds" ]; do
    sleep 15
  done
  echo "[claude-max-proxy] credentials found; starting."
fi

node dist/server/standalone.js &
proxy_pid=$!

# Forward container stop signals to the proxy so it can drain in-flight work.
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
      while [ "$i" -lt 240 ]; do
        snap="$(curl -fsS -m 5 'http://127.0.0.1:3456/ops/snapshot?conversationLimit=1&logLimit=1' 2>/dev/null || echo '')"
        busy="$(printf '%s' "$snap" | node -e '
          let d = ""; process.stdin.on("data", c => d += c).on("end", () => {
            try { const q = JSON.parse(d).queue || {};
              const a = (q.activeRequests || []).length, w = q.queuedConversations || 0;
              process.stdout.write(a + w > 0 ? "1" : "0"); }
            catch { process.stdout.write("0"); } });' 2>/dev/null || echo 0)"
        [ "$busy" = "0" ] && break
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
# A watchdog-triggered exit (0 or 143) still restarts under unless-stopped.
exit "$rc"
