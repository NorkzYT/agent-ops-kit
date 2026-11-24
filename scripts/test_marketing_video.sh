#!/usr/bin/env bash
# Offline tests for scripts/marketing-video.sh: default engine, explicit legacy
# engine, refusal of unknown engines, no fallback. Uses a stub node on PATH, so
# nothing is installed or rendered.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/mv-test.XXXXXX")"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"
cat >"$tmp/bin/node" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG"
exit "${STUB_EXIT:-0}"
STUB
chmod +x "$tmp/bin/node"
fail=0; pass=0
check() { if eval "$2"; then pass=$((pass+1)); else echo "FAIL: $1" >&2; fail=$((fail+1)); fi; }
# Default-engine cases must not see an ENGINE inherited from the caller (e.g.
# `make test-marketing-video ENGINE=remotion` exports it); explicit cases set it.
run() { env -u ENGINE STUB_LOG="$tmp/log" PATH="$tmp/bin:$PATH" bash "$here/marketing-video.sh" "$@"; }
run_engine() { local e="$1"; shift; env ENGINE="$e" STUB_LOG="$tmp/log" PATH="$tmp/bin:$PATH" bash "$here/marketing-video.sh" "$@"; }

: >"$tmp/log"; run render --out x >/dev/null 2>&1 && rc=0 || rc=$?
check "default engine is hyperframes" '[[ $rc == 0 ]] && grep -q "tools/hyperframes-marketing/scripts/render-campaign.mjs --out x" "$tmp/log"'

: >"$tmp/log"; run_engine remotion render >/dev/null 2>"$tmp/err" && rc=0 || rc=$?
check "ENGINE=remotion uses the legacy lane" '[[ $rc == 0 ]] && grep -q "tools/remotion-marketing/scripts/render-campaign.mjs" "$tmp/log" && grep -q legacy "$tmp/err"'

: >"$tmp/log"; run_engine ffmpeg render >/dev/null 2>&1 && rc=0 || rc=$?
check "unknown engine refused with exit 2, nothing run" '[[ $rc == 2 && ! -s "$tmp/log" ]]'

: >"$tmp/log"; STUB_EXIT=1 run render >/dev/null 2>&1 && rc=0 || rc=$?
check "hyperframes failure propagates; no remotion fallback" '[[ $rc == 1 ]] && [[ $(wc -l <"$tmp/log") == 1 ]] && ! grep -q remotion "$tmp/log"'

: >"$tmp/log"; run gates --out y >/dev/null 2>&1 && rc=0 || rc=$?
check "gates runs --gates-only" '[[ $rc == 0 ]] && grep -q -- "--gates-only --out y" "$tmp/log"'

: >"$tmp/log"; run_engine remotion gates >/dev/null 2>&1 && rc=0 || rc=$?
check "gates refused for remotion" '[[ $rc != 0 && ! -s "$tmp/log" ]]'

# Regression: the whole suite must also pass when the caller exports ENGINE.
if [[ -z "${MV_TEST_NESTED:-}" ]]; then
  for inherited in remotion ffmpeg; do
    MV_TEST_NESTED=1 ENGINE="$inherited" bash "${BASH_SOURCE[0]}" >/dev/null 2>"$tmp/nested" && rc=0 || rc=$?
    check "suite passes with inherited ENGINE=$inherited" '[[ $rc == 0 ]]'
    [[ $rc == 0 ]] || cat "$tmp/nested" >&2
  done
fi

echo "marketing-video dispatcher: $pass passed, $fail failed"
[[ $fail == 0 ]]
