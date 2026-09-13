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
ck "orchestrator base_url = CLIProxyAPI"  "grep -q 'http://127.0.0.1:8317/v1' '$cfg'"
ck "delegation base_url = claude-max"     "grep -q 'http://127.0.0.1:3456/v1' '$cfg'"
ck "delegation model = opus"              "grep -q 'model: opus' '$cfg'"
ck "hermes .env has the real proxy key"   "grep -q '^CLAUDE_MAX_PROXY_API_KEY=secrettoken123$' '$henv'"
ck "hermes .env not left as placeholder"  "! grep -q '^CLAUDE_MAX_PROXY_API_KEY=local$' '$henv'"
ck "honcho.json written, rendered"        "[[ -f '$hj' ]] && ! grep -q '__[A-Z0-9_]*__' '$hj'"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]] || { echo '--- installer output ---'; cat "$tmp/out.log"; }
[[ "$fail" -eq 0 ]]
