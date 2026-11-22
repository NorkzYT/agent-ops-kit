#!/usr/bin/env bash
# ui-audit-project.sh — run axe, Lighthouse CI and Impeccable on your own static
# pages (make ui-audit-project). All guards live in
# tools/ui-audit-pilot/scripts/lib/project.mjs and run before a browser starts.
#   PROJECT=/abs/dir  PAGES=index.html,docs/a.html (default index.html)  OUT=dir
# Reports: <OUT>/<run-id>/ (default tools/ui-audit-pilot/reports/project-<name>-<hash>/<run-id>/).
# Exit: 1 if any tool errored or refused, else 2 if axe/LHCI found failures, else 0.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
dir="$(cd "$here/.." && pwd)/tools/ui-audit-pilot"

[[ -n "${PROJECT:-}" ]] || { printf '[agent-ops-kit][error] PROJECT=/absolute/path/to/static/site is required\n' >&2; exit 2; }
[[ -d "$dir/node_modules" ]] || die "UI-audit deps not installed: TOOL_PILOTS_ALLOW_DOWNLOADS=1 TOOLS=ui-audit make tool-pilots-install"
args=(--project "$PROJECT")
if [[ -n "${PAGES:-}" ]]; then
  IFS=',' read -r -a pages <<<"$PAGES"
  for p in "${pages[@]}"; do args+=(--page-path "$p"); done
fi
# Relative OUT is taken from the caller's directory, not the tool dir.
[[ -n "${OUT:-}" ]] && { [[ "$OUT" == /* ]] || OUT="$PWD/$OUT"; args+=(--out "$OUT"); }

# One shared run id: the three runners write into the same new directory
# <OUT>/<run-id>/ and never delete or overwrite existing reports.
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
args+=(--run-id "$run_id")

cd "$dir"
rc=0
for s in axe lhci impeccable; do
  node "scripts/run-$s.mjs" "${args[@]}" && r=0 || r=$?
  if (( r == 1 || (r != 0 && r != 2) )); then rc=1; elif (( r == 2 && rc == 0 )); then rc=2; fi
done
printf '[agent-ops-kit] run id %s (reports in <OUT>/%s/)\n' "$run_id" "$run_id"
exit "$rc"
