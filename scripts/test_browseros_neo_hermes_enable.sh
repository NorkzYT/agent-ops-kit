#!/usr/bin/env bash
# test_browseros_neo_hermes_enable.sh — opt-in: `browseros-neo.sh hermes-enable` /
# `hermes-disable` against the real Hermes CLI in a throwaway HERMES_HOME.
#
# Proves: refused without --approve-activation or while the MCP port is closed
# (config untouched); enabled false first and enabled true last; unrelated
# config preserved byte-for-byte in meaning; second run is a no-op; an existing
# entry with operator values is refused untouched; a failing step rolls back
# (entry removed when it was absent, kept disabled when it existed); disable
# keeps the entry. A stdlib TCP listener stands in for the gate. Never reads or
# writes the live Hermes home; starts no containers. Skips (exit 0) without
# Hermes. The scratch folder is kept and printed (pruned by the scratch policy).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAP="$ROOT/scripts/browseros-neo.sh"
REAL="$(command -v hermes)" || { echo "skip: hermes not installed"; exit 0; }
base="$HOME/.hermes/cache/scratch"; [[ -d "$base" && ! -L "$base" ]] || base="${TMPDIR:-/tmp}"
tmp="$(mktemp -d -p "$base" neo-hermes-enable.XXXXXX)" || { echo "mktemp failed" >&2; exit 1; }
listener_pid=""
trap '[[ -n "$listener_pid" ]] && kill "$listener_pid" 2>/dev/null; echo "scratch kept: $tmp"' EXIT
pass=0; fail=0
ok() { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no() { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

free_port() { python3 -I -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
port="$(free_port)"; closed="$(free_port)"
timeout 600 python3 -I -c 'import socket,sys
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(16)
while True: s.accept()[0].close()' "$port" & listener_pid=$!
for _ in $(seq 1 50); do python3 -I -c 'import socket,sys; socket.create_connection(("127.0.0.1",int(sys.argv[1])),1)' "$port" 2>/dev/null && break; sleep 0.1; done

# A shim in front of the real CLI: logs each call, can fail one chosen `config set` key.
cat > "$tmp/hermes-shim" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\$SHIM_LOG"
if [[ -n "\${SHIM_FAIL_KEY:-}" && "\$1 \$2 \$3" == "config set \$SHIM_FAIL_KEY" ]]; then echo "injected failure" >&2; exit 9; fi
exec "$REAL" "\$@"
EOF
chmod +x "$tmp/hermes-shim"

seed() {  # seed HOME_NAME [extra-yaml] -> a home with unrelated config
  mkdir -p "$tmp/$1"
  { printf 'model:\n  default: some-model\n  provider: custom\nmcp_servers:\n  other_server:\n    url: http://127.0.0.1:1/mcp\n    enabled: true\n'; printf '%s' "${2:-}"; } > "$tmp/$1/config.yaml"
}
neo() {  # neo HOME_NAME PORT args... (stdout+stderr -> $tmp/HOME_NAME.out)
  local h="$1" p="$2"; shift 2
  HERMES_HOME="$tmp/$h" SHIM_LOG="$tmp/$h.calls" BROWSEROS_NEO_HERMES="$tmp/hermes-shim" \
    BROWSEROS_NEO_MCP_PORT="$p" timeout 300 bash "$WRAP" "$@" > "$tmp/$h.out" 2>&1
}
check() {  # check HOME_NAME MODE -> python assertion on the parsed config
  python3 -I - "$tmp/$1/config.yaml" "$2" "$port" <<'EOF'
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
EOF
}

# 1. Refusals leave the config untouched.
seed refuse; cp "$tmp/refuse/config.yaml" "$tmp/refuse.orig"
neo refuse "$port" hermes-enable; rc1=$?
neo refuse "$port" hermes-enable --yes; rc2=$?
neo refuse "$closed" hermes-enable --approve-activation; rc3=$?
(( rc1 != 0 && rc2 != 0 && rc3 != 0 )) && cmp -s "$tmp/refuse/config.yaml" "$tmp/refuse.orig" && [[ ! -s "$tmp/refuse.calls" ]] \
  && ok "no flag / wrong flag / closed MCP port: refused, Hermes never called, config untouched" || no "refusals ($rc1 $rc2 $rc3)"

# 2. Enable: order, result, unrelated config kept.
seed main
neo main "$port" hermes-enable --approve-activation && check main enabled \
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
cp "$tmp/main/config.yaml" "$tmp/main.after1"; : > "$tmp/main.calls"
neo main "$port" hermes-enable --approve-activation && cmp -s "$tmp/main/config.yaml" "$tmp/main.after1" \
  && ! grep -q '^config set' "$tmp/main.calls" && grep -q 'nothing changed' "$tmp/main.out" \
  && ok "second enable: no write, config bytes identical" || { no "idempotent enable"; cat "$tmp/main.out"; }

# 4. Disable keeps the entry; disable twice is fine; enable again restores.
neo main "$port" hermes-disable && check main disabled && neo main "$port" hermes-disable && check main disabled \
  && ok "disable (twice): entry kept with enabled false; unrelated config unchanged" || { no "disable"; cat "$tmp/main.out"; }
neo main "$port" hermes-enable --approve-activation && check main enabled \
  && ok "enable after disable: re-enabled" || { no "re-enable"; cat "$tmp/main.out"; }
seed nodisable
neo nodisable "$port" hermes-disable && check nodisable absent && grep -q 'not configured' "$tmp/nodisable.out" \
  && ok "disable with no entry: no-op, no entry created" || { no "disable absent"; cat "$tmp/nodisable.out"; }

# 5. Operator override is refused untouched.
seed override "  browseros_neo:
    url: http://127.0.0.1:$port/mcp
    enabled: false
    tools:
      exclude: [credentials]
"
cp "$tmp/override/config.yaml" "$tmp/override.orig"
neo override "$port" hermes-enable --approve-activation; rc=$?
(( rc != 0 )) && cmp -s "$tmp/override/config.yaml" "$tmp/override.orig" && grep -q 'tools.exclude' "$tmp/override.out" \
  && ok "existing entry with different exclusions: refused, names the key, config bytes unchanged" || { no "override refusal (rc $rc)"; cat "$tmp/override.out"; }

# 6. Rollback: absent before -> removed; disabled before -> stays disabled; never enabled.
seed rb1
SHIM_FAIL_KEY=mcp_servers.browseros_neo.tools.exclude neo rb1 "$port" hermes-enable --approve-activation; rc=$?
(( rc != 0 )) && check rb1 absent && grep -q 'rolled back: entry removed' "$tmp/rb1.out" \
  && ok "step failure with no prior entry: rolled back, entry removed, unrelated config kept" || { no "rollback absent (rc $rc)"; cat "$tmp/rb1.out"; }
seed rb2 "  browseros_neo:
    enabled: false
    url: http://127.0.0.1:$port/mcp
"
SHIM_FAIL_KEY=mcp_servers.browseros_neo.enabled neo rb2 "$port" hermes-enable --approve-activation; rc=$?
python3 -I -c 'import sys,yaml; n=yaml.safe_load(open(sys.argv[1]))["mcp_servers"]["browseros_neo"]; assert n.get("enabled") is False, n' "$tmp/rb2/config.yaml" 2>/dev/null
rc_cfg=$?
(( rc != 0 && rc_cfg == 0 )) && grep -q 'ROLLBACK FAILED' "$tmp/rb2.out" \
  && ok "every enabled write failing: exits non-zero, entry still disabled, prints the revoke command" || { no "rollback disabled (rc $rc)"; cat "$tmp/rb2.out"; }
seed rb3
SHIM_FAIL_KEY=mcp_servers.browseros_neo.trust neo rb3 "$port" hermes-enable --approve-activation; rc=$?
seed rb3b "  browseros_neo:
    enabled: false
    url: http://127.0.0.1:$port/mcp
"
SHIM_FAIL_KEY=mcp_servers.browseros_neo.trust neo rb3b "$port" hermes-enable --approve-activation; rc2=$?
python3 -I -c 'import sys,yaml; n=yaml.safe_load(open(sys.argv[1]))["mcp_servers"]["browseros_neo"]; assert n["enabled"] is False and "trust" not in n, n' "$tmp/rb3b/config.yaml" 2>/dev/null
rc_cfg=$?
(( rc != 0 && rc2 != 0 && rc_cfg == 0 )) && check rb3 absent && grep -q 'rolled back to disabled' "$tmp/rb3b.out" \
  && ok "mid-sequence failure with a prior disabled entry: kept disabled, never enabled" || { no "rollback prior (rc $rc/$rc2)"; cat "$tmp/rb3b.out"; }

grep -rqs -i -E 'api_key|token|secret|password' "$tmp"/main/config.yaml && no "entry carries no secrets" || ok "entry carries no secrets"
echo "browseros-neo hermes enable: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
