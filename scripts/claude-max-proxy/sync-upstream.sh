#!/usr/bin/env bash
#
# sync-upstream.sh — refresh the vendored claude-max-api-proxy sources.
#
# The proxy source tree lives IN this repo (vendor/claude-max-api-proxy, plain
# files, no nested .git) so the kit keeps working even if the upstream GitHub
# repo goes away. vendor/claude-max-api-proxy/UPSTREAM records which upstream
# commit the tree is. This script:
#
#   --check   (make claude-proxy-install)  never modifies the tree. Asks
#             upstream for its head; says "current", "newer upstream: run
#             make claude-proxy-update", or "unreachable: using vendored copy".
#   (none)    (make claude-proxy-update, make update)  if upstream is
#             reachable AND newer, replaces the vendored tree with a fresh
#             clone of it and rewrites UPSTREAM. The change is left for you
#             to review and commit. If upstream is unreachable, deleted, or
#             the vendored tree has uncommitted edits, it does nothing and the
#             build uses what the repo already has.
#
# Always exits 0 on network trouble so installs and updates work offline.
#
# Usage: sync-upstream.sh [--check] [proxy-dir]
# Env:   CLAUDE_MAX_PROXY_DIR   (default ./vendor/claude-max-api-proxy)
#        CLAUDE_MAX_PROXY_REPO  (default the url= line of UPSTREAM)
#        CLAUDE_MAX_PROXY_REF   (default the ref= line of UPSTREAM, i.e. main)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../lib.sh
. "$ROOT/scripts/lib.sh"

MODE=""
[[ "${1:-}" == "--check" ]] && { MODE="--check"; shift; }
PROXY_DIR="${1:-${CLAUDE_MAX_PROXY_DIR:-$ROOT/vendor/claude-max-api-proxy}}"
case "$PROXY_DIR" in /*) ;; *) PROXY_DIR="$ROOT/${PROXY_DIR#./}" ;; esac
PIN="$PROXY_DIR/UPSTREAM"
# Files in the vendored dir that are NOT upstream's and must survive a refresh.
KEEP=(UPSTREAM node_modules dist .agent-ops-kit-built .env)
# Upstream paths not copied in (their CI, templates; not needed to build).
EXCLUDE=(.git .github)

TAG="sync-proxy"
say() { echo "[$TAG] $*"; }
pinned() { env_file_get "$1" "$PIN" 2>/dev/null || true; }

# Never hang on a credential prompt (a deleted repo answers 404, a private
# one asks for a login).
export GIT_TERMINAL_PROMPT=0
net() { if have timeout; then timeout 30 "$@"; else "$@"; fi; }

REPO_URL="${CLAUDE_MAX_PROXY_REPO:-$(pinned url)}"
REF="${CLAUDE_MAX_PROXY_REF:-$(pinned ref)}"
REPO_URL="${REPO_URL:-https://github.com/mattschwen/claude-max-api-proxy.git}"
REF="${REF:-main}"

write_pin() {  # write_pin <clone-dir>
  local c="$1" commit cdate subj
  commit="$(git -C "$c" rev-parse HEAD)"
  cdate="$(git -C "$c" log -1 --format=%cI)"
  subj="$(git -C "$c" log -1 --format=%s)"
  cat > "$PIN" <<PIN
# Vendored copy of claude-max-api-proxy (MIT, see LICENSE). This tree is
# committed to agent-ops-kit so the proxy keeps building even if upstream
# disappears. Managed by scripts/claude-max-proxy/sync-upstream.sh
# (make claude-proxy-update); do not edit by hand.
url=$REPO_URL
ref=$REF
commit=$commit
commit_date=$cdate
commit_subject=$subj
synced=$(date -u +%Y-%m-%dT%H:%M:%SZ)
excluded=${EXCLUDE[*]}
PIN
}

# --- legacy: a checkout from before the sources were vendored (nested .git)
if [[ -d "$PROXY_DIR/.git" ]]; then
  if [[ -n "$(git -C "$PROXY_DIR" status --porcelain 2>/dev/null)" ]]; then
    warn "$PROXY_DIR is an old git clone with local changes; commit or discard them, then re-run"
    exit 0
  fi
  say "converting the old clone at $PROXY_DIR into a vendored tree"
  [[ -f "$PIN" ]] || write_pin "$PROXY_DIR"
  rm -rf "${EXCLUDE[@]/#/$PROXY_DIR/}"
fi

[[ -f "$PROXY_DIR/package.json" ]] || die "no proxy sources at $PROXY_DIR (git checkout incomplete?)"
current="$(pinned commit)"
short="${current:0:7}"

if ! have git; then
  say "git not found; using the vendored copy ($short)"
  exit 0
fi

# --- is upstream still there, and does it have something newer?
remote_head="$(net git ls-remote --heads "$REPO_URL" "$REF" 2>/dev/null | awk '{print $1; exit}' || true)"
if [[ -z "$remote_head" ]]; then
  say "upstream $REPO_URL unreachable or gone; using the vendored copy ($short)"
  exit 0
fi
if [[ "$remote_head" == "$current" ]]; then
  say "vendored copy is current with upstream '$REF' ($short)"
  exit 0
fi
if [[ "$MODE" == "--check" ]]; then
  say "upstream '$REF' has moved on (vendored $short, upstream ${remote_head:0:7}). Update: make claude-proxy-update"
  exit 0
fi

# --- update: only over a committed tree, never over local edits
dirty="$(git -C "$ROOT" status --porcelain -- "$PROXY_DIR" 2>/dev/null || true)"
if [[ -n "$dirty" ]]; then
  warn "uncommitted changes under $PROXY_DIR; commit or discard them before updating. Using the vendored copy ($short)"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if ! net git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$tmp/src" 2>/dev/null; then
  warn "clone of $REPO_URL failed; using the vendored copy ($short)"
  exit 0
fi

# Replace upstream's files, keep ours (build output, pin, local env).
keep_args=()
for k in "${KEEP[@]}"; do keep_args+=(! -name "$k"); done
find "$PROXY_DIR" -mindepth 1 -maxdepth 1 "${keep_args[@]}" -exec rm -rf {} +
git -C "$tmp/src" archive --format=tar HEAD | tar -x -C "$PROXY_DIR"
rm -rf "${EXCLUDE[@]/#/$PROXY_DIR/}"
write_pin "$tmp/src"

new="$(git -C "$tmp/src" log -1 --format='%h %s')"
say "vendored copy updated: $short -> $new"
say "review:  git diff --stat -- vendor/"
say "commit:  git add vendor && git commit -m 'chore(proxy): sync claude-max-api-proxy to ${new%% *}'"
