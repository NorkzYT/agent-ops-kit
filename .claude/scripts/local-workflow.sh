#!/usr/bin/env bash
set -euo pipefail

# local-workflow.sh
#
# Local engineer workflow wrapper for repo-specific commands detected into
# `TOOLS.md`.
#
# Runs, in order:
#   build -> run local stack -> test -> confirm (smoke check)
#
# This script is meant to be called:
# - manually from a terminal
# - by the autopilot/closer agents
# - by a Hermes coding subagent as the verification step

log()  { printf '[workflow] %s\n' "$*"; }
warn() { printf '[workflow][warn] %s\n' "$*" >&2; }
err()  { printf '[workflow][error] %s\n' "$*" >&2; }

REPO=""
TOOLS_FILE=""
SKIP_BUILD=0
SKIP_RUN_LOCAL=0
SKIP_TEST=0
SKIP_CONFIRM=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="${2:-}"
      shift 2
      ;;
    --tools)
      TOOLS_FILE="${2:-}"
      shift 2
      ;;
    --skip-build)
      SKIP_BUILD=1
      shift
      ;;
    --skip-run-local)
      SKIP_RUN_LOCAL=1
      shift
      ;;
    --skip-test)
      SKIP_TEST=1
      shift
      ;;
    --skip-confirm)
      SKIP_CONFIRM=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  bash .claude/scripts/local-workflow.sh [--repo <path>] [--tools <TOOLS.md>] [--skip-*]

Runs local engineering verification steps in order:
  build -> run-local -> test -> confirm
EOF
      exit 0
      ;;
    *)
      err "Unknown arg: $1"
      exit 2
      ;;
  esac
done

if [[ -z "$REPO" ]]; then
  REPO="$(pwd)"
fi
REPO="$(cd "$REPO" && pwd)"

if [[ -z "$TOOLS_FILE" ]]; then
  TOOLS_FILE="$REPO/TOOLS.md"
fi

if [[ ! -f "$TOOLS_FILE" ]]; then
  err "TOOLS.md not found: $TOOLS_FILE"
  err "Run: bash .claude/bootstrap/analyze_repo.sh $REPO"
  exit 2
fi

extract_code_block_after_header() {
  local header="$1"
  local file="$2"
  awk -v header="$header" '
    $0 == header { in_header=1; next }
    in_header && /^```/ { if (!in_code) { in_code=1; next } else { exit } }
    in_code { print }
  ' "$file" | sed '/^[[:space:]]*$/d'
}

normalize_cmd() {
  local c="${1:-}"
  if [[ -z "$c" || "$c" =~ ^# ]]; then
    echo ""
  else
    echo "$c"
  fi
}

BUILD_CMD="$(normalize_cmd "$(extract_code_block_after_header '## Build' "$TOOLS_FILE" | head -n1 || true)")"
RUN_LOCAL_CMD="$(normalize_cmd "$(extract_code_block_after_header '## Run Local Stack (Use this for local "deploy")' "$TOOLS_FILE" | head -n1 || true)")"
TEST_CMD="$(normalize_cmd "$(extract_code_block_after_header '## Test' "$TOOLS_FILE" | head -n1 || true)")"
CONFIRM_CMD="$(normalize_cmd "$(extract_code_block_after_header '## Confirm (Smoke Check)' "$TOOLS_FILE" | head -n1 || true)")"

run_step() {
  local name="$1"
  local cmd="$2"
  local rc=0

  if [[ -z "$cmd" ]]; then
    warn "$name: no command detected (skip)"
    return 10
  fi

  log "$name: $cmd"
  (cd "$REPO" && bash -lc "$cmd") || rc=$?
  return "$rc"
}

build_status="skipped"
run_local_status="skipped"
test_status="skipped"
confirm_status="skipped"

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  if run_step "build" "$BUILD_CMD"; then
    build_status="passed"
  else
    case $? in
      10) build_status="not-detected" ;;
      *)  build_status="failed" ;;
    esac
  fi
fi

if [[ "$SKIP_RUN_LOCAL" -eq 0 && "$build_status" != "failed" ]]; then
  if run_step "run-local" "$RUN_LOCAL_CMD"; then
    run_local_status="passed"
  else
    case $? in
      10) run_local_status="not-detected" ;;
      *)  run_local_status="failed" ;;
    esac
  fi
fi

if [[ "$SKIP_TEST" -eq 0 && "$build_status" != "failed" && "$run_local_status" != "failed" ]]; then
  if run_step "test" "$TEST_CMD"; then
    test_status="passed"
  else
    case $? in
      10) test_status="not-detected" ;;
      *)  test_status="failed" ;;
    esac
  fi
fi

if [[ "$SKIP_CONFIRM" -eq 0 && "$build_status" != "failed" && "$run_local_status" != "failed" ]]; then
  if run_step "confirm" "$CONFIRM_CMD"; then
    confirm_status="passed"
  else
    case $? in
      10) confirm_status="not-detected" ;;
      *)  confirm_status="failed" ;;
    esac
  fi
fi

SUMMARY_FILE="$REPO/.claude/workflow-report.local.json"
mkdir -p "$REPO/.claude"
python3 - "$SUMMARY_FILE" "$REPO" "$build_status" "$run_local_status" "$test_status" "$confirm_status" <<'PY'
import json, sys
path, repo, build, run_local, test, confirm = sys.argv[1:]
with open(path, "w", encoding="utf-8") as f:
    json.dump(
        {
            "repo": repo,
            "steps": {
                "build": build,
                "run_local": run_local,
                "test": test,
                "confirm": confirm,
            },
        },
        f,
        indent=2,
    )
    f.write("\n")
PY

log "Summary written: $SUMMARY_FILE"
printf '\nLocal workflow summary\n'
printf '  build:     %s\n' "$build_status"
printf '  run-local: %s\n' "$run_local_status"
printf '  test:      %s\n' "$test_status"
printf '  confirm:   %s\n' "$confirm_status"

if [[ "$build_status" == "failed" || "$run_local_status" == "failed" || "$test_status" == "failed" || "$confirm_status" == "failed" ]]; then
  exit 1
fi
