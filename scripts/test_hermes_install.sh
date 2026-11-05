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
ck "tirith scanner enabled"               "grep -qE '^  tirith_enabled: true\$' '$cfg'"
ck "tirith scanner fails closed"          "grep -qE '^  tirith_fail_open: false\$' '$cfg'"
ck "tirith never fail-open in template"   "! grep -qE 'tirith_fail_open: true' '$cfg'"
ck "orchestrator model = gpt-5.6-sol"     "grep -q 'default: gpt-5.6-sol' '$cfg'"
ck "orchestrator reasoning effort = high" "grep -qE '^  reasoning_effort: high\$' '$cfg'"
ck "title reasoning effort = low"         "grep -A4 '^  title_generation:' '$cfg' | grep -qE '^    reasoning_effort: low\$'"
ck "config schema version = 44"           "grep -qE '^_config_version: 44\$' '$cfg'"
ck "kanban auto_decompose = true"         "grep -qE '^  auto_decompose: true\$' '$cfg'"
ck "compression reasoning effort = low"   "grep -qE '^    reasoning_effort: low\$' '$cfg'"
ck "orchestrator base_url = CLIProxyAPI"  "grep -q 'http://127.0.0.1:8317/v1' '$cfg'"
ck "delegation base_url = claude-max"     "grep -q 'http://127.0.0.1:3456/v1' '$cfg'"
ck "delegation model = opus"              "grep -q 'model: opus' '$cfg'"
ck "hermes .env has the real proxy key"   "grep -q '^CLAUDE_MAX_PROXY_API_KEY=secrettoken123$' '$henv'"
ck "hermes .env not left as placeholder"  "! grep -q '^CLAUDE_MAX_PROXY_API_KEY=local$' '$henv'"
ck "honcho.json written, rendered"        "[[ -f '$hj' ]] && ! grep -q '__[A-Z0-9_]*__' '$hj'"
ck "capability skills are activation-gated" "[[ ! -e '$hh/skills/agent-ops-kit/gstack-qa' && ! -e '$hh/skills/agent-ops-kit/problem-definition' ]]"
ck "other kit skills still install"       "[[ -f '$hh/skills/agent-ops-kit/kanban-workflow/SKILL.md' ]]"
ck "installer names the gated skills"     "grep -q 'activation-gated (docs/capability-activation.md): .*gstack-qa' '$tmp/out.log'"

# --- capability gate fails closed: in a private copy of the kit (never this checkout), a
# missing, malformed, empty or incomplete activation manifest stops the installer before it
# writes anything; a manifest whose names differ from the registry's source-implemented skills
# is refused too.
kit="$tmp/kit-copy"; mkdir -p "$kit"
tar -C "$ROOT" --exclude=./.git --exclude=./data --exclude='./.env' -cf - . | tar -C "$kit" -xf -
man="$kit/hermes/capabilities/activation/manifest.json"; cp "$man" "$tmp/manifest.orig"
gate_case() {  # label, mutation (python: m = manifest dict, or raw text via RAW=)
  local label="$1" hhx="$tmp/hermes-gate-$2"
  cp "$tmp/manifest.orig" "$man"
  case "$2" in
    missing) rm -f "$man" ;;
    malformed) printf '{"skills": [' > "$man" ;;
    dupkey) python3 - "$man" <<'PY'
import sys; p = sys.argv[1]; s = open(p).read(); open(p, "w").write(s.replace('"skills": [', '"skills": [], "skills": [', 1))
PY
    ;;
    *) python3 - "$man" "$2" <<'PY'
