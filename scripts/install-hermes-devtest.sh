#!/usr/bin/env bash
# install-hermes-devtest.sh — install the trusted dev-test runner and its profile file
# root-owned, with the runner's SHA-256 pinned in the profile file (docs/dev-test-runner.md).
#
#   scripts/install-hermes-devtest.sh plan     [--profiles FILE]   # validate + show what would be installed
#   scripts/install-hermes-devtest.sh install  [--profiles FILE]   # sudo install (asks for the sudo password)
#   scripts/install-hermes-devtest.sh status                       # installed vs kit digest, trust checks
#
# FILE defaults to hermes/devtest/profiles.example.json (a synthetic example-app profile). Its
# helper_sha256 is replaced by the digest of hermes/libexec/hermes-devtest-runner. Nothing
# reads the credential file; only its path is registered. Nothing is restarted: Hermes picks
# the dev_test_run tool up from the runtime patch (0004) on its next start, and the tool
# re-verifies owner, mode and digest on every call, so a later edit of either file without
# re-running `install` makes every call fail closed.
#
# STATUS: `plan`/`install` exit 1 unless the runner's check-config reports every profile
# runnable: REJECTED for the rejected `same-uid-landlock` design (the shipped example) and
# NOT IMPLEMENTED for the proposed `credential-broker-netns` design, which no runner version
# implements yet. A valid profile file is not a runnable one. `status` exits non-zero unless a
# trusted install whose profiles are runnable exists; today nothing can satisfy that.
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
    if [[ -f "$DEST_RUNNER" ]] && check_out="$("$PY" -I -S "$DEST_RUNNER" check-config 2>/dev/null)"; then
      echo "trusted config:    runnable (every profile's isolation design is implemented and installable)"
    else
      detail="$(printf '%s' "${check_out:-}" | "$PY" -I -S -c '
import json, sys
try:
    c = json.load(sys.stdin)
except Exception:
    print("REFUSED (untrusted, malformed or unreadable)"); sys.exit()
parts = []
if c.get("rejected_profiles"): parts.append("rejected design: " + ", ".join(c["rejected_profiles"]))
if c.get("not_implemented_profiles"): parts.append("valid but NOT IMPLEMENTED (no backend): " + ", ".join(c["not_implemented_profiles"]))
print("; ".join(parts) or "not runnable")' 2>/dev/null || echo "REFUSED")"
      echo "trusted config:    NOT RUNNABLE — $detail (dev_test_run fails closed)"; rc=1
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
  rejected="$(printf '%s' "$check" | "$PY" -I -S -c 'import json,sys; c=json.load(sys.stdin); print(", ".join(c.get("rejected_profiles") or []))' 2>/dev/null || echo "")"
  notimpl="$(printf '%s' "$check" | "$PY" -I -S -c 'import json,sys; c=json.load(sys.stdin); print(", ".join(c.get("not_implemented_profiles") or []))' 2>/dev/null || echo "")"
  [[ -n "$rejected" ]] && echo "REJECTED: $rejected — isolation design rejected by review (same-uid-landlock): same-uid credential access and port-based network rules do not meet the isolation requirements." >&2
  [[ -n "$notimpl" ]] && echo "NOT IMPLEMENTED: $notimpl — the profile is valid but its proposed credential-broker-netns design has no implemented, provisioned or attested backend; a mode string is not readiness." >&2
  [[ -z "$rejected$notimpl" ]] && echo "INVALID: the profile file does not validate." >&2
  echo "Nothing is installed. The proposed design needs operator approval and privileged provisioning first (docs/dev-test-runner.md, section Proposed design)." >&2
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
