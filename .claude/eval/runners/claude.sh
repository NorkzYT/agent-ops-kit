#!/usr/bin/env bash
# Real runner — drives `claude -p` against the task prompt inside the workdir.
# The active mode overlay has already been written to <workdir>/CLAUDE.md by the
# harness, so Claude Code picks it up. Gated on the claude CLI being installed.
# args: <workdir> <task_dir> <metrics_file>
set -euo pipefail
# shellcheck source=../lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
workdir="$1"; task_dir="$2"; metrics="${3:-/dev/null}"
case "$metrics" in /*) ;; *) metrics="$PWD/$metrics" ;; esac
# Model-process stderr goes to a log beside the metrics file (never /dev/null'd).
if [ "$metrics" = /dev/null ]; then errlog=/dev/null; else errlog="$metrics.stderr"; fi
command -v claude >/dev/null 2>&1 || {
  echo "[claude-runner] claude CLI not found" >&2
  printf 'runner_status=missing\nrunner_exit=2\ntokens=NA\n' >"$metrics"
  exit 2
}

prompt="$(cat "$task_dir/task.md")"
cd "$workdir"
# Headless run in the isolated workdir. skip-permissions avoids hangs on prompts
# (the dir is a throwaway fixture); a timeout bounds a stuck session.
TIMEOUT="${WT_EVAL_TIMEOUT:-240}"
runner=(claude -p "$prompt" --dangerously-skip-permissions --output-format json)
rc=0
if command -v timeout >/dev/null 2>&1; then
  out="$(timeout "$TIMEOUT" "${runner[@]}" 2>"$errlog")" || rc=$?
else
  out="$("${runner[@]}" 2>"$errlog")" || rc=$?
fi
# Status is recorded, not masked; the harness still runs the task's own verify.sh.
if [ "$rc" -eq 0 ]; then status=ok
elif [ "$rc" -eq 124 ] && command -v timeout >/dev/null 2>&1; then status=timeout
else status=error
fi

# Token keys: tokens = input+output; cache_* recorded separately (never summed in).
# Anything unparseable or without numeric usage is "NA" (unmeasured), never 0.
usage_kv=""
if eval_dep_met "uv|python3"; then
  usage_kv="$(printf '%s' "$out" | eval_python -c '
import sys, json
def num(u, k):
    v = u.get(k)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
try:
    d = json.load(sys.stdin)
except Exception:
    d = None
u = d.get("usage") if isinstance(d, dict) else None
u = u if isinstance(u, dict) else {}
i, o = num(u, "input_tokens"), num(u, "output_tokens")
print("tokens=%s" % (i + o if i is not None and o is not None else "NA"))
for src, dst in (("cache_read_input_tokens", "cache_read_tokens"),
                 ("cache_creation_input_tokens", "cache_creation_tokens")):
    v = num(u, src)
    if v is not None:
        print("%s=%d" % (dst, v))
' 2>>"$errlog" || true)"
fi
case "$usage_kv" in tokens=*) ;; *) usage_kv="tokens=NA" ;; esac  # no toolchain / parser crash
printf 'runner_status=%s\nrunner_exit=%s\n%s\n' "$status" "$rc" "$usage_kv" >"$metrics"
exit "$rc"
