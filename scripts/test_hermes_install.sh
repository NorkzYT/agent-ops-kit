#!/usr/bin/env bash
# test_hermes_install.sh — run scripts/hermes-install.sh against a fixture .env
# and a stub `hermes`, asserting the rendered config wires the right model and
# the real claude-max-proxy bearer (not the "local" placeholder). Never touches
# the operator's real .env (uses ENV_FILE + an isolated HERMES_HOME).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
# Refuse unless cwd is this kit's own git checkout: the private kit copy below archives only the
# files git tracks here, never a home, cache or data tree.
[[ "$(pwd -P)" == "$(git rev-parse --show-toplevel 2>/dev/null)" && -f scripts/hermes-install.sh ]] \
  || { echo "test_hermes_install: $ROOT is not the agent-ops-kit git checkout; refusing" >&2; exit 1; }

pass=0; fail=0
ck() { if eval "$2"; then printf '  ok   %s\n' "$1"; pass=$((pass+1)); else printf '  FAIL %s\n' "$1"; fail=$((fail+1)); fi; }

# Scratch is an explicit absolute directory ($TMPDIR, else /tmp) outside the checkout.
scratch="${TMPDIR:-/tmp}"
[[ "$scratch" == /* && -d "$scratch" ]] || { echo "test_hermes_install: scratch dir '$scratch' must be an existing absolute path" >&2; exit 1; }
tmp="$(mktemp -d "$scratch/test-hermes-install.XXXXXX")" || exit 1
case "$tmp/" in "$ROOT"/*) echo "test_hermes_install: scratch $tmp is inside the checkout; refusing" >&2; rm -rf "$tmp"; exit 1 ;; esac
trap 'rm -rf "$tmp"' EXIT
# Fake HOME = $tmp: the installer prepends $HOME/.local/bin to PATH, so these stubs shadow every
# hermes/systemctl/loginctl/curl the installer could run. Each records its argv in $STUB_LOG.
stubbin="$tmp/.local/bin"; mkdir -p "$stubbin"
STUB_LOG="$tmp/stub-calls.log"; export STUB_LOG
cat > "$stubbin/hermes" <<'EOF'
#!/usr/bin/env bash
printf 'hermes %s\n' "$*" >> "${STUB_LOG:-/dev/null}"
# At gateway install/restart, record whether the router plugin is already on disk.
if [[ "$1" == gateway && -n "${HERMES_HOME:-}" ]]; then
  if [[ -f "$HERMES_HOME/plugins/model-router/__init__.py" ]]; then r=present; else r=absent; fi
  printf 'router-at-gateway-%s %s\n' "$2" "$r" >> "${STUB_LOG:-/dev/null}"
fi
case "$1" in --version) echo "hermes-stub 0.0.0";; esac
exit 0
EOF
cat > "$stubbin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "${STUB_LOG:-/dev/null}"
exit 0
EOF
cat > "$stubbin/loginctl" <<'EOF'
#!/usr/bin/env bash
printf 'loginctl %s\n' "$*" >> "${STUB_LOG:-/dev/null}"
echo Linger=yes
EOF
cat > "$stubbin/curl" <<'EOF'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "${STUB_LOG:-/dev/null}"
exit 1
EOF
chmod +x "$stubbin/hermes" "$stubbin/systemctl" "$stubbin/loginctl" "$stubbin/curl"
# The 12 capability skills the activation manifest gates (read from this checkout).
mapfile -t capskills < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["skills"]))' hermes/capabilities/activation/manifest.json)
no_capskills() {  # home -> true when none of the gated skills was copied into it
  local n; for n in "${capskills[@]}"; do [[ -e "$1/skills/agent-ops-kit/$n" ]] && return 1; done; return 0
}
router_matches_source() {  # home -> true when every source router file is installed byte-identical
  local f n=0
  while IFS= read -r -d '' f; do
    cmp -s "hermes/plugins/model-router/$f" "$1/plugins/model-router/$f" || return 1; n=$((n+1))
  done < <(cd hermes/plugins/model-router && find . -path ./__pycache__ -prune -o -type f -print0)
  (( n > 0 ))
}

hh="$tmp/hermes"
envf="$tmp/fixture.env"
cat > "$envf" <<EOF
CLIPROXY_PORT=8317
CLIPROXY_API_KEY=cptest
CLAUDE_MAX_PROXY_PORT=3456
CLAUDE_MAX_PROXY_API_KEY=secrettoken123
HERMES_MODEL=gpt-6.2-sol
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
ck "orchestrator model = gpt-6.2-sol"     "grep -q 'default: gpt-6.2-sol' '$cfg'"
ck "compression model = gpt-6.1-terra"    "grep -A3 '^  compression:' '$cfg' | grep -qE '^    model: gpt-6.1-terra\$'"
ck "compression has no default fallback"  "grep -A10 '^  compression:' '$cfg' | grep -qE '^    fallback_chain: \[\]\$'"
ck "no below-floor GPT id rendered"       "! grep -qE 'gpt-5\.|gpt-6-astra|luna' '$cfg'"
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
ck "anatomy skill installs self-contained" "[[ -f '$hh/skills/agent-ops-kit/anatomy/SKILL.md' && -f '$hh/skills/agent-ops-kit/anatomy/LICENSE' && -f '$hh/skills/agent-ops-kit/anatomy/kit/iso-kit.mjs' && -f '$hh/skills/agent-ops-kit/anatomy/scripts/chrome-offline.mjs' && ! -e '$hh/skills/agent-ops-kit/anatomy/scripts/publish.mjs' ]]"
ck "browseros-neo skill installs, Neo not enabled" "[[ -f '$hh/skills/agent-ops-kit/browseros-neo-browser/SKILL.md' ]] && ! grep -q 'browseros_neo' '$cfg'"
ck "installer names the gated skills"     "grep -q 'activation-gated (docs/capability-activation.md): .*gstack-qa' '$tmp/out.log'"

# --- capability gate fails closed: in a private copy of the kit (never this checkout), a
# missing, malformed, empty or incomplete activation manifest stops the installer before it
# writes anything; a manifest whose names differ from the registry's source-implemented skills
# is refused too.
kit="$tmp/kit-copy"; mkdir -p "$kit"
# Allowlist: only git-tracked source (working-tree content), never .git, .env, data/ or caches.
git -C "$ROOT" ls-files -z | tar -C "$ROOT" --null --no-recursion -T - -cf - | tar -C "$kit" -xf - \
  || { echo "test_hermes_install: kit copy failed" >&2; exit 1; }
[[ ! -e "$kit/.git" && ! -e "$kit/.env" && -f "$kit/scripts/hermes-install.sh" ]] || { echo "test_hermes_install: kit copy is not source-only" >&2; exit 1; }
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
# --- model-router: the routine installer installs it exactly as before the capability work
# (05a3d4f): copy over, no backup, stale extra files kept. Installing the router is not
# activation: the capability seam is inert until both opt-in settings below are added by hand.
ck "fresh home: model-router installed"     "[[ -f '$hh/plugins/model-router/__init__.py' && -f '$hh/plugins/model-router/plugin.yaml' ]]"
ck "fresh home: router files match source"  "router_matches_source '$hh'"
ck "fresh home: installer lists model-router" "grep -q 'installed plugins: .*model-router' '$tmp/out.log'"
ck "no specialist-install deflection"       "! grep -q 'model-router not installed here' '$tmp/out.log'"
ck "fresh home: 12 capability skills gated"  "[[ \${#capskills[@]} -eq 12 ]] && no_capskills '$hh'"
ck "opt-in capability root key absent"      "! grep -q 'specialist_capability_root' '$cfg'"
ck "opt-in capability catalog pin absent"   "! grep -q 'specialist_capability_catalog_sha256' '$cfg'"
hhp="$tmp/hermes-plugin"; mkdir -p "$hhp/plugins/model-router"
printf 'PRIOR\n' > "$hhp/plugins/model-router/specialist_hook.py"
printf 'STALE\n' > "$hhp/plugins/model-router/stale_extra.py"
sed "s#^HERMES_HOME=.*#HERMES_HOME=$hhp#" "$envf" > "$tmp/gate.env"
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$tmp/gate.env" HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash "$kit/scripts/hermes-install.sh" >"$tmp/gate-plugin.log" 2>&1
rcp=$?
ck "existing router: installer exits 0 (rc=$rcp)" "[[ $rcp -eq 0 ]]"
ck "existing router: overwritten with source" "router_matches_source '$hhp'"
ck "existing router: stale extra file kept (cp, no prune)" "[[ \"\$(cat '$hhp/plugins/model-router/stale_extra.py')\" == STALE ]]"
ck "existing router: no backup made (same as before)" "! compgen -G '$hhp/plugins/model-router*.bak*' >/dev/null && [[ \$(ls '$hhp/plugins') == model-router ]]"
ck "existing router: capability skills gated" "no_capskills '$hhp'"
# Gateway path with every service command stubbed: on a fresh home the router plugin is on disk
# before `hermes gateway install/restart`, and doctor runs after; nothing reaches curl.
hhg="$tmp/hermes-gateway"
sed -e "s#^HERMES_HOME=.*#HERMES_HOME=$hhg#" -e 's#^DISCORD_BOT_TOKEN=.*#DISCORD_BOT_TOKEN=stub-not-a-token#' "$envf" > "$tmp/gw.env"
: > "$STUB_LOG"
env -u _HERMES_GATEWAY -u HERMES_GATEWAY -u HERMES_GATEWAY_MODE -u HERMES_GATEWAY_SESSION -u HERMES_CRON_SESSION \
  HOME="$tmp" USER=stub PATH="$stubbin:/usr/bin:/bin" ENV_FILE="$tmp/gw.env" HERMES_SKIP_INSTALL=1 \
  bash scripts/hermes-install.sh >"$tmp/gw.log" 2>&1
rcg=$?
ck "gateway run: installer exits 0 (rc=$rcg)"  "[[ $rcg -eq 0 ]]"
ck "gateway run: router installed"             "router_matches_source '$hhg'"
ck "gateway run: router present at gateway install" "grep -qx 'router-at-gateway-install present' '$STUB_LOG'"
ck "gateway run: router present at gateway restart" "grep -qx 'router-at-gateway-restart present' '$STUB_LOG'"
ck "gateway run: doctor runs after the gateway" "awk '/^hermes gateway restart/{g=NR} /^hermes doctor/{d=NR} END{exit !(g && d > g)}' '$STUB_LOG'"
ck "gateway run: systemctl only probed the user session" "[[ \"\$(grep '^systemctl' '$STUB_LOG')\" == 'systemctl --user show-environment' ]]"
ck "gateway run: curl never called"            "! grep -q '^curl' '$STUB_LOG'"
ck "gateway run: capability skills gated"      "no_capskills '$hhg'"
# --- capability root deleted or dangling: the gate cannot be bypassed by removing
# hermes/capabilities; it refuses before any write and never copies the 12 gated skills.
cap_root_case() {  # label, tag
  local hhx="$tmp/hermes-cap-$2"
  sed "s#^HERMES_HOME=.*#HERMES_HOME=$hhx#" "$envf" > "$tmp/gate.env"
  HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$tmp/gate.env" HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
    bash "$kit/scripts/hermes-install.sh" >"$tmp/gate-cap-$2.log" 2>&1
  local rcx=$?
  ck "$1 capability root: gate refuses (rc=$rcx)" "[[ $rcx -ne 0 ]] && grep -q 'capability activation gate' '$tmp/gate-cap-$2.log'"
  ck "$1 capability root: nothing written"        "[[ ! -e '$hhx' ]]"
  ck "$1 capability root: no capability skill copied" "no_capskills '$hhx'"
}
rm -rf "$kit/hermes/capabilities"
cap_root_case deleted deleted
ln -s "$tmp/no-such-capabilities" "$kit/hermes/capabilities"
cap_root_case dangling dangling

# --- default-model scenario: with HERMES_MODEL unset the installer must fall
# back to the Terra starting model (gpt-6.1-terra), not sol. Terra is also the
# routine floor; nothing below GPT 6.1 Terra is rendered. The fixture above keeps proving an explicit HERMES_MODEL renders
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

# --- model-floor scenario: a stale explicit GPT override below 6.1/Terra in
# .env (each key the installer renders or forwards) is refused before anything
# is written; the error names the key and the migration planner, never a secret.
floor_case() {  # label, KEY=VALUE line
  local hhf="$tmp/hermes-floor-$1" envff="$tmp/fixture-floor-$1.env" key="${2%%=*}"
  grep -v "^$key=" "$envf" | sed "s#^HERMES_HOME=.*#HERMES_HOME=$hhf#" > "$envff"
  printf '%s\n' "$2" >> "$envff"
  HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$envff" \
    HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
    bash scripts/hermes-install.sh >"$tmp/out-floor-$1.log" 2>&1
  local rcf=$?
  ck "floor $1: refused (rc=$rcf)"          "[[ $rcf -ne 0 ]]"
  ck "floor $1: no config written"          "[[ ! -e '$hhf/config.yaml' ]]"
  ck "floor $1: names key + migration"      "grep -q '$key=' '$tmp/out-floor-$1.log' && grep -q 'model-floor-migrate.py' '$tmp/out-floor-$1.log'"
  ck "floor $1: no secret in output"        "! grep -qE 'cptest|secrettoken123' '$tmp/out-floor-$1.log'"
}
floor_case hermes-luna    "HERMES_MODEL=gpt-5.6-luna"
floor_case hermes-typo    "HERMES_MODEL=gpt-6-1-terra"
floor_case aux-comp       "AUX_COMPRESSION_MODEL=gpt-5.6-terra"
floor_case aux-comp-fb    "AUX_COMPRESSION_FALLBACK_MODEL=gpt-5.6-luna"
floor_case honcho         "HONCHO_MODEL=gpt-5.6-terra"
floor_case classifier     "MODEL_ROUTER_SPECIALIST_CLASSIFIER_MODEL=gpt-5.6-sol"
floor_case gpt-list       "MODEL_ROUTER_GPT_MODELS=gpt-6.1-terra,gpt-6-astra"
floor_case aux-title      "AUX_MODEL=gpt-5.6-luna"
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

# --- Mac clone root: a value set directly in $HERMES_HOME/.env survives a re-install
# whose kit .env leaves HERMES_MAC_REPO_ROOT unset; a kit value is forwarded.
printf 'HERMES_MAC_REPO_ROOT=/Users/mac-operator/src\n' >> "$henv"
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$envf" HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash scripts/hermes-install.sh >"$tmp/out-macroot.log" 2>&1
ck "mac root: existing value kept"        "grep -qx 'HERMES_MAC_REPO_ROOT=/Users/mac-operator/src' '$henv'"
printf 'HERMES_MAC_REPO_ROOT=/Users/mac-operator/Documents\n' >> "$envf"
HOME="$tmp" PATH="/usr/bin:/bin" ENV_FILE="$envf" HERMES_SKIP_INSTALL=1 HERMES_SKIP_GATEWAY=1 \
  bash scripts/hermes-install.sh >"$tmp/out-macroot2.log" 2>&1
ck "mac root: kit value forwarded"        "grep -qx 'HERMES_MAC_REPO_ROOT=/Users/mac-operator/Documents' '$henv'"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]] || { echo '--- installer output ---'; cat "$tmp/out.log"; }
[[ "$fail" -eq 0 ]]
