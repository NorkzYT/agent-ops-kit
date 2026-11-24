#!/usr/bin/env bash
# marketing-video.sh — one entry point for marketing-video renders.
#   ENGINE=hyperframes (default for new videos) → tools/hyperframes-marketing
#   ENGINE=remotion    (explicit legacy lane)  → tools/remotion-marketing
# There is no fallback: if the chosen engine fails, the command fails. Any other
# ENGINE value is refused.
# usage: marketing-video.sh <install|test|render|gates> [render args...]
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
root="$(cd "$here/.." && pwd)"

engine="${ENGINE:-hyperframes}"
case "$engine" in
  hyperframes) dir="$root/tools/hyperframes-marketing" ;;
  remotion)    dir="$root/tools/remotion-marketing"
               warn "ENGINE=remotion is the legacy lane; new videos default to HyperFrames" ;;
  *) printf '[agent-ops-kit][error] unknown ENGINE=%s (use hyperframes or remotion; no fallback)\n' "$engine" >&2; exit 2 ;;
esac

cmd="${1:-}"; shift || true
case "$cmd" in
  install) cd "$dir" && exec npm ci --ignore-scripts --no-audit --no-fund ;;
  test)    cd "$dir" && exec node --test "tests/*.test.mjs" ;;
  gates)
    [[ "$engine" == hyperframes ]] || die "gates-only is a HyperFrames option; for Remotion use its check:* npm scripts"
    exec node "$dir/scripts/render-campaign.mjs" --gates-only "$@" ;;
  render)  exec node "$dir/scripts/render-campaign.mjs" "$@" ;;
  *) printf 'usage: ENGINE=hyperframes|remotion %s <install|test|render|gates> [args]\n' "$0" >&2; exit 2 ;;
esac
