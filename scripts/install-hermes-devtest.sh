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
#
# STATUS: `plan`/`install` exit 1 with REJECTED for every profile whose isolation.mode is not
# the approved `credential-broker-netns` design (the shipped example uses the rejected
# `same-uid-landlock`); `status` exits non-zero unless a trusted, approved install exists.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$ROOT/hermes/libexec/hermes-devtest-runner"
PROFILES="$ROOT/hermes/devtest/profiles.example.json"
DEST_RUNNER="${HERMES_DEVTEST_DEST_RUNNER:-/usr/local/libexec/hermes-devtest-runner}"
DEST_DIR="${HERMES_DEVTEST_DEST_DIR:-/usr/local/etc/hermes-devtest}"
DEST_PROFILES="$DEST_DIR/profiles.json"
PY="/usr/bin/python3"
SUDO="${SUDO:-sudo}"
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
  # Fail closed: exit 0 only for a complete, trusted, digest-matching install of an approved design.
  rc=0
  kit="$(digest "$RUNNER")"
  echo "kit runner:        $RUNNER ($kit)"
  if [[ -f "$DEST_RUNNER" ]]; then
    echo "installed runner:  $DEST_RUNNER ($(digest "$DEST_RUNNER")) $(stat -c '%U:%G %a' "$DEST_RUNNER")"
  else
    echo "installed runner:  missing"; rc=1
  fi
  if [[ -f "$DEST_PROFILES" ]]; then
    pinned="$("$PY" -I -S -c 'import json,sys; print(json.load(open(sys.argv[1]))["helper_sha256"])' "$DEST_PROFILES" 2>/dev/null || echo unreadable)"
    echo "profile file:      $DEST_PROFILES $(stat -c '%U:%G %a' "$DEST_PROFILES") pins $pinned"
    if [[ -f "$DEST_RUNNER" ]] && "$PY" -I -S "$DEST_RUNNER" check-config >/dev/null 2>&1; then
      echo "trusted config:    ok (approved isolation design)"
    else
      echo "trusted config:    REFUSED or rejected design (dev_test_run fails closed)"; rc=1
    fi
    if [[ -f "$DEST_RUNNER" && "$pinned" == "$(digest "$DEST_RUNNER")" ]]; then
      echo "pinned digest:     matches the installed runner"
    else
      echo "pinned digest:     MISMATCH (dev_test_run fails closed)"; rc=1
    fi
    [[ "$kit" == "$pinned" ]] && echo "kit vs installed:  same" || echo "kit vs installed:  differs (run install after review)"
  else
    echo "profile file:      missing"; rc=1
  fi
  [[ "$rc" -eq 0 ]] && echo "state:             INSTALLED and trusted" || echo "state:             NOT INSTALLED / not trusted (dev_test_run unavailable)"
  exit "$rc"
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
check="$("$PY" -I -S "$RUNNER" check-config --candidate --config "$TMP/profiles.json")" && check_rc=0 || check_rc=$?
echo "$check"
if [[ "$check_rc" -ne 0 ]]; then
  rejected="$(printf '%s' "$check" | "$PY" -I -S -c 'import json,sys; c=json.load(sys.stdin); print(", ".join(c.get("rejected_profiles") or []) or "invalid config")' 2>/dev/null || echo "invalid config")"
  echo "REJECTED: $rejected — isolation design rejected by review (same-uid-landlock) or config does not validate." >&2
  echo "Nothing is installed. The same-uid-landlock design does not meet the network/credential isolation requirements;" >&2
  echo "the approved credential-broker-netns design needs privileged provisioning and operator approval (docs/dev-test-runner.md)." >&2
  exit 1
fi
echo "runner:   $RUNNER -> $DEST_RUNNER (root:root 0755, sha256 $sha)"
echo "profiles: $PROFILES -> $DEST_PROFILES (root:root 0644, pins that digest)"
[[ "$CMD" == "plan" ]] && { echo "plan only; run: $0 install"; exit 0; }

"$SUDO" install -d -o root -g root -m 0755 "$DEST_DIR"
"$SUDO" install -o root -g root -m 0755 "$RUNNER" "$DEST_RUNNER"
"$SUDO" install -o root -g root -m 0644 "$TMP/profiles.json" "$DEST_PROFILES"
[[ "$(digest "$DEST_RUNNER")" == "$sha" ]] || { echo "installed runner digest differs from the kit copy" >&2; exit 1; }
"$PY" -I -S "$DEST_RUNNER" check-config
echo "installed; verify with: $0 status"
