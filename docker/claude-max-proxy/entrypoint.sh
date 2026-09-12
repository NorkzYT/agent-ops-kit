#!/bin/sh
# claude-max-proxy entrypoint for agent-ops-kit.
#
# The proxy runs `claude auth status` at boot and exits 1 when nothing is
# logged in. Under `restart: unless-stopped` that is a crash loop, and a
# crash-looping container cannot be `exec`ed into to log in. So: if there are
# no credentials yet, stay up and idle with a clear message until either
#   - CLAUDE_CODE_OAUTH_TOKEN is set in .env (make auth-claude-proxy), which
#     recreates the container, or
#   - a `claude auth login` run inside the container writes .credentials.json.
# Then start the proxy.
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

exec node dist/server/standalone.js
