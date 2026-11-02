#!/usr/bin/env bash
# test_hermes_runtime_patch.sh — scripts/hermes-runtime-patch.sh against a throwaway git
# "runtime" checkout and a fixture patch directory: status/check/apply states, idempotent
# apply, refusal on a dirty target, unrelated dirty files ignored, conflict reporting.
# Never touches ~/.hermes.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
pass=0; fail=0
ck() { if eval "$2"; then printf '  ok   %s\n' "$1"; pass=$((pass+1)); else printf '  FAIL %s\n' "$1"; fail=$((fail+1)); fi; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
src="$tmp/runtime"; patches="$tmp/patches"; mkdir -p "$patches"
g() { git -C "$src" -c user.email=t@example.com -c user.name=t -c commit.gpgsign=false "$@"; }
git init -q -b main "$src"
printf 'a = 1\n' > "$src/agent.py"; printf 'other\n' > "$src/unrelated.py"
g add . >/dev/null; g commit -q -m "base"
# fixture patch 1: edits agent.py; fixture patch 2: adds tests/new_test.py
printf 'a = 2\n' > "$src/agent.py"; g commit -q -am "feat: a=2"
mkdir -p "$src/tests"; printf 'ok\n' > "$src/tests/new_test.py"; g add tests; g commit -q -m "test: add"
g format-patch -q -2 --no-signature -o "$patches" HEAD >/dev/null
g reset -q --hard HEAD~2            # back to base: both patches applicable
printf 'local change\n' >> "$src/unrelated.py"   # an unrelated dirty file, like memory_manager.py

S="bash scripts/hermes-runtime-patch.sh"
out="$($S status --src "$src" --patches "$patches")"; rc=$?
ck "status exits 0 when all applicable (rc=$rc)"   "[[ $rc -eq 0 ]]"
ck "status lists both as applicable"               "[[ \$(grep -c 'applicable' <<< \"$out\") -eq 2 ]]"
ck "check passes when applicable"                  "$S check --src '$src' --patches '$patches' >/dev/null"

out="$($S apply --src "$src" --patches "$patches")"; rc=$?
ck "apply exits 0 (rc=$rc)"                        "[[ $rc -eq 0 ]]"
ck "apply reports two applied"                     "grep -q 'applied now: 2' <<< \"$out\""
ck "agent.py patched"                              "grep -q 'a = 2' '$src/agent.py'"
ck "new test file created"                         "[[ -f '$src/tests/new_test.py' ]]"
ck "unrelated dirty file untouched"                "grep -q 'local change' '$src/unrelated.py'"
ck "nothing committed"                             "[[ \$(g rev-list --count HEAD) -eq 1 ]]"
out="$($S status --src "$src" --patches "$patches" --json)"
n_applied="$(grep -o '"state":"applied"' <<< "$out" | wc -l)"
ck "json status says applied x2 (got $n_applied)"  "[[ $n_applied -eq 2 ]]"
out="$($S apply --src "$src" --patches "$patches")"; rc=$?
ck "second apply is a no-op (rc=$rc)"              "[[ $rc -eq 0 ]] && grep -q 'applied now: 0' <<< \"$out\""

# dirty target: revert agent.py to base and leave an uncommitted edit on it -> refused
g checkout -q -- agent.py; printf 'a = 1\nmine\n' > "$src/agent.py"
out="$($S apply --src "$src" --patches "$patches")"; rc=$?
ck "apply refuses a dirty/conflicting target (rc=$rc)" "[[ $rc -ne 0 ]]"
ck "agent.py left as the operator had it"          "grep -q '^mine$' '$src/agent.py'"
# a committed divergence of the target makes the patch a conflict: reported, never forced
printf 'b = 3\n' > "$src/agent.py"; g commit -q -am "diverge"
ck "conflict reported by check"                    "! $S check --src '$src' --patches '$patches' >/dev/null"
out="$($S apply --src "$src" --patches "$patches")"; rc=$?
ck "apply skips the conflict and fails (rc=$rc)"   "[[ $rc -ne 0 ]] && grep -q 'skipped: conflict' <<< \"$out\""
ck "diverged file untouched"                       "grep -q '^b = 3$' '$src/agent.py'"
ck "usage error without a command"                 "! $S --src '$src' >/dev/null 2>&1"
ck "missing src is an error"                       "! $S status --src '$tmp/nope' >/dev/null 2>&1"

# The kit's real patches are well-formed git patches naming only runtime files.
for p in hermes/runtime-patches/*.patch; do
  ck "$(basename "$p") is a git patch"             "grep -q '^diff --git a/' '$p'"
  ck "$(basename "$p") touches no .env or secrets" "! grep -E '^\+\+\+ b/.*(\.env|secret|credential)' '$p' >/dev/null"
done

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