import json, sys
p, case = sys.argv[1:]; m = json.load(open(p))
if case == "noskills": del m["skills"]
elif case == "empty": m["skills"] = []
elif case == "dropname": m["skills"] = [s for s in m["skills"] if s != "gstack-qa"]
elif case == "extraname": m["skills"].append("kanban-workflow")
elif case == "notlist": m["skills"] = " ".join(m["skills"])
json.dump(m, open(p, "w"))
PY
    ;;
  esac
  sed "s#^HERMES_HOME=.*#HERMES_HOME=$hhx#" "$envf" > "$tmp/gate.env"
  HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$tmp/gate.env" HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
    bash "$kit/scripts/hermes-install.sh" >"$tmp/gate-$2.log" 2>&1
  local rcg=$?
  ck "gate refuses $label (rc=$rcg)" "[[ $rcg -ne 0 ]] && grep -q 'capability activation gate' '$tmp/gate-$2.log'"
  ck "gate wrote nothing for $label"  "[[ ! -e '$hhx' ]]"
}
gate_case "a missing manifest" missing
gate_case "a malformed manifest" malformed
gate_case "a duplicate-key manifest" dupkey
gate_case "a manifest without skills" noskills
gate_case "an empty skills list" empty
gate_case "a manifest missing a capability skill" dropname
gate_case "a manifest gating a non-capability skill" extraname
gate_case "a non-list skills field" notlist
cp "$tmp/manifest.orig" "$man"
# Review 2026-11-05: the routine installer copied the capability-seam model-router over the live
# plugin (no backup, no approval). With capability source present it must leave the plugin alone.
hhp="$tmp/hermes-plugin"; mkdir -p "$hhp/plugins/model-router"
printf 'PRIOR\n' > "$hhp/plugins/model-router/specialist_hook.py"
sed "s#^HERMES_HOME=.*#HERMES_HOME=$hhp#" "$envf" > "$tmp/gate.env"
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$tmp/gate.env" HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash "$kit/scripts/hermes-install.sh" >"$tmp/gate-plugin.log" 2>&1
ck "capability checkout: installer still runs (rc=$?)" "[[ -f '$hhp/config.yaml' ]]"
ck "capability checkout: live model-router left untouched" "[[ \"\$(cat '$hhp/plugins/model-router/specialist_hook.py')\" == PRIOR ]]"
ck "capability checkout: no model-router copied into a fresh home" "[[ ! -e '$hh/plugins/model-router' ]]"
ck "capability checkout: installer names the specialist-install path" "grep -q 'make specialist-install' '$tmp/gate-plugin.log'"
rm -rf "$kit/hermes/capabilities"
sed "s#^HERMES_HOME=.*#HERMES_HOME=$tmp/hermes-precap#" "$envf" > "$tmp/gate.env"
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$tmp/gate.env" HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash "$kit/scripts/hermes-install.sh" >"$tmp/gate-precap.log" 2>&1
ck "no capability source: installer still runs (rc=$?)" "[[ -f '$tmp/hermes-precap/config.yaml' ]]"
ck "no capability source: plugins install as before" "[[ -f '$tmp/hermes-precap/plugins/model-router/specialist_hook.py' ]]"

# --- default-model scenario: with HERMES_MODEL unset the installer must fall
# back to the Terra starting model (gpt-6.1-terra), not luna or sol. Luna stays
# the per-task routine floor the router rewrites to; that is not the configured
# default. The fixture above keeps proving an explicit HERMES_MODEL renders
# through; this proves the install DEFAULT.
hh2="$tmp/hermes-default"
envf2="$tmp/fixture-default.env"
grep -v '^HERMES_MODEL=' "$envf" | sed "s#^HERMES_HOME=.*#HERMES_HOME=$hh2#" > "$envf2"
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$envf2" \
  HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash scripts/hermes-install.sh >"$tmp/out-default.log" 2>&1
