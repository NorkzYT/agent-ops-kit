#!/usr/bin/env bash
# tool-pilots.sh — fresh-machine setup for the project-local tool pilots.
#   check    prerequisites (Node 22+, npm, Chrome, FFmpeg, python3) and whether
#            each tool's pinned deps are installed; downloads nothing.
#   install  project-local `npm ci --ignore-scripts` per tool, from the tracked
#            lockfiles. Only the prerequisites gate the install: missing or stale
#            pins are what the install fixes, so they never block it. The full
#            check (prerequisites + pins) runs afterwards. Downloads from the npm registry, so it needs the explicit
#            opt-in TOOL_PILOTS_ALLOW_DOWNLOADS=1. Nothing global, no npx, no
#            postinstall scripts, no hooks, no Hermes/Claude config, no restarts.
# TOOLS=ui-audit,hyperframes (default: both). RTK is not installed here: its
# pinned binary (tools/rtk-pilot/rtk.lock.json) is a separate, manual step.
# Exit: 0 ok, 1 a prerequisite/pin/install failed, 2 usage or missing opt-in.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
root="$(cd "$here/.." && pwd)"

usage() { printf 'usage: [TOOLS=ui-audit,hyperframes] [TOOL_PILOTS_ALLOW_DOWNLOADS=1] %s <check|install>\n' "$0" >&2; exit 2; }

tool_dir() {
  case "$1" in
    ui-audit)    echo "$root/tools/ui-audit-pilot" ;;
    hyperframes) echo "$root/tools/hyperframes-marketing" ;;
    *) return 1 ;;
  esac
}

IFS=',' read -r -a tools <<<"${TOOLS:-ui-audit,hyperframes}"
for t in "${tools[@]}"; do
  tool_dir "$t" >/dev/null || { printf '[agent-ops-kit][error] unknown tool %q (supported: ui-audit, hyperframes)\n' "$t" >&2; exit 2; }
done
want() { local t; for t in "${tools[@]}"; do [[ "$t" == "$1" ]] && return 0; done; return 1; }

# Compare each pinned dependency with what is installed under node_modules.
# Prints "ok", "missing" or "mismatch <pkg> <got> != <want>".
PIN_JS='const fs=require("fs"),p=require("path"),d=process.argv[1];
const pk=JSON.parse(fs.readFileSync(p.join(d,"package.json"),"utf8"));
const deps={...pk.dependencies,...pk.devDependencies};
if(!fs.existsSync(p.join(d,"node_modules"))){console.log("missing");process.exit(0)}
for(const[n,v]of Object.entries(deps)){let got;try{got=JSON.parse(fs.readFileSync(p.join(d,"node_modules",n,"package.json"),"utf8")).version}catch{got="absent"}
if(got!==v){console.log(`mismatch ${n} ${got} != ${v}`);process.exit(0)}}
console.log("ok")'

bad=0
need() { printf '  %-12s %s\n' "$1" "$2"; }
missing() { printf '  %-12s MISSING: %s\n' "$1" "$2"; bad=1; }

# The google-chrome wrapper writes ~/.local/share/applications/mimeapps.list
# even for --version, so it runs with a throwaway HOME.
chrome_version() {
  local h v
  h="$(mktemp -d "${TMPDIR:-/tmp}/tool-pilots-chrome.XXXXXX")"
  v="$(HOME="$h" "$1" --version 2>/dev/null | head -n1)" || true
  rm -rf -- "$h"
  printf '%s' "$v"
}

check_chrome() {
  local var="$1" path="$2"
  if [[ -x "$path" ]]; then need chrome "$(chrome_version "$path") ($path; override with $var)"
  else missing chrome "Chrome not executable at $path (install Google Chrome or set $var)"; fi
}

prereqs() {
  log "prerequisites"
  if have node; then
    local v; v="$(node --version)"
    if [[ "$v" =~ ^v([0-9]+)\. ]] && (( BASH_REMATCH[1] >= 22 )); then need node "$v"
    else missing node "found $v, need Node 22+ on PATH"; fi
  else missing node "need Node 22+ on PATH"; fi
  if have npm; then need npm "$(npm --version)"; else missing npm "npm (ships with Node)"; fi
  want ui-audit && check_chrome UI_AUDIT_CHROME "${UI_AUDIT_CHROME:-/usr/bin/google-chrome}"
  if want hyperframes; then
    [[ "${MARKETING_CHROME:-/usr/bin/google-chrome}" == "${UI_AUDIT_CHROME:-/usr/bin/google-chrome}" ]] && want ui-audit \
      || check_chrome MARKETING_CHROME "${MARKETING_CHROME:-/usr/bin/google-chrome}"
    if have ffmpeg; then need ffmpeg "$(command -v ffmpeg)"; else missing ffmpeg "FFmpeg on PATH (HyperFrames renders)"; fi
  fi
  if have python3; then need python3 "$(python3 --version 2>&1) (RTK replay tests)"; else warn "python3 not found: make test-rtk-pilot will not run"; fi
  need rtk "not installed by this setup (manual pinned binary, tools/rtk-pilot/README.md)"
}

# Pin diagnostics only; never a reason to refuse an install.
pins() {
  log "project-local deps"
  local t d s
  for t in "${tools[@]}"; do
    d="$(tool_dir "$t")"
    if ! have node; then need "$t" "unknown (no node)"; continue; fi
    s="$(node -e "$PIN_JS" "$d")"
    case "$s" in
      ok)      need "$t" "installed, pins match ($d/node_modules)" ;;
      missing) need "$t" "not installed: TOOL_PILOTS_ALLOW_DOWNLOADS=1 make tool-pilots-install" ;;
      *)       missing "$t" "$s (stale: TOOL_PILOTS_ALLOW_DOWNLOADS=1 make tool-pilots-install; npm ci replaces node_modules from the lockfile)" ;;
    esac
  done
}

check() { bad=0; prereqs; pins; return "$bad"; }

install() {
  if [[ "${TOOL_PILOTS_ALLOW_DOWNLOADS:-}" != 1 ]]; then
    cat >&2 <<EOF
[agent-ops-kit][error] install downloads the pinned packages from https://registry.npmjs.org/
  into tools/<tool>/node_modules (npm ci --ignore-scripts, lockfile integrity checked;
  npm's cache is \$HOME/.npm unless npm_config_cache is set). Nothing global is changed.
  Re-run with the explicit opt-in:  TOOL_PILOTS_ALLOW_DOWNLOADS=1 make tool-pilots-install
EOF
    exit 2
  fi
  bad=0
  prereqs
  (( bad == 0 )) || die "prerequisites missing; nothing installed"
  local t
  for t in "${tools[@]}"; do
    log "npm ci --ignore-scripts in $(tool_dir "$t")"
    (cd "$(tool_dir "$t")" && npm ci --ignore-scripts --no-audit --no-fund) || die "npm ci failed for $t"
  done
  check
}

case "${1:-}" in
  check)   check ;;
  install) install ;;
  *) usage ;;
esac
