#!/usr/bin/env bash
# shellcheck disable=SC2034  # root/rc are read inside check's eval strings
# Offline tests for scripts/tool-pilots.sh: prerequisite checks, the explicit
# download opt-in, project-local `npm ci --ignore-scripts` only, unknown tool
# refusal, and no writes to HOME. Uses stub node/npm/chrome on PATH, so nothing
# is downloaded or installed.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/tp-test.XXXXXX")"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/home"
cat >"$tmp/bin/node" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == --version ]]; then echo "${STUB_NODE_VERSION:-v24.1.0}"; exit 0; fi
printf 'node %s\n' "$*" >>"$STUB_LOG"
# Pin readback (`node -e`): report whatever STUB_PINS says.
[[ "${1:-}" == -e ]] && { echo "${STUB_PINS:-missing}"; exit 0; }
exit 0
STUB
cat >"$tmp/bin/npm" <<'STUB'
#!/usr/bin/env bash
[[ "${1:-}" == --version ]] && { echo 11.0.0; exit 0; }
printf 'npm %s @ %s\n' "$*" "$PWD" >>"$STUB_LOG"
exit "${STUB_NPM_EXIT:-0}"
STUB
cat >"$tmp/bin/chrome" <<'STUB'
#!/usr/bin/env bash
# Like the real google-chrome wrapper, touch $HOME (mimeapps.list) on --version.
mkdir -p "$HOME/.local/share/applications" && : >"$HOME/.local/share/applications/mimeapps.list"
echo "Google Chrome 137.0.0.0"
STUB
chmod +x "$tmp/bin/"*
fail=0; pass=0
check() { if eval "$2"; then pass=$((pass+1)); else echo "FAIL: $1" >&2; fail=$((fail+1)); fi; }
run() {
  env -u TOOL_PILOTS_ALLOW_DOWNLOADS -u TOOLS HOME="$tmp/home" STUB_LOG="$tmp/log" PATH="$tmp/bin:$PATH" \
    UI_AUDIT_CHROME="$tmp/bin/chrome" MARKETING_CHROME="$tmp/bin/chrome" "$@"
}

: >"$tmp/log"; run bash "$here/tool-pilots.sh" check >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "check passes with node 24, npm, chrome" '[[ $rc == 0 ]] && grep -q "ui-audit" "$tmp/out" && grep -q "hyperframes" "$tmp/out"'
check "check reports deps not installed and the opt-in command" 'grep -q "not installed" "$tmp/out" && grep -q "TOOL_PILOTS_ALLOW_DOWNLOADS=1" "$tmp/out"'
check "check reports RTK as not provided by setup" 'grep -qi "rtk.*not installed by this setup" "$tmp/out"'
check "check runs no npm" '! grep -q "^npm" "$tmp/log"'

: >"$tmp/log"; run env STUB_NODE_VERSION=v20.11.0 bash "$here/tool-pilots.sh" check >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "node 20 fails the check with exit 1" '[[ $rc == 1 ]] && grep -q "Node 22+" "$tmp/out"'

: >"$tmp/log"; run env UI_AUDIT_CHROME="$tmp/nope" MARKETING_CHROME="$tmp/nope" bash "$here/tool-pilots.sh" check >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "missing Chrome fails the check" '[[ $rc == 1 ]] && grep -q "Chrome" "$tmp/out"'

: >"$tmp/log"; run env STUB_PINS=ok bash "$here/tool-pilots.sh" check >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "installed pins are reported" '[[ $rc == 0 ]] && grep -q "installed, pins match" "$tmp/out"'

: >"$tmp/log"; run env STUB_PINS="mismatch axe-core 4.0.0 != 4.14.1" bash "$here/tool-pilots.sh" check >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "pin mismatch fails the check" '[[ $rc == 1 ]] && grep -q "mismatch axe-core" "$tmp/out"'

: >"$tmp/log"; run bash "$here/tool-pilots.sh" install >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "install without opt-in exits 2 and runs no npm" '[[ $rc == 2 && ! -s "$tmp/log" ]] && grep -q "registry.npmjs.org" "$tmp/out"'

: >"$tmp/log"; run env TOOL_PILOTS_ALLOW_DOWNLOADS=yes bash "$here/tool-pilots.sh" install >/dev/null 2>&1 && rc=0 || rc=$?
check "opt-in must be exactly 1" '[[ $rc == 2 && ! -s "$tmp/log" ]]'

