#!/usr/bin/env bash
# test_hermes_install.sh — run scripts/hermes-install.sh against a fixture .env
# and a stub `hermes`, asserting the rendered config wires the right model and
# the real claude-max-proxy bearer (not the "local" placeholder). Never touches
# the operator's real .env (uses ENV_FILE + an isolated HERMES_HOME).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

pass=0; fail=0
ck() { if eval "$2"; then printf '  ok   %s\n' "$1"; pass=$((pass+1)); else printf '  FAIL %s\n' "$1"; fail=$((fail+1)); fi; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
stubbin="$tmp/.local/bin"; mkdir -p "$stubbin"
cat > "$stubbin/hermes" <<'EOF'
#!/usr/bin/env bash
case "$1" in --version) echo "hermes-stub 0.0.0";; esac
exit 0
EOF
chmod +x "$stubbin/hermes"

hh="$tmp/hermes"
envf="$tmp/fixture.env"
cat > "$envf" <<EOF
CLIPROXY_PORT=8317
CLIPROXY_API_KEY=cptest
CLAUDE_MAX_PROXY_PORT=3456
CLAUDE_MAX_PROXY_API_KEY=secrettoken123
HERMES_MODEL=gpt-5.6-sol
HERMES_CODING_MODEL=opus
HONCHO_PORT=8000
HONCHO_PEER_NAME=me
HONCHO_WORKSPACE=agent-ops
REPOS_DIR=$tmp/repos
HERMES_HOME=$hh
DISCORD_BOT_TOKEN=
DISCORD_ALLOWED_USERS=
DISCORD_HOME_CHANNEL=
DISCORD_FREE_RESPONSE_CHANNELS=
BROWSER_USE_API_KEY=
EOF

HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$envf" \
  HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash scripts/hermes-install.sh >"$tmp/out.log" 2>&1
rc=$?
ck "installer exits 0 (rc=$rc)" "[[ $rc -eq 0 ]]"

cfg="$hh/config.yaml"; henv="$hh/.env"; hj="$hh/honcho.json"
ck "config.yaml written"                 "[[ -f '$cfg' ]]"
ck "config.yaml has no unrendered tokens" "! grep -q '__[A-Z0-9_]*__' '$cfg'"
ck "orchestrator model = gpt-5.6-sol"     "grep -q 'default: gpt-5.6-sol' '$cfg'"
ck "orchestrator reasoning effort = high" "grep -qE '^  reasoning_effort: high\$' '$cfg'"
ck "auxiliary reasoning effort = high"    "[[ \$(grep -c 'reasoning_effort: high' '$cfg') -ge 3 ]]"
ck "orchestrator base_url = CLIProxyAPI"  "grep -q 'http://127.0.0.1:8317/v1' '$cfg'"
ck "delegation base_url = claude-max"     "grep -q 'http://127.0.0.1:3456/v1' '$cfg'"
ck "delegation model = opus"              "grep -q 'model: opus' '$cfg'"
ck "hermes .env has the real proxy key"   "grep -q '^CLAUDE_MAX_PROXY_API_KEY=secrettoken123$' '$henv'"
ck "hermes .env not left as placeholder"  "! grep -q '^CLAUDE_MAX_PROXY_API_KEY=local$' '$henv'"
ck "honcho.json written, rendered"        "[[ -f '$hj' ]] && ! grep -q '__[A-Z0-9_]*__' '$hj'"

# --- default-model scenario: with HERMES_MODEL unset the installer must fall
# back to the corrected routine-tier floor (gpt-5.6-luna), not sol. The fixture
# above keeps proving an explicit HERMES_MODEL renders through; this proves the
# install DEFAULT.
hh2="$tmp/hermes-default"
envf2="$tmp/fixture-default.env"
grep -v '^HERMES_MODEL=' "$envf" | sed "s#^HERMES_HOME=.*#HERMES_HOME=$hh2#" > "$envf2"
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$envf2" \
  HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash scripts/hermes-install.sh >"$tmp/out-default.log" 2>&1
rc2=$?
cfg2="$hh2/config.yaml"
ck "default installer exits 0 (rc=$rc2)"  "[[ $rc2 -eq 0 ]]"
ck "default orchestrator model = luna"    "grep -q 'default: gpt-5.6-luna' '$cfg2'"
# Router knobs unset in .env must default to ON (privacy is fail-closed by
# default; complexity rewrite on by default). Both fixtures above omit them.
ck "default complexity_routing = true"    "grep -qE '^        complexity_routing: true\$' '$cfg2'"
ck "default privacy_routing = true"       "grep -qE '^        privacy_routing: true\$' '$cfg2'"

# --- explicit-false scenario: MODEL_ROUTER_* set to false in .env must render
# as false (the risky opt-out), NOT be silently forced back to true. This is the
# regression the hardcoded template caused: a --force install overwrote an
# operator's deliberate `false` back to `true`.
hh3="$tmp/hermes-routing-off"
envf3="$tmp/fixture-routing-off.env"
sed "s#^HERMES_HOME=.*#HERMES_HOME=$hh3#" "$envf" > "$envf3"
cat >> "$envf3" <<EOF
MODEL_ROUTER_COMPLEXITY=false
MODEL_ROUTER_PRIVACY=false
EOF
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$envf3" \
  HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash scripts/hermes-install.sh >"$tmp/out-routing-off.log" 2>&1
rc3=$?
cfg3="$hh3/config.yaml"
ck "routing-off installer exits 0 (rc=$rc3)"  "[[ $rc3 -eq 0 ]]"
ck "explicit complexity_routing = false"      "grep -qE '^        complexity_routing: false\$' '$cfg3'"
ck "explicit privacy_routing = false"         "grep -qE '^        privacy_routing: false\$' '$cfg3'"
ck "routing-off leaves no unrendered tokens"  "! grep -q '__[A-Z0-9_]*__' '$cfg3'"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]] || { echo '--- installer output ---'; cat "$tmp/out.log"; }
[[ "$fail" -eq 0 ]]
