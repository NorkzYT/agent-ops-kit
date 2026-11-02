#!/usr/bin/env bash
# hermes-runtime-patch.sh — track the kit's patches to the installed Hermes runtime
# (hermes/runtime-patches/*.patch) so a Hermes reinstall or upgrade can re-apply them.
#
#   scripts/hermes-runtime-patch.sh status            # per patch: applied | applicable | conflict
#   scripts/hermes-runtime-patch.sh check             # exit 0 iff every patch is applied or applicable
#   scripts/hermes-runtime-patch.sh apply             # apply the applicable ones (working tree only)
#
# Options: --src DIR (default $HERMES_HOME/hermes-agent, else ~/.hermes/hermes-agent),
#          --patches DIR (default hermes/runtime-patches), --json.
#
# Scope: only the files each patch names are touched, and only when they are clean in
# the runtime checkout (an unrelated dirty file never blocks, a dirty target always does).
# Nothing is committed, reset, stashed or restarted; the gateway picks the change up on
# its next restart (make hermes-restart, from your own terminal). `apply` is idempotent:
# an already-applied patch is reported and skipped. Patch order is the file order.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SRC="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
PATCHES="$ROOT/hermes/runtime-patches"
JSON=0
CMD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    status|check|apply) CMD="$1" ;;
    --src) SRC="${2:?--src needs a directory}"; shift ;;
    --patches) PATCHES="${2:?--patches needs a directory}"; shift ;;
    --json) JSON=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$CMD" ]] || { echo "usage: $0 status|check|apply [--src DIR] [--patches DIR] [--json]" >&2; exit 2; }
[[ -d "$SRC" ]] || { echo "runtime source not found: $SRC (pass --src)" >&2; exit 2; }
git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "not a git checkout: $SRC" >&2; exit 2; }
[[ -d "$PATCHES" ]] || { echo "no patch directory: $PATCHES" >&2; exit 2; }

# Files a patch touches (b/ side), one per line.
patch_files() { grep -E '^\+\+\+ b/' "$1" | sed 's#^+++ b/##' | sort -u; }

# Any of the patch's target files dirty (staged or unstaged) in the runtime checkout?
dirty_targets() {
  local f out=""
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    if [[ -n "$(git -C "$SRC" status --porcelain --untracked-files=no -- "$f" 2>/dev/null)" ]]; then
      out+="${out:+ }$f"
    fi
  done < <(patch_files "$1")
  printf '%s' "$out"
}

# applied | applicable | conflict  (+ detail)
classify() {
  local p="$1"
  if git -C "$SRC" apply --reverse --check "$p" >/dev/null 2>&1; then
    echo "applied"
  elif git -C "$SRC" apply --check "$p" >/dev/null 2>&1; then
    echo "applicable"
  else
    echo "conflict"
  fi
}

shopt -s nullglob
patches=("$PATCHES"/*.patch)
shopt -u nullglob
[[ ${#patches[@]} -gt 0 ]] || { echo "no *.patch files in $PATCHES" >&2; exit 2; }

head_sha="$(git -C "$SRC" rev-parse --short=10 HEAD 2>/dev/null || echo unknown)"
rows=(); rc=0; applied_now=0
for p in "${patches[@]}"; do
  name="$(basename "$p")"
  state="$(classify "$p")"
  dirty="$(dirty_targets "$p")"
  action="none"
  case "$CMD" in
    apply)
      if [[ "$state" == "applicable" ]]; then
        if [[ -n "$dirty" ]]; then
          action="refused: dirty target(s): $dirty"; rc=1
        elif git -C "$SRC" apply "$p" >/dev/null 2>&1; then
          action="applied"; state="applied"; applied_now=$((applied_now + 1))
        else
          action="failed"; state="conflict"; rc=1
        fi
      elif [[ "$state" == "conflict" ]]; then
        action="skipped: conflict"; rc=1
      fi ;;
    check)
      [[ "$state" == "conflict" ]] && rc=1 ;;
  esac
  rows+=("$name|$state|$action|$dirty")
done

if [[ "$JSON" == "1" ]]; then
  printf '{"src":"%s","head":"%s","patches":[' "$SRC" "$head_sha"
  first=1
  for r in "${rows[@]}"; do
    IFS='|' read -r n s a d <<< "$r"
    [[ $first -eq 1 ]] || printf ','
    first=0
    printf '{"patch":"%s","state":"%s","action":"%s","dirty_targets":"%s"}' "$n" "$s" "$a" "$d"
  done
  printf '],"exit":%d}\n' "$rc"
else
  echo "runtime: $SRC @ $head_sha"
  for r in "${rows[@]}"; do
    IFS='|' read -r n s a d <<< "$r"
    line="  $s	$n"
    [[ "$a" != "none" ]] && line+="  [$a]"
    [[ -n "$d" && "$a" == "none" ]] && line+="  (dirty: $d)"
    echo "$line"
  done
  if [[ "$CMD" == "apply" ]]; then
    echo "applied now: $applied_now; restart the gateway to activate (make hermes-restart, from your own terminal)"
  fi
fi
exit "$rc"