: >"$tmp/log"; run env TOOL_PILOTS_ALLOW_DOWNLOADS=1 STUB_PINS=ok bash "$here/tool-pilots.sh" install >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "opt-in install runs npm ci --ignore-scripts in each tool dir only" '[[ $rc == 0 ]] \
  && grep -qx "npm ci --ignore-scripts --no-audit --no-fund @ $root/tools/ui-audit-pilot" "$tmp/log" \
  && grep -qx "npm ci --ignore-scripts --no-audit --no-fund @ $root/tools/hyperframes-marketing" "$tmp/log" \
  && [[ $(grep -c "^npm" "$tmp/log") == 2 ]]'
check "install never uses global installs, npx or plain npm install" '! grep -Eq "npm (install|i |exec)|-g|--global|npx" "$tmp/log"'

: >"$tmp/log"; run env TOOL_PILOTS_ALLOW_DOWNLOADS=1 TOOLS=ui-audit STUB_PINS=ok bash "$here/tool-pilots.sh" install >/dev/null 2>&1 && rc=0 || rc=$?
check "TOOLS=ui-audit installs only that tool" '[[ $rc == 0 && $(grep -c "^npm" "$tmp/log") == 1 ]] && grep -q "ui-audit-pilot" "$tmp/log"'

: >"$tmp/log"; run env TOOL_PILOTS_ALLOW_DOWNLOADS=1 TOOLS=rtk bash "$here/tool-pilots.sh" install >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "unknown/unsupported tool refused with exit 2" '[[ $rc == 2 && ! -s "$tmp/log" ]]'

: >"$tmp/log"; run env TOOL_PILOTS_ALLOW_DOWNLOADS=1 STUB_NODE_VERSION=v18.0.0 bash "$here/tool-pilots.sh" install >/dev/null 2>&1 && rc=0 || rc=$?
check "install stops before npm when prerequisites fail" '[[ $rc == 1 ]] && ! grep -q "^npm" "$tmp/log"'

: >"$tmp/log"; run env TOOL_PILOTS_ALLOW_DOWNLOADS=1 STUB_NPM_EXIT=1 bash "$here/tool-pilots.sh" install >/dev/null 2>&1 && rc=0 || rc=$?
check "npm ci failure fails the install" '[[ $rc != 0 ]]'

# Upgrade path: stale pins must not block the install that fixes them.
: >"$tmp/log"; run env TOOL_PILOTS_ALLOW_DOWNLOADS=1 STUB_PINS="mismatch axe-core 4.0.0 != 4.14.1" bash "$here/tool-pilots.sh" install >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "stale pins do not block install: npm ci runs for each tool" '[[ $(grep -c "^npm ci" "$tmp/log") == 2 ]] && ! grep -q "prerequisites missing" "$tmp/out"'

# Real install simulation: real node evaluates the pins of a copied kit whose
# node_modules is stale; an npm stub "installs" by writing node_modules from
# package.json, like npm ci does from the lockfile.
if [[ "$(command -v node)" && "$(node --version)" =~ ^v(2[2-9]|[3-9][0-9]) ]]; then
  sim="$tmp/sim"; mkdir -p "$sim/scripts" "$sim/bin" "$sim/home"
  cp "$here/tool-pilots.sh" "$here/lib.sh" "$sim/scripts/"
  for d in ui-audit-pilot hyperframes-marketing; do
    mkdir -p "$sim/tools/$d/node_modules/pkg-a"
    printf '{"name":"%s","devDependencies":{"pkg-a":"2.0.0"}}\n' "$d" >"$sim/tools/$d/package.json"
    printf '{"name":"pkg-a","version":"1.0.0"}\n' >"$sim/tools/$d/node_modules/pkg-a/package.json"
    printf 'keep me\n' >"$sim/tools/$d/package-lock.json"
  done
  cat >"$sim/bin/npm" <<'STUB'
