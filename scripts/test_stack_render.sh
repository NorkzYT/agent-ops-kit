#!/usr/bin/env bash
# test_stack_render.sh — render_template must substitute values literally even
# when they contain sed metacharacters (| \ &) or a GNU-sed `e`-command attempt,
# so a crafted key can never break out of the substitution or run a command.
# Regression for F14 (stack-init previously used hand-rolled unescaped sed).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
out="$tmp/config.yaml"
fail=0

# A value packed with delimiters and an `;e <cmd>` sed-execute attempt.
evil="k|;e touch $tmp/pwned|x"
export CLIPROXY_API_KEY="$evil"
export CLIPROXY_MANAGEMENT_KEY='a&b\c'
render_template "$ROOT/docker/cliproxyapi/config.example.yaml" "$out"

[[ ! -e "$tmp/pwned" ]] && echo "  ok   no command executed by sed" || { echo "  FAIL sed executed a command"; fail=1; }
grep -qF "$evil" "$out" && echo "  ok   api key rendered literally" || { echo "  FAIL api key not literal"; fail=1; }
grep -qF 'a&b\c' "$out" && echo "  ok   mgmt key (& \\) rendered literally" || { echo "  FAIL mgmt key mangled"; fail=1; }
! grep -q '__CLIPROXY' "$out" && echo "  ok   placeholders substituted" || { echo "  FAIL placeholder left"; fail=1; }

[[ "$fail" -eq 0 ]] && echo "PASS: render_template escaping" || echo "FAIL: render_template escaping"
exit "$fail"
