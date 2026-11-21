#!/usr/bin/env bash
# test_browseros_neo_hermes_enable.sh — opt-in: the hermes-enable/hermes-disable
# config transaction (scripts/lib/browseros_neo_hermes.py) against the real
# Hermes CLI, with explicit throwaway HOME and HERMES_HOME (one of each, the
# config reseeded per case: a fresh HOME makes Hermes prepare its dependencies
# once per data root).
#
# Proves: the wrapper refuses without --approve-activation or when the gate
# cannot be verified (stub Docker, Hermes never called); enabled false first
# and enabled true last; unrelated config preserved; second run is a no-op; an
# existing entry with operator values is refused untouched; a failing step
# rolls back (entry removed when it was absent, kept disabled when it existed);
# disable keeps the entry; a MALFORMED config holding enabled: true is never
# reported as "not configured" by disable or enable, exits non-zero and is not
# written. The gate checks themselves are covered offline by
# test_browseros_neo.sh, the lock and redaction by
# test_browseros_neo_hermes_txn.sh. Never reads or writes the live Hermes home;
# starts no containers. Skips (exit 0) without Hermes. The scratch folder is
# kept and printed (pruned by the scratch policy).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAP="$ROOT/scripts/browseros-neo.sh"
HELPER="$ROOT/scripts/lib/browseros_neo_hermes.py"
REAL="$(command -v hermes)" || { echo "skip: hermes not installed"; exit 0; }
base="$HOME/.hermes/cache/scratch"; [[ -d "$base" && ! -L "$base" ]] || base="${TMPDIR:-/tmp}"
tmp="$(mktemp -d -p "$base" neo-hermes-enable.XXXXXX)" || { echo "mktemp failed" >&2; exit 1; }
trap 'echo "scratch kept: $tmp"' EXIT
pass=0; fail=0
ok() { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no() { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
port=19098
HH="$tmp/hermes-home"; UH="$tmp/user-home"; mkdir -p "$HH" "$UH"
BROWSEROS_NEO_MCP_PORT=$port bash "$WRAP" hermes-snippet > "$tmp/snippet.yaml" || { echo "snippet failed" >&2; exit 1; }
cat > "$tmp/docker-stub" <<'EOF2'
#!/usr/bin/env bash
echo "docker stub: no engine in this test" >&2; exit 1
EOF2
chmod +x "$tmp/docker-stub"

# A shim in front of the real CLI: logs each call, can fail one chosen `config set` key.
cat > "$tmp/hermes-shim" <<EOF2
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\$SHIM_LOG"
if [[ -n "\${SHIM_FAIL_KEY:-}" && "\$1 \$2 \$3" == "config set \$SHIM_FAIL_KEY" ]]; then echo "injected failure" >&2; exit 9; fi
exec "$REAL" "\$@"
EOF2
chmod +x "$tmp/hermes-shim"

seed() {  # seed CASE [extra-yaml] -> reseed the one throwaway home with unrelated config
  { printf 'model:\n  default: some-model\n  provider: custom\nmcp_servers:\n  other_server:\n    url: http://127.0.0.1:1/mcp\n    enabled: true\n'; printf '%s' "${2:-}"; } > "$HH/config.yaml"
  : > "$tmp/$1.calls"
}
henv() { env HOME="$UH" HERMES_HOME="$HH" SHIM_LOG="$tmp/$CASE.calls" BROWSEROS_NEO_HERMES="$tmp/hermes-shim" "$@"; }
neo() {  # neo CASE args... : the wrapper (output -> $tmp/CASE.out)
  CASE="$1"; shift
  henv BROWSEROS_NEO_DOCKER="$tmp/docker-stub" BROWSEROS_NEO_MCP_PORT="$port" timeout 300 bash "$WRAP" "$@" > "$tmp/$CASE.out" 2>&1
}
txn() {  # txn CASE enable|disable : the config transaction the wrapper runs after its gate checks
  CASE="$1"; shift
  if [[ "$1" == enable ]]; then set -- enable "$tmp/snippet.yaml"; fi
  henv timeout 300 python3 -I "$HELPER" "$@" > "$tmp/$CASE.out" 2>&1
}
check() {  # check MODE -> python assertion on the parsed config
  python3 -I - "$HH/config.yaml" "$1" "$port" <<'EOF2'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])); mode = sys.argv[2]; port = sys.argv[3]