#!/usr/bin/env bash
[[ "${1:-}" == --version ]] && { echo 11.0.0; exit 0; }
printf 'npm %s @ %s\n' "$*" "$PWD" >>"$STUB_LOG"
[[ "${1:-}" == ci ]] || exit 9
[[ -n "${STUB_NPM_FAIL:-}" ]] && exit 1
rm -rf node_modules && mkdir -p node_modules/pkg-a && printf '{"name":"pkg-a","version":"2.0.0"}\n' >node_modules/pkg-a/package.json
STUB
  chmod +x "$sim/bin/npm"
  srun() {
    env -u TOOL_PILOTS_ALLOW_DOWNLOADS -u TOOLS HOME="$sim/home" STUB_LOG="$tmp/log" PATH="$sim/bin:$PATH" \
      UI_AUDIT_CHROME="$tmp/bin/chrome" MARKETING_CHROME="$tmp/bin/chrome" "$@"
  }
  srun_ff() { srun env PATH="$sim/bin:$tmp/ffbin:$PATH" "$@"; }
  mkdir -p "$tmp/ffbin"; printf '#!/bin/sh\nexit 0\n' >"$tmp/ffbin/ffmpeg"; chmod +x "$tmp/ffbin/ffmpeg"

  : >"$tmp/log"; srun_ff bash "$sim/scripts/tool-pilots.sh" check >"$tmp/out" 2>&1 && rc=0 || rc=$?
  check "sim: real pin readback reports the stale pin (exit 1)" '[[ $rc == 1 ]] && grep -q "mismatch pkg-a 1.0.0 != 2.0.0" "$tmp/out"'
  check "sim: stale pin is reported as fixable by the install" 'grep -q "tool-pilots-install" "$tmp/out"'

  : >"$tmp/log"; srun_ff env TOOL_PILOTS_ALLOW_DOWNLOADS=1 bash "$sim/scripts/tool-pilots.sh" install >"$tmp/out" 2>&1 && rc=0 || rc=$?
  check "sim: install upgrades stale pins and the final check passes" '[[ $rc == 0 ]] && [[ $(grep -c "^npm ci" "$tmp/log") == 2 ]] \
    && grep -q "installed, pins match" "$tmp/out" && ! grep -q mismatch "$tmp/out" \
    && grep -q "\"2.0.0\"" "$sim/tools/ui-audit-pilot/node_modules/pkg-a/package.json"'
  check "sim: lockfiles untouched" '[[ "$(cat "$sim/tools/ui-audit-pilot/package-lock.json")" == "keep me" ]]'

  : >"$tmp/log"; srun_ff env TOOL_PILOTS_ALLOW_DOWNLOADS=1 bash "$sim/scripts/tool-pilots.sh" install >/dev/null 2>&1 && rc=0 || rc=$?
  check "sim: install is repeatable" '[[ $rc == 0 ]]'

  for d in ui-audit-pilot hyperframes-marketing; do printf '{"name":"pkg-a","version":"1.0.0"}\n' >"$sim/tools/$d/node_modules/pkg-a/package.json"; done
  : >"$tmp/log"; srun_ff env TOOL_PILOTS_ALLOW_DOWNLOADS=1 STUB_NPM_FAIL=1 bash "$sim/scripts/tool-pilots.sh" install >"$tmp/out" 2>&1 && rc=0 || rc=$?
  check "sim: failed npm ci leaves install failing (no false ok)" '[[ $rc == 1 ]] && ! grep -q "installed, pins match" "$tmp/out"'

  : >"$tmp/log"; srun env PATH="$sim/bin:$(dirname "$(command -v node)"):/usr/bin:/bin" TOOL_PILOTS_ALLOW_DOWNLOADS=1 UI_AUDIT_CHROME="$tmp/nope" MARKETING_CHROME="$tmp/nope" bash "$sim/scripts/tool-pilots.sh" install >"$tmp/out" 2>&1 && rc=0 || rc=$?
  check "sim: missing prerequisite still stops install before npm" '[[ $rc == 1 ]] && ! grep -q "^npm" "$tmp/log" && grep -q "prerequisites missing" "$tmp/out"'
  check "sim: HOME untouched" '[[ -z "$(ls -A "$sim/home")" ]]'
else
  echo "SKIP: real-install simulation needs node 22+ on PATH" >&2
fi

check "HOME untouched by the script" '[[ -z "$(ls -A "$tmp/home")" ]]'

: >"$tmp/log"; run bash "$here/tool-pilots.sh" bogus >/dev/null 2>&1 && rc=0 || rc=$?
check "unknown command exits 2" '[[ $rc == 2 ]]'

# ui-audit-project.sh: run from a copy whose tool dir has a node_modules marker,
# with a node stub whose exit code per runner comes from STUB_EXIT_<runner>.
mkdir -p "$tmp/kit/scripts" "$tmp/kit/tools/ui-audit-pilot/node_modules" "$tmp/kit-nodeps/scripts" "$tmp/kit-nodeps/tools/ui-audit-pilot" "$tmp/pbin"
cp "$here/ui-audit-project.sh" "$here/lib.sh" "$tmp/kit/scripts/"
cp "$here/ui-audit-project.sh" "$here/lib.sh" "$tmp/kit-nodeps/scripts/"
cat >"$tmp/pbin/node" <<'STUB'
#!/usr/bin/env bash
printf 'node %s @ %s\n' "$*" "$PWD" >>"$STUB_LOG"
r="${1#scripts/run-}"; r="${r%.mjs}"; v="STUB_EXIT_$r"
exit "${!v:-0}"
STUB
chmod +x "$tmp/pbin/node"
prun() { env -u PROJECT -u PAGES -u OUT STUB_LOG="$tmp/log" PATH="$tmp/pbin:$PATH" "$@"; }

