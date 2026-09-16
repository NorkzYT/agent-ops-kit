#!/usr/bin/env bash
# route-smoke.sh — read-only smoke test for the model-router design.
#
# Proves, without touching the live gateway and without sending any private
# data anywhere:
#   1. both proxies are reachable and expose the expected model tiers;
#   2. every complexity alias (luna/terra/sol/astra, opus/fable) resolves to a
#      model id that is LIVE-available, degrading when a tier is missing;
#   3. the plugin unit + zero-egress canary suite passes.
#
# Usage: bash scripts/route-smoke.sh   (needs .env for the CLIProxy key)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"
getenv() { env_file_get "$1" .env 2>/dev/null || printf '%s' "${2:-}"; }

cp_port="$(getenv CLIPROXY_PORT 8317)"
cm_port="$(getenv CLAUDE_MAX_PROXY_PORT 3456)"
cp_key="$(getenv CLIPROXY_API_KEY)"
cm_key="$(getenv CLAUDE_MAX_PROXY_API_KEY local)"

ids() { # URL BEARER -> newline-separated model ids (empty on failure)
  local url="$1" bearer="$2"; local -a auth=()
  [[ -n "$bearer" ]] && auth=(-H "Authorization: Bearer $bearer")
  curl -fsS -m 10 "${auth[@]}" "$url" 2>/dev/null \
    | grep -o '"id":"[^"]*"' | sed 's/"id":"//; s/"$//' || true
}

log "probing CLIProxyAPI (:$cp_port) and claude-max-proxy (:$cm_port)"
GPT_IDS="$(ids "http://127.0.0.1:${cp_port}/v1/models" "$cp_key")"
CODE_IDS="$(ids "http://127.0.0.1:${cm_port}/v1/models" "$cm_key")"
[[ -n "$GPT_IDS" ]]  || die "CLIProxyAPI not reachable/authed (make auth-codex)"
[[ -n "$CODE_IDS" ]] || die "claude-max-proxy not reachable/authed (make auth-claude-proxy)"
printf '%s\n' "$GPT_IDS"  | sed 's/^/  gpt:  /'
printf '%s\n' "$CODE_IDS" | sed 's/^/  code: /'

log "resolving every complexity tier against the LIVE model set (degrade-aware)"
GPT_IDS="$GPT_IDS" CODE_IDS="$CODE_IDS" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "scripts", "tests"))
from _loader import submodule
cx = submodule("complexity")

gpt = [x for x in os.environ["GPT_IDS"].splitlines() if x.strip()]
code = [x for x in os.environ["CODE_IDS"].splitlines() if x.strip()]

def check(family, avail):
    ok = True
    for tier in cx.TIERS:
        chosen = cx.resolve_model(tier, family=family, available=avail)
        live = chosen in avail if chosen else False
        flag = "ok" if live else "MISSING"
        if not live:
            ok = False
        print(f"    {family:6} {tier:8} -> {chosen or '(none)':14} [{flag}]")
    return ok

ok = check("gpt", gpt) & check("coding", code)
# degrade demonstration: pretend the top tier vanished
degraded = cx.resolve_model("max", family="gpt", available=[m for m in gpt if m != "gpt-6-astra"])
print(f"    degrade  gpt max (astra removed) -> {degraded}")
assert degraded and degraded in gpt, "degrade did not fall back to a live tier"
sys.exit(0 if ok else 1)
PY

log "running plugin unit + zero-egress canary suite"
python3 -m unittest discover -s scripts/tests -p 'test_*.py'

log "route-smoke OK"
