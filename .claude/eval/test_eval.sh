#!/usr/bin/env bash
# test_eval.sh — E2E tests for the eval harness using deterministic mock runners.
# Proves the plumbing: full matrix runs, pass/fail recorded correctly, fixtures
# are never mutated, and the scoreboard aggregates. Exit non-zero on any fail.
set -uo pipefail

EVAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$EVAL_DIR/run-eval.sh"
# shellcheck source=lib.sh
. "$EVAL_DIR/lib.sh"

PASS=0 FAIL=0 SKIP=0
ok() { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
no() { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  \033[33mSKIP\033[0m %s (%s)\n' "$1" "$2"; }
assert() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else no "$1   [cond: $2]"; fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
SOLVE="$TMP/solve.tsv"; NOOP="$TMP/noop.tsv"

seed_sum() { find "$EVAL_DIR/tasks" -path '*/seed/*' -type f -exec cat {} + | cksum; }
BEFORE="$(seed_sum)"

bash "$RUN" --runner mock-solve --out "$SOLVE" --quiet >/dev/null 2>&1
bash "$RUN" --runner mock-noop  --out "$NOOP"  --quiet >/dev/null 2>&1

NMODES="$(awk -F'\t' 'NF{n++} END{print n+0}' "$EVAL_DIR/modes/modes.tsv")"
# Count only tasks runnable on this host (same deps gating run-eval.sh applies).
NTASKS=0
for d in "$EVAL_DIR"/tasks/*/; do eval_task_runnable "$d" && NTASKS=$((NTASKS + 1)); done
EXPECT=$((NMODES * NTASKS))

cell()      { awk -F'\t' -v m="$1" -v t="$2" 'NR>1 && $1==m && $2==t{print $4; exit}' "$3"; }
passcount() { awk -F'\t' 'NR>1 && $4==1{c++} END{print c+0}' "$1"; }
rowcount()  { awk -F'\t' 'NR>1{c++} END{print c+0}' "$1"; }

assert "results header has 7 columns"        '[ "$(head -1 "$SOLVE" | awk -F"\t" "{print NF}")" -eq 7 ]'
assert "matrix size is modes*tasks (>0)"     '[ "$EXPECT" -gt 0 ]'
assert "mock-solve ran the full matrix"      '[ "$(rowcount "$SOLVE")" -eq "$EXPECT" ]'
assert "mock-noop ran the full matrix"       '[ "$(rowcount "$NOOP")" -eq "$EXPECT" ]'
assert "mock-solve passes every cell"        '[ "$(passcount "$SOLVE")" -eq "$EXPECT" ]'
assert "mock-noop fails every cell"          '[ "$(passcount "$NOOP")" -eq 0 ]'
if eval_task_runnable "$EVAL_DIR/tasks/py-evenodd"; then
  assert "py-evenodd passes under solve"       '[ "$(cell autopilot py-evenodd "$SOLVE")" = "1" ]'
  assert "py-evenodd fails under noop"         '[ "$(cell autopilot py-evenodd "$NOOP")" = "0" ]'
else
  skip "py-evenodd cell assertions" "no uv or python3 on this host"
fi
assert "bash-slugify passes under solve"     '[ "$(cell minimal bash-slugify "$SOLVE")" = "1" ]'

# requires alternatives (`a|b` = any-of) — the syntax the py tasks rely on for uv|python3.
FAKE_TASK="$TMP/fake-task"; mkdir -p "$FAKE_TASK"
printf 'definitely-missing-xyz|bash\n' >"$FAKE_TASK/requires"
assert "requires alt: any-present is runnable" 'eval_task_runnable "$FAKE_TASK"'
printf 'definitely-missing-xyz|also-missing-abc\n' >"$FAKE_TASK/requires"
assert "requires alt: all-missing is skipped"  '! eval_task_runnable "$FAKE_TASK"'
assert "all modes appear in results"         '[ "$(tail -n +2 "$SOLVE" | cut -f1 | sort -u | wc -l | tr -d " ")" -eq "$NMODES" ]'
assert "fixtures NOT mutated by a run"       '[ "$(seed_sum)" = "$BEFORE" ]'
assert "unknown runner errors out"           '! bash "$RUN" --runner nope --out "$TMP/x.tsv" --quiet'
assert "--modes subset is respected"         '[ "$(bash "$RUN" --runner mock-solve --modes minimal --out "$TMP/m.tsv" --quiet >/dev/null 2>&1; rowcount "$TMP/m.tsv")" -eq "$NTASKS" ]'
assert "--tasks subset is respected"         '[ "$(bash "$RUN" --runner mock-solve --tasks bash-slugify --out "$TMP/t.tsv" --quiet >/dev/null 2>&1; rowcount "$TMP/t.tsv")" -eq "$NMODES" ]'
assert "bad --reps is rejected"              '! bash "$RUN" --runner mock-solve --reps abc --out "$TMP/r.tsv" --quiet'
assert "scoreboard shows overall row"        '[ -n "$(bash "$RUN" --runner mock-solve --out "$TMP/sb.tsv" 2>/dev/null | grep overall)" ]'

# --- claude runner: exit status, timeout and token accounting (fake CLI, no model) ---
# A fake `claude` is put first on PATH; its behaviour is chosen by FAKE_CLAUDE_MODE.
FAKEBIN="$TMP/fakebin"; mkdir -p "$FAKEBIN"
cat >"$FAKEBIN/claude" <<'FAKE'
#!/usr/bin/env bash
echo "fake-claude stderr line" >&2
case "${FAKE_CLAUDE_MODE:-}" in
  nonjson7) echo "this is not json"; exit 7 ;;
  sleep)    exec sleep 5 ;;
  usage)    echo '{"result":"ok","usage":{"input_tokens":37,"output_tokens":11}}' ;;
  empty)    echo '{}' ;;
  cache)    echo '{"usage":{"input_tokens":37,"output_tokens":11,"cache_read_input_tokens":500,"cache_creation_input_tokens":20}}' ;;
  *)        exit 99 ;;
esac
FAKE
chmod +x "$FAKEBIN/claude"
CTASK="$EVAL_DIR/tasks/bash-slugify"
# run_claude <mode> <tag> — runs the real runner script against the fake CLI; sets RC.
run_claude() {
  local wd="$TMP/cw-$2"; rm -rf "$wd"; mkdir -p "$wd"
  PATH="$FAKEBIN:$PATH" FAKE_CLAUDE_MODE="$1" WT_EVAL_TIMEOUT=1 \
    bash "$EVAL_DIR/runners/claude.sh" "$wd" "$CTASK" "$TMP/cm-$2" >/dev/null 2>&1
  RC=$?
}
mkey() { awk -F= -v k="$1" '$1==k{print substr($0, length(k) + 2); exit}' "$2"; }

run_claude nonjson7 n7; RC_N7=$RC
assert "claude runner: CLI exit 7 is not masked (runner nonzero)" '[ "$RC_N7" -ne 0 ]'
assert "claude runner: CLI exit 7 -> runner_exit=7"             '[ "$(mkey runner_exit "$TMP/cm-n7")" = "7" ]'
assert "claude runner: CLI exit 7 -> runner_status=error"       '[ "$(mkey runner_status "$TMP/cm-n7")" = "error" ]'
assert "claude runner: non-JSON output -> tokens=NA (not 0)"    '[ "$(mkey tokens "$TMP/cm-n7")" = "NA" ]'
assert "claude runner: CLI stderr kept in a log, not discarded" 'grep -q "fake-claude stderr line" "$TMP/cm-n7.stderr"'

if command -v timeout >/dev/null 2>&1; then
  run_claude sleep to; RC_TO=$RC
  assert "claude runner: timeout -> runner exit 124"            '[ "$RC_TO" -eq 124 ]'
  assert "claude runner: timeout -> runner_status=timeout"      '[ "$(mkey runner_status "$TMP/cm-to")" = "timeout" ]'
  assert "claude runner: timeout -> runner_exit=124"            '[ "$(mkey runner_exit "$TMP/cm-to")" = "124" ]'
  assert "claude runner: timeout -> tokens=NA"                  '[ "$(mkey tokens "$TMP/cm-to")" = "NA" ]'
else
  skip "claude runner timeout assertions" "no timeout(1) on this host"
fi

if eval_dep_met "uv|python3"; then
  run_claude usage ok; RC_OK=$RC
  assert "claude runner: valid usage -> exit 0"                 '[ "$RC_OK" -eq 0 ]'
  assert "claude runner: valid usage -> runner_status=ok"       '[ "$(mkey runner_status "$TMP/cm-ok")" = "ok" ]'
  assert "claude runner: valid usage -> tokens=48"              '[ "$(mkey tokens "$TMP/cm-ok")" = "48" ]'
  run_claude empty em
  assert "claude runner: {} (no usage) -> tokens=NA"            '[ "$(mkey tokens "$TMP/cm-em")" = "NA" ]'
  run_claude cache ca
  assert "claude runner: cache tokens not added into tokens"    '[ "$(mkey tokens "$TMP/cm-ca")" = "48" ]'
  assert "claude runner: cache_read_tokens recorded"            '[ "$(mkey cache_read_tokens "$TMP/cm-ca")" = "500" ]'
  assert "claude runner: cache_creation_tokens recorded"        '[ "$(mkey cache_creation_tokens "$TMP/cm-ca")" = "20" ]'
else
  skip "claude runner token assertions" "no uv or python3 on this host"
fi

# Harness propagation: a failing runner still gets an independent verdict, and its
# status/NA reach the results TSV (never counted as 0 tokens).
PATH="$FAKEBIN:$PATH" FAKE_CLAUDE_MODE=nonjson7 WT_EVAL_TIMEOUT=1 \
  bash "$RUN" --runner claude --modes minimal --tasks bash-slugify --out "$TMP/cl.tsv" --quiet >/dev/null 2>&1
assert "harness: failing runner row still recorded with a verdict" '[ "$(cell minimal bash-slugify "$TMP/cl.tsv")" = "0" ]'
assert "harness: failing runner tokens recorded as NA"          '[ "$(awk -F"\t" "NR==2{print \$6}" "$TMP/cl.tsv")" = "NA" ]'
assert "harness: runner_status column propagated (error)"       '[ "$(awk -F"\t" "NR==1{print \$7} NR==2{print \$7}" "$TMP/cl.tsv" | tr "\n" " ")" = "runner_status error " ]'

echo
echo "==================================================="
printf "  RESULT: %d passed, %d failed, %d skipped\n" "$PASS" "$FAIL" "$SKIP"
echo "==================================================="
[ "$FAIL" -eq 0 ]