: >"$tmp/log"; prun bash "$tmp/kit/scripts/ui-audit-project.sh" >/dev/null 2>&1 && rc=0 || rc=$?
check "ui-audit-project needs PROJECT (exit 2, nothing run)" '[[ $rc == 2 && ! -s "$tmp/log" ]]'

: >"$tmp/log"; prun env PROJECT=/srv/site bash "$tmp/kit-nodeps/scripts/ui-audit-project.sh" >"$tmp/out" 2>&1 && rc=0 || rc=$?
check "ui-audit-project without deps points at the opt-in install" '[[ $rc == 1 && ! -s "$tmp/log" ]] && grep -q "TOOL_PILOTS_ALLOW_DOWNLOADS=1" "$tmp/out"'

: >"$tmp/log"; (cd "$tmp" && prun env PROJECT=/srv/my\ site PAGES="index.html,docs/a b.html" OUT=rel/out bash "$tmp/kit/scripts/ui-audit-project.sh") >/dev/null 2>&1 && rc=0 || rc=$?
check "ui-audit-project passes guarded args to all three runners" '[[ $rc == 0 && $(grep -c "^node" "$tmp/log") == 3 ]] \
  && grep -Eqx "node scripts/run-axe.mjs --project /srv/my site --page-path index.html --page-path docs/a b.html --out $tmp/rel/out --run-id [0-9]{8}T[0-9]{6}Z-[0-9]+ @ $tmp/kit/tools/ui-audit-pilot" "$tmp/log" \
  && grep -q "^node scripts/run-impeccable.mjs --project" "$tmp/log"'
check "ui-audit-project shares one run id across the three runners" '[[ $(grep -o -- "--run-id [^ ]*" "$tmp/log" | sort -u | wc -l) == 1 ]]'

# make passes variables through the environment: quotes and spaces survive.
if [[ -d "$root/tools/ui-audit-pilot/node_modules" ]] && have_make=$(command -v make); then
  q="'"
  : >"$tmp/log"; prun make -s -C "$root" ui-audit-project "PROJECT=/srv/it${q}s a site" "PAGES=a b.html" "OUT=/srv/o${q}ut" >"$tmp/out" 2>&1 && rc=0 || rc=$?
  check "make ui-audit-project keeps quotes/spaces in PROJECT/PAGES/OUT" '[[ $rc == 0 && $(grep -c "^node" "$tmp/log") == 3 ]] \
    && grep -q -- "--project /srv/it${q}s a site --page-path a b.html --out /srv/o${q}ut --run-id" "$tmp/log"'
  : >"$tmp/log"; prun make -s -C "$root" tool-pilots-check "TOOLS=bad${q}tool" >"$tmp/out" 2>&1 && rc=0 || rc=$?
  check "make tool-pilots-check passes TOOLS with a quote to the script (refused as unknown)" 'grep -q "unknown tool" "$tmp/out" && ! grep -q "unexpected EOF" "$tmp/out"'
else
  echo "SKIP: make pass-through test needs make and tools/ui-audit-pilot/node_modules" >&2
fi

: >"$tmp/log"; prun env PROJECT=/srv/site STUB_EXIT_axe=2 bash "$tmp/kit/scripts/ui-audit-project.sh" >/dev/null 2>&1 && rc=0 || rc=$?
check "findings only -> exit 2, all runners still run" '[[ $rc == 2 && $(grep -c "^node" "$tmp/log") == 3 ]]'

: >"$tmp/log"; prun env PROJECT=/srv/site STUB_EXIT_axe=2 STUB_EXIT_impeccable=1 bash "$tmp/kit/scripts/ui-audit-project.sh" >/dev/null 2>&1 && rc=0 || rc=$?
check "a tool error wins over findings -> exit 1" '[[ $rc == 1 ]]'

: >"$tmp/log"; prun env PROJECT=/srv/site STUB_EXIT_lhci=1 STUB_EXIT_impeccable=2 bash "$tmp/kit/scripts/ui-audit-project.sh" >/dev/null 2>&1 && rc=0 || rc=$?
check "error then later exit 2 stays exit 1" '[[ $rc == 1 ]]'

echo "tool-pilots tests: $pass passed, $fail failed"
[[ $fail == 0 ]]