assert cfg["model"] == {"default": "some-model", "provider": "custom"}, cfg
assert cfg["mcp_servers"]["other_server"] == {"url": "http://127.0.0.1:1/mcp", "enabled": True}, cfg
neo = cfg["mcp_servers"].get("browseros_neo")
want = {"url": f"http://127.0.0.1:{port}/mcp", "trust": "untrusted", "timeout": 90, "connect_timeout": 15,
        "supports_parallel_tool_calls": False,
        "tools": {"exclude": ["credentials", "payment_cards", "solve_captcha"], "resources": False, "prompts": False}}
if mode == "absent": assert neo is None, neo
elif mode == "enabled": assert neo == dict(want, enabled=True), neo
elif mode == "disabled": assert neo == dict(want, enabled=False), neo
else: raise SystemExit(f"unknown mode {mode}")
EOF2
}

# Warm-up with a valid config: a fresh data root prepares Hermes' dependencies on its first
# call, and that preparation itself fails on an unparseable config (the helper then refuses).
seed warmup; env HOME="$UH" HERMES_HOME="$HH" timeout 900 "$REAL" config path > "$tmp/warmup.out" 2>&1 \
  || { echo "hermes warm-up failed; see $tmp/warmup.out" >&2; exit 1; }

# 1. Wrapper refusals leave the config untouched and never call Hermes.
seed refuse; cp "$HH/config.yaml" "$tmp/refuse.orig"
neo refuse hermes-enable; rc1=$?
neo refuse hermes-enable --yes; rc2=$?
neo refuse hermes-enable --approve-activation; rc3=$?
(( rc1 != 0 && rc2 != 0 && rc3 != 0 )) && cmp -s "$HH/config.yaml" "$tmp/refuse.orig" && [[ ! -s "$tmp/refuse.calls" ]] \
  && ok "no flag / wrong flag / gate not verifiable: refused, Hermes never called, config untouched" || no "refusals ($rc1 $rc2 $rc3)"

