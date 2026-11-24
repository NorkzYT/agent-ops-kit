#!/usr/bin/env bash
# run-eval.sh — run the mode × task matrix through a runner, write a results TSV,
# and print a scoreboard. Each cell runs in its own isolated temp workdir (a fresh
# copy of the task's seed/), with the mode's overlay applied as CLAUDE.md.
#
# Isolation note: synthetic fixture tasks use temp-dir isolation. For a real-repo
# corpus, swap the per-cell setup to a `wt` worktree off a base commit.
set -uo pipefail

EVAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODES_TSV="$EVAL_DIR/modes/modes.tsv"
TASKS_DIR="$EVAL_DIR/tasks"
RUNNERS_DIR="$EVAL_DIR/runners"
# shellcheck source=lib.sh
. "$EVAL_DIR/lib.sh"

RUNNER="mock-solve"; MODES=""; TASKS=""; OUT=""; QUIET=0; REPS=1; AGENTS=0

usage() {
  cat <<EOF
run-eval.sh — measure modes against tasks

Usage: run-eval.sh [--runner <name>] [--modes "a b"] [--tasks "id id"] [--reps N] [--agents] [--out <file>] [--quiet]
  --runner   runners/<name>.sh   (default: mock-solve; real runner: claude)
  --modes    subset of modes     (default: all in modes/modes.tsv)
  --tasks    subset of task ids   (default: all dirs in tasks/)
  --reps     repetitions per (mode,task) cell, for variance (default: 1)
  --agents   symlink the kit's .claude/agents into each workdir (pipeline fidelity)
  --out      results TSV path     (default: results/run-<timestamp>.tsv)
  --quiet    suppress scoreboard (prints only the results path)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --runner) RUNNER="${2:?}"; shift 2;;
    --modes)  MODES="${2:?}";  shift 2;;
    --tasks)  TASKS="${2:?}";  shift 2;;
    --out)    OUT="${2:?}";    shift 2;;
    --quiet)  QUIET=1; shift;;
    --reps)   REPS="${2:?}";   shift 2;;
    --agents) AGENTS=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "run-eval: unknown arg '$1'" >&2; usage; exit 1;;
  esac
done

RUNNER_SH="$RUNNERS_DIR/$RUNNER.sh"
[ -f "$RUNNER_SH" ] || { echo "run-eval: no such runner '$RUNNER' ($RUNNER_SH)" >&2; exit 1; }
[[ "$REPS" =~ ^[1-9][0-9]*$ ]] || { echo "run-eval: --reps must be a positive integer (got '$REPS')" >&2; exit 1; }
[ -z "$MODES" ] && MODES="$(awk -F'\t' 'NF{print $1}' "$MODES_TSV")"
[ -z "$TASKS" ] && TASKS="$(for d in "$TASKS_DIR"/*/; do [ -d "$d" ] && basename "$d"; done)"

# Deps preflight: drop tasks whose declared host requirements are unmet (loudly),
# so a missing interpreter is reported as a skip — never as agent failures.
# shellcheck disable=SC2086
TASKS="$(eval_filter_runnable "$TASKS_DIR" $TASKS)"
[ -n "$TASKS" ] || { echo "run-eval: no runnable tasks on this host (see SKIP lines above)" >&2; exit 1; }

if [ -z "$OUT" ]; then
  mkdir -p "$EVAL_DIR/results"
  OUT="$EVAL_DIR/results/run-$(date +%Y%m%d-%H%M%S).tsv"
fi
printf 'mode\ttask\trunner\tpass\tseconds\ttokens\trunner_status\n' >"$OUT"

mode_overlay() { awk -F'\t' -v m="$1" '$1==m{print $2; exit}' "$MODES_TSV"; }

run_cell() {
  local mode="$1" task="$2" task_dir="$TASKS_DIR/$2"
  [ -d "$task_dir/seed" ] || { echo "run-eval: task '$task' has no seed/ — skipping" >&2; return; }
  local workdir metrics overlay start end secs tokens rstatus pass
  workdir="$(mktemp -d)"; metrics="$(mktemp)"
  cp -R "$task_dir/seed/." "$workdir/"
  overlay="$(mode_overlay "$mode")"
  [ -n "$overlay" ] && [ -f "$EVAL_DIR/modes/$overlay" ] && cp "$EVAL_DIR/modes/$overlay" "$workdir/CLAUDE.md"
  # Fidelity: expose the kit's real agents so a pipeline mode can actually orchestrate.
  if [ "$AGENTS" = "1" ] && [ -d "$EVAL_DIR/../agents" ]; then
    mkdir -p "$workdir/.claude"
    ln -s "$(cd "$EVAL_DIR/../agents" && pwd)" "$workdir/.claude/agents"
  fi

  start="$(date +%s)"
  bash "$RUNNER_SH" "$workdir" "$task_dir" "$metrics" >/dev/null 2>&1 || true
  end="$(date +%s)"; secs=$((end - start))
  # Runner exit status is recorded via metrics (runner_status), never allowed to
  # skip the independent verify.sh verdict below. Missing values mean "NA".
  tokens="$(awk -F= '/^tokens=/{print $2; exit}' "$metrics" 2>/dev/null || true)"; [ -n "$tokens" ] || tokens=NA
  rstatus="$(awk -F= '/^runner_status=/{print $2; exit}' "$metrics" 2>/dev/null || true)"; [ -n "$rstatus" ] || rstatus=NA
  if [ -s "$metrics.stderr" ]; then
    { printf '=== %s %s (runner_status=%s)\n' "$mode" "$task" "$rstatus"; cat "$metrics.stderr"; } >>"$OUT.stderr.log"
  fi

  if ( cd "$workdir" && bash "$task_dir/verify.sh" ) >/dev/null 2>&1; then pass=1; else pass=0; fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$mode" "$task" "$RUNNER" "$pass" "$secs" "$tokens" "$rstatus" >>"$OUT"
  rm -rf "$workdir" "$metrics" "$metrics.stderr"
}

for mode in $MODES; do
  for task in $TASKS; do
    for ((rep = 1; rep <= REPS; rep++)); do
      run_cell "$mode" "$task"
    done
  done
done

if [ "$QUIET" = "1" ]; then echo "$OUT"; exit 0; fi

echo
echo "Runner: $RUNNER"
awk -F'\t' '
NR>1 { tot[$1]++; sec[$1]+=$5; if($4==1) ok[$1]++; gt++; gs+=$5; if($4==1) gok++ }
END {
  printf "%-12s %-9s %-8s\n","MODE","PASS","AVG_S";
  n=asorti(tot, idx);
  for(i=1;i<=n;i++){ m=idx[i]; printf "%-12s %d/%-7d %.1f\n", m, ok[m]+0, tot[m], sec[m]/tot[m] }
  printf "%-12s %d/%-7d %.1f\n","(overall)", gok+0, gt, (gt? gs/gt : 0);
}' "$OUT" 2>/dev/null || \
awk -F'\t' '
NR>1 { tot[$1]++; sec[$1]+=$5; if($4==1) ok[$1]++; gt++; gs+=$5; if($4==1) gok++ }
END {
  printf "%-12s %-9s %-8s\n","MODE","PASS","AVG_S";
  for(m in tot){ printf "%-12s %d/%-7d %.1f\n", m, ok[m]+0, tot[m], sec[m]/tot[m] }
  printf "%-12s %d/%-7d %.1f\n","(overall)", gok+0, gt, (gt? gs/gt : 0);
}' "$OUT"
echo
echo "results: $OUT"
