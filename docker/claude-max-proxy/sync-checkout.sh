#!/usr/bin/env bash
#
# Keep the claude-max-api-proxy source checkout current so `make update`
# actually ships new proxy code (new models, fixes) instead of rebuilding
# the same sources from cache.
#
# Runs on the HOST (called by `make init` and `make update`), never inside a
# container. It is conservative by design:
#   - clones the default ref on a fresh machine
#   - fast-forwards the current branch when it is clean
#   - migrates a checkout still on a LEGACY kit-pinned branch to the default ref
#   - respects a user-chosen branch (only fast-forwards it, with a note)
#   - never touches a dirty worktree, never force-pulls, never resets
#   - network/divergence problems warn and exit 0 so updates keep working
#     offline with the sources already present
#
# Usage:
#   sync-proxy-checkout.sh [proxy-dir]
#
# Env:
#   CLAUDE_MAX_PROXY_DIR   checkout location   (default: ./vendor/claude-max-api-proxy)
#   CLAUDE_MAX_PROXY_REPO  clone URL           (default: NorkzYT/claude-max-api-proxy)
#   CLAUDE_MAX_PROXY_REF   branch to track     (default: main)
#
set -euo pipefail

PROXY_DIR="${1:-${CLAUDE_MAX_PROXY_DIR:-./vendor/claude-max-api-proxy}}"
REPO_URL="${CLAUDE_MAX_PROXY_REPO:-https://github.com/NorkzYT/claude-max-api-proxy.git}"
REF="${CLAUDE_MAX_PROXY_REF:-main}"

# Branches the kit pinned in older installs. A checkout sitting on one of
# these was put there by install.sh, not by the user, so it is safe to move
# it to the current default ref.
LEGACY_REFS=("fix/oauth-refresh-race")

TAG="sync-proxy"
log() { echo "[$TAG] $*"; }

describe_head() {
  git -C "$PROXY_DIR" log -1 --format='%h %s' 2>/dev/null || echo "(unknown)"
}

if ! command -v git >/dev/null 2>&1; then
  log "git not found; skipping proxy source sync" >&2
  exit 0
fi

if [[ ! -d "$PROXY_DIR/.git" ]]; then
  log "cloning $REPO_URL ($REF) -> $PROXY_DIR"
  if git clone --branch "$REF" "$REPO_URL" "$PROXY_DIR"; then
    log "proxy sources at: $(describe_head)"
  else
    log "WARN: clone failed (offline?); proxy build will be skipped until sources exist" >&2
  fi
  exit 0
fi

if ! git -C "$PROXY_DIR" fetch origin 2>/dev/null; then
  log "WARN: fetch failed (offline?); building existing sources: $(describe_head)" >&2
  exit 0
fi

if [[ -n "$(git -C "$PROXY_DIR" status --porcelain 2>/dev/null)" ]]; then
  log "WARN: local changes in $PROXY_DIR; leaving sources untouched: $(describe_head)" >&2
  exit 0
fi

branch="$(git -C "$PROXY_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"

is_legacy=0
for legacy in "${LEGACY_REFS[@]}"; do
  [[ "$branch" == "$legacy" ]] && is_legacy=1
done

if [[ "$branch" == "HEAD" || "$is_legacy" == "1" ]]; then
  # Detached HEAD or a kit-pinned legacy branch: converge on the default ref.
  if [[ "$branch" != "$REF" ]]; then
    log "moving checkout from '$branch' to kit default '$REF'"
    if ! git -C "$PROXY_DIR" checkout "$REF" 2>/dev/null; then
      log "WARN: could not check out '$REF'; keeping '$branch': $(describe_head)" >&2
      exit 0
    fi
    branch="$REF"
  fi
fi

before="$(git -C "$PROXY_DIR" rev-parse --short HEAD)"
if git -C "$PROXY_DIR" pull --ff-only origin "$branch" >/dev/null 2>&1; then
  after="$(git -C "$PROXY_DIR" rev-parse --short HEAD)"
  if [[ "$before" == "$after" ]]; then
    log "proxy sources already current on '$branch': $(describe_head)"
  else
    log "proxy sources updated on '$branch': $before -> $(describe_head)"
  fi
else
  log "WARN: '$branch' did not fast-forward (diverged from origin?); building: $(describe_head)" >&2
fi

if [[ "$branch" != "$REF" ]]; then
  log "note: tracking custom branch '$branch' (kit default: '$REF')"
fi