rc2=$?
cfg2="$hh2/config.yaml"
ck "default installer exits 0 (rc=$rc2)"  "[[ $rc2 -eq 0 ]]"
ck "default orchestrator model = terra"   "grep -qE '^  default: gpt-6.1-terra\$' '$cfg2'"
ck "default model is not luna"            "! grep -qE '^  default: gpt-5.6-luna' '$cfg2'"
# Router knobs unset in .env must default to ON (privacy is fail-closed by
# default; complexity rewrite on by default). Both fixtures above omit them.
ck "default complexity_routing = true"    "grep -qE '^        complexity_routing: true\$' '$cfg2'"
ck "default privacy_routing = true"       "grep -qE '^        privacy_routing: true\$' '$cfg2'"
# Balanced-routing knobs unset in .env must render their defaults: effort
# rewrite on, Astra opt-in per task, 30-minute task state TTL.
ck "default effort_routing = true"        "grep -qE '^        effort_routing: true\$' '$cfg2'"
ck "default coding_effort_routing = true" "grep -qE '^        coding_effort_routing: true\$' '$cfg2'"
ck "default coding_effort_ceiling = max"  "grep -qE '^        coding_effort_ceiling: max\$' '$cfg2'"
ck "default astra_policy = task"          "grep -qE '^        astra_policy: task\$' '$cfg2'"
ck "default task_ttl_seconds = 1800"      "grep -qE '^        task_ttl_seconds: 1800\$' '$cfg2'"
# Specialist-router pilot is opt-in: unset renders off with the default cap.
ck "default specialist_routing = false"   "grep -qE '^        specialist_routing: false\$' '$cfg2'"
ck "default specialist_max_profiles = 4"  "grep -qE '^        specialist_max_profiles: 4\$' '$cfg2'"
ck "default specialist_selection_mode = ai" "grep -qE '^        specialist_selection_mode: ai\$' '$cfg2'"

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
MODEL_ROUTER_EFFORT=false
MODEL_ROUTER_CODING_EFFORT=false
MODEL_ROUTER_CODING_EFFORT_CEILING=high
MODEL_ROUTER_ASTRA=never
MODEL_ROUTER_TASK_TTL=600
MODEL_ROUTER_SPECIALISTS=true
MODEL_ROUTER_SPECIALIST_MAX=2
MODEL_ROUTER_SPECIALIST_MODE=hybrid
EOF
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$envf3" \
  HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash scripts/hermes-install.sh >"$tmp/out-routing-off.log" 2>&1
rc3=$?
cfg3="$hh3/config.yaml"
ck "routing-off installer exits 0 (rc=$rc3)"  "[[ $rc3 -eq 0 ]]"
ck "explicit complexity_routing = false"      "grep -qE '^        complexity_routing: false\$' '$cfg3'"
ck "explicit privacy_routing = false"         "grep -qE '^        privacy_routing: false\$' '$cfg3'"
ck "explicit effort_routing = false"          "grep -qE '^        effort_routing: false\$' '$cfg3'"
ck "explicit coding_effort_routing = false" "grep -qE '^        coding_effort_routing: false\$' '$cfg3'"
ck "explicit coding_effort_ceiling = high"  "grep -qE '^        coding_effort_ceiling: high\$' '$cfg3'"
ck "explicit astra_policy = never"            "grep -qE '^        astra_policy: never\$' '$cfg3'"
ck "explicit task_ttl_seconds = 600"          "grep -qE '^        task_ttl_seconds: 600\$' '$cfg3'"
ck "explicit specialist_routing = true"       "grep -qE '^        specialist_routing: true\$' '$cfg3'"
ck "explicit specialist_max_profiles = 2"     "grep -qE '^        specialist_max_profiles: 2\$' '$cfg3'"
ck "explicit specialist_selection_mode = hybrid" "grep -qE '^        specialist_selection_mode: hybrid\$' '$cfg3'"
ck "routing-off leaves no unrendered tokens"  "! grep -q '__[A-Z0-9_]*__' '$cfg3'"

# --- approvals: documented deny defaults render; a fresh install has an empty
# command_allowlist; a --force re-render keeps the operator's live "Always"
# pattern keys instead of wiping them.
ck "approvals single_query_mode = deny"   "grep -qE '^  single_query_mode: deny\$' '$cfg'"
ck "approvals cron_mode = deny"           "grep -qE '^  cron_mode: deny\$' '$cfg'"
ck "fresh command_allowlist is empty"     "grep -qE '^command_allowlist: \[\]\$' '$cfg'"
ck "no yolo / approve mode rendered"      "! grep -qE '^ *[a-z_]*mode: approve\$|^ *yolo' '$cfg'"
python3 - "$cfg" <<'PY'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"^command_allowlist: \[\]$", "command_allowlist:\n  - script execution via -e/-c flag\n  - git push *", s, flags=re.M)
open(p, "w").write(s)
PY
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$envf" \
  HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash scripts/hermes-install.sh --force >"$tmp/out-force.log" 2>&1
rc4=$?
ck "--force installer exits 0 (rc=$rc4)"  "[[ $rc4 -eq 0 ]]"
ck "--force keeps live allowlist entries" "grep -qF 'command_allowlist: [\"script execution via -e/-c flag\", \"git push *\"]' '$cfg'"
ck "--force logged the kept entries"      "grep -q 'command_allowlist: keeping' '$tmp/out-force.log'"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]] || { echo '--- installer output ---'; cat "$tmp/out.log"; }
[[ "$fail" -eq 0 ]]