# 1b. Malformed config holding enabled: true (real CLI says "not set" for it): never success.
seed malformed "  browseros_neo:
    enabled: true
    url: http://127.0.0.1:$port/mcp
  broken: [
"
cp "$HH/config.yaml" "$tmp/malformed.orig"
neo malformed hermes-disable; rc1=$?; cp "$tmp/malformed.out" "$tmp/malformed-wrapper.out"
txn malformed disable; rc2=$?; cp "$tmp/malformed.out" "$tmp/malformed-disable.out"
txn malformed enable; rc3=$?
(( rc1 != 0 && rc2 != 0 && rc3 != 0 )) && cmp -s "$HH/config.yaml" "$tmp/malformed.orig" \
  && grep -q 'not valid YAML' "$tmp/malformed-wrapper.out" && grep -q 'NOT proven disabled' "$tmp/malformed-wrapper.out" \
  && ! grep -qiE 'not configured|already disabled|disabled in' "$tmp/malformed-wrapper.out" "$tmp/malformed-disable.out" \
  && ! grep -q '^config \(set\|unset\)' "$tmp/malformed.calls" \
  && ok "malformed config with enabled: true: hermes-disable/disable/enable exit non-zero ($rc1/$rc2/$rc3), say unparseable, no write" \
  || { no "malformed (rc $rc1/$rc2/$rc3)"; cat "$tmp/malformed-wrapper.out" "$tmp/malformed.out"; }
REAL_GET="$(env HOME="$UH" HERMES_HOME="$HH" "$REAL" config get --json mcp_servers.browseros_neo 2>&1)"
[[ "$REAL_GET" == *"not set"* ]] && ok "  ... (control: the real CLI itself reports the malformed entry as not set)" || no "  ... control: real CLI said: $REAL_GET"

# 2. Enable: order, result, unrelated config kept.
seed main
txn main enable && check enabled \
  && ok "enable: reference entry enabled; model and other_server unchanged" || { no "enable"; cat "$tmp/main.out"; }
python3 -I - "$tmp/main.calls" <<'EOF' && ok "enable: 'enabled false' is the first write, 'enabled true' the last, exclusions before it" || no "enable order: $(grep 'config set' "$tmp/main.calls" | tr '\n' '|')"
import sys
sets = [l.split()[2:] for l in open(sys.argv[1]) if l.startswith("config set ")]
assert sets[0] == ["mcp_servers.browseros_neo.enabled", "false"], sets
assert sets[-1] == ["mcp_servers.browseros_neo.enabled", "true"], sets
assert [k for k, *_ in sets].index("mcp_servers.browseros_neo.tools.exclude") < len(sets) - 1
assert sum(k == "mcp_servers.browseros_neo.enabled" for k, *_ in sets) == 2, sets
EOF

# 3. Idempotent.
cp "$HH/config.yaml" "$tmp/main.after1"; : > "$tmp/main.calls"
txn main enable && cmp -s "$HH/config.yaml" "$tmp/main.after1" \
  && ! grep -q '^config set' "$tmp/main.calls" && grep -q 'nothing changed' "$tmp/main.out" \
  && ok "second enable: no write, config bytes identical" || { no "idempotent enable"; cat "$tmp/main.out"; }

# 4. Disable keeps the entry; disable twice is fine; enable again restores.
txn main disable && check disabled && txn main disable && check disabled \
  && ok "disable (twice): entry kept with enabled false; unrelated config unchanged" || { no "disable"; cat "$tmp/main.out"; }
txn main enable && check enabled \
  && ok "enable after disable: re-enabled" || { no "re-enable"; cat "$tmp/main.out"; }
seed nodisable
txn nodisable disable && check absent && grep -q 'not configured' "$tmp/nodisable.out" \
  && ok "disable with no entry: no-op, no entry created" || { no "disable absent"; cat "$tmp/nodisable.out"; }

# 5. Operator override is refused untouched.
seed override "  browseros_neo:
    url: http://127.0.0.1:$port/mcp
    enabled: false
    tools:
      exclude: [credentials]
"
cp "$HH/config.yaml" "$tmp/override.orig"
txn override enable; rc=$?
(( rc != 0 )) && cmp -s "$HH/config.yaml" "$tmp/override.orig" && grep -q 'tools.exclude' "$tmp/override.out" \
  && ok "existing entry with different exclusions: refused, names the key, config bytes unchanged" || { no "override refusal (rc $rc)"; cat "$tmp/override.out"; }

# 6. Rollback: absent before -> removed; disabled before -> stays disabled; never enabled.
seed rb1
SHIM_FAIL_KEY=mcp_servers.browseros_neo.tools.exclude txn rb1 enable; rc=$?
(( rc != 0 )) && check absent && grep -q 'rolled back: entry removed' "$tmp/rb1.out" \
  && ok "step failure with no prior entry: rolled back, entry removed, unrelated config kept" || { no "rollback absent (rc $rc)"; cat "$tmp/rb1.out"; }
seed rb2 "  browseros_neo:
    enabled: false
    url: http://127.0.0.1:$port/mcp
"
SHIM_FAIL_KEY=mcp_servers.browseros_neo.enabled txn rb2 enable; rc=$?
python3 -I -c 'import sys,yaml; n=yaml.safe_load(open(sys.argv[1]))["mcp_servers"]["browseros_neo"]; assert n.get("enabled") is False, n' "$HH/config.yaml" 2>/dev/null
rc_cfg=$?
(( rc != 0 && rc_cfg == 0 )) && grep -q 'rolled back to disabled' "$tmp/rb2.out" && ! grep -q 'ROLLBACK FAILED' "$tmp/rb2.out" \
  && ok "every enabled write failing, entry already false: exits non-zero, disabled proven from the file" || { no "rollback disabled (rc $rc)"; cat "$tmp/rb2.out"; }
seed rb3
SHIM_FAIL_KEY=mcp_servers.browseros_neo.trust txn rb3 enable; rc=$?
check absent; rc3a=$?
seed rb3b "  browseros_neo:
    enabled: false
    url: http://127.0.0.1:$port/mcp
"
SHIM_FAIL_KEY=mcp_servers.browseros_neo.trust txn rb3b enable; rc2=$?
python3 -I -c 'import sys,yaml; n=yaml.safe_load(open(sys.argv[1]))["mcp_servers"]["browseros_neo"]; assert n["enabled"] is False and "trust" not in n, n' "$HH/config.yaml" 2>/dev/null
rc_cfg=$?
(( rc != 0 && rc2 != 0 && rc_cfg == 0 && rc3a == 0 )) && grep -q 'rolled back to disabled' "$tmp/rb3b.out" \
  && ok "mid-sequence failure with a prior disabled entry: kept disabled, never enabled" || { no "rollback prior (rc $rc/$rc2)"; cat "$tmp/rb3b.out"; }

grep -rqs -i -E 'api_key|token|secret|password' "$tmp/main.after1" && no "entry carries no secrets" || ok "entry carries no secrets"
echo "browseros-neo hermes enable: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
