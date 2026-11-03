#!/usr/bin/env bash
# install-hermes-devtest.sh — install the trusted dev-test runner and its profile file
# root-owned, with the runner's SHA-256 pinned in the profile file (docs/dev-test-runner.md).
#
#   scripts/install-hermes-devtest.sh plan     [--profiles FILE]   # validate + show what would be installed
#   scripts/install-hermes-devtest.sh install  [--profiles FILE]   # sudo install (asks for the sudo password)
#   scripts/install-hermes-devtest.sh status                       # installed vs kit digest, trust checks
#
# FILE defaults to hermes/devtest/profiles.example.json (this host's Validate profile). Its
# helper_sha256 is replaced by the digest of hermes/libexec/hermes-devtest-runner. Nothing
# reads the credential file; only its path is registered. Nothing is restarted: Hermes picks
# the dev_test_run tool up from the runtime patch (0004) on its next start, and the tool
# re-verifies owner, mode and digest on every call, so a later edit of either file without
# re-running `install` makes every call fail closed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$ROOT/hermes/libexec/hermes-devtest-runner"
PROFILES="$ROOT/hermes/devtest/profiles.example.json"
DEST_RUNNER="/usr/local/libexec/hermes-devtest-runner"
DEST_DIR="/usr/local/etc/hermes-devtest"
DEST_PROFILES="$DEST_DIR/profiles.json"
PY="/usr/bin/python3"
CMD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    plan|install|status) CMD="$1" ;;
    --profiles) PROFILES="${2:?--profiles needs a file}"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$CMD" ]] || { echo "usage: $0 plan|install|status [--profiles FILE]" >&2; exit 2; }

digest() { sha256sum "$1" | cut -d' ' -f1; }

if [[ "$CMD" == "status" ]]; then
  kit="$(digest "$RUNNER")"
  echo "kit runner:        $RUNNER ($kit)"
  if [[ -f "$DEST_RUNNER" ]]; then
    echo "installed runner:  $DEST_RUNNER ($(digest "$DEST_RUNNER")) $(stat -c '%U:%G %a' "$DEST_RUNNER")"
  else
    echo "installed runner:  missing"
  fi
  if [[ -f "$DEST_PROFILES" ]]; then
    pinned="$("$PY" -I -S -c 'import json,sys; print(json.load(open(sys.argv[1]))["helper_sha256"])' "$DEST_PROFILES")"
    echo "profile file:      $DEST_PROFILES $(stat -c '%U:%G %a' "$DEST_PROFILES") pins $pinned"
    "$PY" -I -S "$DEST_RUNNER" check-config >/dev/null && echo "trusted config:    ok" || echo "trusted config:    REFUSED"
    [[ "$pinned" == "$(digest "$DEST_RUNNER")" ]] && echo "pinned digest:     matches the installed runner" \
      || { echo "pinned digest:     MISMATCH (dev_test_run fails closed)"; exit 1; }
    [[ "$kit" == "$pinned" ]] && echo "kit vs installed:  same" || echo "kit vs installed:  differs (run install after review)"
  else
    echo "profile file:      missing"
  fi
  exit 0
fi

[[ -f "$PROFILES" ]] || { echo "profiles file not found: $PROFILES" >&2; exit 2; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sha="$(digest "$RUNNER")"
"$PY" -I -S - "$PROFILES" "$sha" "$DEST_RUNNER" > "$TMP/profiles.json" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg["helper_sha256"], cfg["helper"] = sys.argv[2], sys.argv[3]
print(json.dumps(cfg, indent=2, sort_keys=True))
EOF
"$PY" -I -S "$RUNNER" check-config --candidate --config "$TMP/profiles.json"
echo "runner:   $RUNNER -> $DEST_RUNNER (root:root 0755, sha256 $sha)"
echo "profiles: $PROFILES -> $DEST_PROFILES (root:root 0644, pins that digest)"
[[ "$CMD" == "plan" ]] && { echo "plan only; run: $0 install"; exit 0; }

sudo install -d -o root -g root -m 0755 "$DEST_DIR"
sudo install -o root -g root -m 0755 "$RUNNER" "$DEST_RUNNER"
sudo install -o root -g root -m 0644 "$TMP/profiles.json" "$DEST_PROFILES"
[[ "$(digest "$DEST_RUNNER")" == "$sha" ]] || { echo "installed runner digest differs from the kit copy" >&2; exit 1; }
"$PY" -I -S "$DEST_RUNNER" check-config
echo "installed; verify with: $0 status"
