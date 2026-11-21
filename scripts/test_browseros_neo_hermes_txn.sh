#!/usr/bin/env bash
# test_browseros_neo_hermes_txn.sh — offline: the hermes-enable/disable config
# transaction (scripts/lib/browseros_neo_hermes.py) against a fake `hermes`
# CLI (scripts/testdata/browseros-neo/fake-hermes.py) in throwaway HOME and
# HERMES_HOME folders. Needs no Hermes install, Docker or network.
#
# Proves: an unparseable, unreadable or wrongly shaped config is never
# reported as "not configured" and disable never claims success on it (the
# fake, like the real CLI, says "not set" for a malformed file); disable checks
# that `enabled: false` was really persisted; one lock per canonical Hermes
# home serializes enable/disable across processes and home aliases (a disable
# waits for a running enable and wins; two enables cannot roll back each
# other's work); a held lock times out with nothing changed; `enabled true` is
# issued only after `enabled false` and the full reference policy were read back
# (a set that exits 0 without persisting never enables); rollback never
# removes an entry another writer changed; child output and config values are
# redacted. The scratch folder is kept and printed (pruned by the scratch policy).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAP="$ROOT/scripts/browseros-neo.sh"
HELPER="$ROOT/scripts/lib/browseros_neo_hermes.py"
FAKE="$ROOT/scripts/testdata/browseros-neo/fake-hermes.py"
base="$HOME/.hermes/cache/scratch"; [[ -d "$base" && ! -L "$base" ]] || base="${TMPDIR:-/tmp}"
tmp="$(mktemp -d -p "$base" neo-hermes-txn.XXXXXX)" || { echo "mktemp failed" >&2; exit 1; }
trap 'touch "$tmp"/*.ctl/release 2>/dev/null; wait; echo "scratch kept: $tmp"' EXIT
pass=0; fail=0
ok() { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no() { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
SECRET="sk-NEOTESTSECRET0123456789abcdef"
PORT=19099
BROWSEROS_NEO_MCP_PORT=$PORT bash "$WRAP" hermes-snippet > "$tmp/snippet.yaml" || { echo "snippet failed" >&2; exit 1; }

seed() {  # seed HOME_NAME [extra-yaml under mcp_servers]
  mkdir -p "$tmp/$1" "$tmp/$1.user" "$tmp/$1.ctl"
  { printf 'model:\n  default: some-model\nmcp_servers:\n  other_server:\n    url: http://127.0.0.1:1/mcp\n    headers:\n      Authorization: Bearer %s\n' "$SECRET"
    printf '%s' "${2:-}"; } > "$tmp/$1/config.yaml"
}
helper() {  # helper HOME_NAME TAG args... (env prefix allowed via `env`); output -> $tmp/HOME_NAME.TAG.out
  local h="$1" t="$2"; shift 2
  HOME="$tmp/$h.user" HERMES_HOME="${HH:-$tmp/$h}" BROWSEROS_NEO_HERMES="$FAKE" FAKE_HERMES_DIR="$tmp/$h.ctl" \
    FAKE_HERMES_LOG="$tmp/$h.calls" FAKE_HERMES_TAG="$t" timeout 120 python3 -I "$HELPER" "$@" > "$tmp/$h.$t.out" 2>&1
}
entry() {  # entry HOME_NAME -> JSON of mcp_servers.browseros_neo (or null) from the file itself
  python3 -I -c 'import json,sys,yaml; print(json.dumps((yaml.safe_load(open(sys.argv[1])) or {}).get("mcp_servers",{}).get("browseros_neo")))' "$tmp/$1/config.yaml"
}
wait_for() {  # wait_for CMD... (up to 20 s)
  local _; for _ in $(seq 1 200); do "$@" && return 0; sleep 0.1; done; return 1
}
no_writes() { ! grep -qE "^$2 (config )?(set|unset)" "$tmp/$1.calls" 2>/dev/null; }
REF_ENABLED='{"enabled": true, "url": "http://127.0.0.1:'$PORT'/mcp", "trust": "untrusted", "timeout": 90, "connect_timeout": 15, "supports_parallel_tool_calls": false, "tools": {"exclude": ["credentials", "payment_cards", "solve_captcha"], "resources": false, "prompts": false}}'
same_json() { python3 -I -c 'import json,sys; a,b=(json.loads(x) for x in sys.argv[1:]); sys.exit(0 if a==b else 1)' "$1" "$2"; }

# 1. Malformed YAML that still holds enabled: true: never "not configured", never success, untouched.
seed bad "  browseros_neo:
    enabled: true
  broken: [
"
cp "$tmp/bad/config.yaml" "$tmp/bad.orig"
helper bad d disable; rc=$?
(( rc != 0 )) && grep -q 'not valid YAML' "$tmp/bad.d.out" && ! grep -qiE 'not configured|already disabled|disabled in' "$tmp/bad.d.out" \
  && cmp -s "$tmp/bad/config.yaml" "$tmp/bad.orig" && no_writes bad d \
  && ok "disable on malformed config (enabled: true inside): exits $rc, says unparseable, no write, bytes unchanged" \
  || { no "malformed disable (rc $rc)"; cat "$tmp/bad.d.out"; }
helper bad e enable "$tmp/snippet.yaml"; rc=$?
(( rc != 0 )) && grep -q 'not valid YAML' "$tmp/bad.e.out" && cmp -s "$tmp/bad/config.yaml" "$tmp/bad.orig" && no_writes bad e \
  && ok "enable on malformed config: refused, no write" || { no "malformed enable (rc $rc)"; cat "$tmp/bad.e.out"; }

# 2. Unreadable file and wrongly shaped data are errors, not absence.
if [[ "$(id -u)" != 0 ]]; then
  seed unread "  browseros_neo:
    enabled: true
"
  chmod 000 "$tmp/unread/config.yaml"
  helper unread d disable; rc=$?
  chmod 600 "$tmp/unread/config.yaml"
  (( rc != 0 )) && grep -q 'cannot read' "$tmp/unread.d.out" && ! grep -qi 'not configured' "$tmp/unread.d.out" \
    && ok "disable on unreadable config: exits $rc, says cannot read" || { no "unreadable disable (rc $rc)"; cat "$tmp/unread.d.out"; }
fi
mkdir -p "$tmp/shape" "$tmp/shape.user" "$tmp/shape.ctl"; printf 'mcp_servers: [browseros_neo]\n' > "$tmp/shape/config.yaml"
helper shape d disable; rc=$?
(( rc != 0 )) && grep -q 'not a mapping' "$tmp/shape.d.out" && ok "disable with mcp_servers not a mapping: refused" || { no "shape (rc $rc)"; cat "$tmp/shape.d.out"; }
seed nullentry "  browseros_neo:
"
helper nullentry d disable; rc=$?
(( rc != 0 )) && grep -q 'not a mapping' "$tmp/nullentry.d.out" && ok "disable with an empty (null) browseros_neo entry: refused" || { no "null entry (rc $rc)"; cat "$tmp/nullentry.d.out"; }
mkdir -p "$tmp/nofile" "$tmp/nofile.user" "$tmp/nofile.ctl"
helper nofile d disable; rc=$?
(( rc == 0 )) && grep -q 'not configured' "$tmp/nofile.d.out" && [[ ! -e "$tmp/nofile/config.yaml" ]] \
  && ok "disable with no config file: valid absence, exit 0, nothing created" || { no "no file (rc $rc)"; cat "$tmp/nofile.d.out"; }

# 3. Disable checks persistence itself: a CLI that exits 0 without writing is a failure.
seed noop "  browseros_neo:
    enabled: true
    url: http://127.0.0.1:$PORT/mcp
"
FAKE_HERMES_NOOP="set mcp_servers.browseros_neo.enabled" helper noop d disable; rc=$?
(( rc != 0 )) && grep -q 'NOT disabled' "$tmp/noop.d.out" \
  && ok "disable: CLI claims success but enabled stays true -> exit $rc, says NOT disabled" || { no "noop disable (rc $rc)"; cat "$tmp/noop.d.out"; }
helper noop d2 disable; rc=$?
(( rc == 0 )) && [[ "$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])["enabled"])' "$(entry noop)")" == False ]] \
  && grep -q 'disabled' "$tmp/noop.d2.out" && ok "disable: persisted enabled false, read back from the file" || { no "disable persisted (rc $rc)"; cat "$tmp/noop.d2.out"; }
helper noop d3 disable; rc=$?
(( rc == 0 )) && grep -q 'already disabled' "$tmp/noop.d3.out" && no_writes noop d3 \
  && ok "disable when already false: no write, exit 0" || { no "already disabled (rc $rc)"; cat "$tmp/noop.d3.out"; }

# 3b. Rollback that cannot prove disabled says so (exit 4) and prints the revoke command.
seed stuck "  browseros_neo:
    enabled: true
    url: http://127.0.0.1:$PORT/mcp
"
FAKE_HERMES_FAIL="set mcp_servers.browseros_neo.enabled" helper stuck e enable "$tmp/snippet.yaml"; rc=$?
(( rc == 4 )) && grep -q 'ROLLBACK FAILED.*NOT proven disabled' "$tmp/stuck.e.out" \
  && ok "enable whose every enabled write fails on an enabled entry: exit 4, ROLLBACK FAILED, revoke command" || { no "rollback failed (rc $rc)"; cat "$tmp/stuck.e.out"; }

# 4. Lock: a disable started during an enable waits, then wins.
seed ser
FAKE_HERMES_BLOCK="set mcp_servers.browseros_neo.trust" helper ser A enable "$tmp/snippet.yaml" & pa=$!
wait_for compgen -G "$tmp/ser.ctl/blocked.*" >/dev/null || no "enable never reached the blocked step"
helper ser B disable & pb=$!
if wait_for grep -q 'waiting for the Hermes config lock' "$tmp/ser.B.out"; then
  no_writes ser B && ! grep -qE '^B config get' "$tmp/ser.calls" \
    && ok "disable during enable: waits on the lock, has not read or written the entry" || no "disable read/wrote while enable held the lock"
else no "disable did not wait for the lock"; cat "$tmp/ser.B.out"; fi
touch "$tmp/ser.ctl/release"; wait "$pa"; ra=$?; wait "$pb"; rb=$?
python3 -I - "$tmp/ser.calls" <<'EOF'; order=$?
import sys
tags = [l.split()[0] for l in open(sys.argv[1]) if " config path" not in l]
last_a = max(i for i, t in enumerate(tags) if t == "A"); first_b = min(i for i, t in enumerate(tags) if t == "B")
sys.exit(0 if last_a < first_b else 1)
EOF
(( ra == 0 && rb == 0 && order == 0 )) && [[ "$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])["enabled"])' "$(entry ser)")" == False ]] \
  && ok "  ... enable finished first, then disable ran: final enabled false" || { no "serialized result (A $ra, B $rb, order $order): $(entry ser)"; cat "$tmp/ser.A.out" "$tmp/ser.B.out"; }

# 5. Lock: a failing enable's rollback cannot delete a later enable's entry.
seed two
FAKE_HERMES_BLOCK="set mcp_servers.browseros_neo.trust" FAKE_HERMES_FAIL="set mcp_servers.browseros_neo.trust" \
  helper two A enable "$tmp/snippet.yaml" & pa=$!
wait_for compgen -G "$tmp/two.ctl/blocked.*" >/dev/null || no "enable A never reached the blocked step"
helper two B enable "$tmp/snippet.yaml" & pb=$!
wait_for grep -q 'waiting for the Hermes config lock' "$tmp/two.B.out" || no "enable B did not wait for the lock"
touch "$tmp/two.ctl/release"; wait "$pa"; ra=$?; wait "$pb"; rb=$?
(( ra != 0 && rb == 0 )) && same_json "$(entry two)" "$REF_ENABLED" \
  && ok "two enables: A fails and rolls back first, B then succeeds; B's entry kept, enabled" || { no "two enables (A $ra, B $rb): $(entry two)"; cat "$tmp/two.A.out" "$tmp/two.B.out"; }

# 6. Lock is per canonical home: held via a symlinked alias, a short wait times out with nothing done.
seed held "  browseros_neo:
    enabled: true
    url: http://127.0.0.1:$PORT/mcp
"
ln -s "$tmp/held" "$tmp/held-alias"
lockf="$tmp/held/browseros-neo-hermes.lock"
( exec 8>>"$lockf"; flock 8; touch "$tmp/held.ctl/locked"; sleep 15 ) & lp=$!
wait_for test -e "$tmp/held.ctl/locked" || no "test could not take the lock"
HH="$tmp/held-alias" BROWSEROS_NEO_HERMES_LOCK_WAIT=1 helper held d disable; rc=$?
kill "$lp" 2>/dev/null; wait "$lp" 2>/dev/null
(( rc != 0 )) && grep -q 'lock' "$tmp/held.d.out" && no_writes held d && ! grep -qE '^d config get' "$tmp/held.calls" \
  && [[ "$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])["enabled"])' "$(entry held)")" == True ]] \
  && ok "lock held on the real home, disable via a symlinked alias: times out (rc $rc), nothing read or written" \
  || { no "held lock (rc $rc)"; cat "$tmp/held.d.out"; }

# 7. Rollback never removes an entry another writer changed during the run.
seed race
FAKE_HERMES_BLOCK="set mcp_servers.browseros_neo.timeout" FAKE_HERMES_FAIL="set mcp_servers.browseros_neo.timeout" \
  helper race A enable "$tmp/snippet.yaml" & pa=$!
wait_for compgen -G "$tmp/race.ctl/blocked.*" >/dev/null || no "enable never reached the blocked step"
HERMES_HOME="$tmp/race" python3 "$FAKE" config set mcp_servers.browseros_neo.url http://127.0.0.1:1234/mcp  # a manual writer (no lock)
touch "$tmp/race.ctl/release"; wait "$pa"; ra=$?
python3 -I - "$(entry race)" <<'EOF'; kept=$?
import json, sys
e = json.loads(sys.argv[1]); sys.exit(0 if e and e["enabled"] is False and e["url"] == "http://127.0.0.1:1234/mcp" else 1)
EOF
(( ra != 0 && kept == 0 )) && grep -q 'not removed' "$tmp/race.A.out" \
  && ok "rollback: entry changed by another writer is kept (disabled), not unset" || { no "rollback changed entry (rc $ra): $(entry race)"; cat "$tmp/race.A.out"; }

# 8. Redaction: child stderr and config values never reach the output.
seed red
FAKE_HERMES_FAIL="set mcp_servers.browseros_neo.trust" FAKE_HERMES_FAIL_MSG="boom token=$SECRET Authorization: Bearer $SECRET" \
  helper red e enable "$tmp/snippet.yaml"; rc=$?
seed redo "  browseros_neo:
    url: http://neo:pw-$SECRET@127.0.0.1:$PORT/mcp
"
helper redo e enable "$tmp/snippet.yaml"; rc2=$?
(( rc != 0 && rc2 == 3 )) && grep -q 'injected\|boom\|failed' "$tmp/red.e.out" && grep -q 'browseros_neo.url' "$tmp/redo.e.out" \
  && ! grep -q "NEOTESTSECRET" "$tmp"/*.out \
  && ok "child stderr and a refused entry's values are redacted (no secret in any output)" \
  || { no "redaction (rc $rc/$rc2)"; grep -l NEOTESTSECRET "$tmp"/*.out; }

# 9. Verification points: `enabled true` is issued only after `enabled false` and the full
#    reference policy were read back. Exclusions that the CLI "sets" without persisting, or
#    persists wrong, never get the entry enabled, not even briefly.
never_true() { ! grep -qE "^$2 config set mcp_servers\.browseros_neo\.enabled true" "$tmp/$1.calls" 2>/dev/null; }
staged() { grep -E "^$2 config set mcp_servers\.browseros_neo\." "$tmp/$1.calls" 2>/dev/null | grep -vqE '\.enabled (true|false)$'; }
seed exnoop
FAKE_HERMES_NOOP="set mcp_servers.browseros_neo.tools.exclude" helper exnoop e enable "$tmp/snippet.yaml"; rc=$?
(( rc != 0 )) && never_true exnoop e && [[ "$(entry exnoop)" == null ]] \
  && ok "exclusion set exits 0 but writes nothing: exit $rc, enabled true never issued, entry rolled back" \
  || { no "exclusion no-op (rc $rc): $(entry exnoop)"; grep -E '^e config set' "$tmp/exnoop.calls"; cat "$tmp/exnoop.e.out"; }
seed exwrong
FAKE_HERMES_WRONG="set mcp_servers.browseros_neo.tools.exclude" FAKE_HERMES_WRONG_VALUE="[credentials]" \
  helper exwrong e enable "$tmp/snippet.yaml"; rc=$?
(( rc != 0 )) && never_true exwrong e \
  && ok "exclusion persisted with a wrong (shorter) list: exit $rc, enabled true never issued" \
  || { no "exclusion wrong (rc $rc): $(entry exwrong)"; grep -E '^e config set' "$tmp/exwrong.calls"; cat "$tmp/exwrong.e.out"; }
seed trwrong
FAKE_HERMES_WRONG="set mcp_servers.browseros_neo.trust" FAKE_HERMES_WRONG_VALUE="trusted" \
  helper trwrong e enable "$tmp/snippet.yaml"; rc=$?
(( rc != 0 )) && never_true trwrong e \
  && [[ "$(python3 -I -c 'import json,sys; e=json.loads(sys.argv[1]); print(e and e["enabled"])' "$(entry trwrong)")" == False ]] \
  && ok "trust persisted wrong: exit $rc, enabled true never issued, entry left disabled" \
  || { no "trust wrong (rc $rc): $(entry trwrong)"; cat "$tmp/trwrong.e.out"; }
seed partial "  browseros_neo:
    enabled: true
    url: http://127.0.0.1:$PORT/mcp
"
cp "$tmp/partial/config.yaml" "$tmp/partial.orig"
FAKE_HERMES_NOOP="set mcp_servers.browseros_neo.enabled" helper partial e enable "$tmp/snippet.yaml"; rc=$?
(( rc != 0 )) && never_true partial e && ! staged partial e && cmp -s "$tmp/partial/config.yaml" "$tmp/partial.orig" \
  && ok "partial enabled entry, initial disable exits 0 without writing: exit $rc, no key staged, enabled true never issued" \
  || { no "partial enabled noop disable (rc $rc): $(entry partial)"; grep -E '^e config set' "$tmp/partial.calls"; cat "$tmp/partial.e.out"; }
seed order
helper order e enable "$tmp/snippet.yaml"; rc=$?
python3 -I - "$tmp/order.calls" <<'EOF2'; order=$?
import sys
c = [l.split(None, 1)[1].strip() for l in open(sys.argv[1]) if l.startswith("e ")]
B = "mcp_servers.browseros_neo"
off, on = c.index(f"config set {B}.enabled false"), c.index(f"config set {B}.enabled true")
st = [i for i, x in enumerate(c) if x.startswith(f"config set {B}.") and not x.startswith(f"config set {B}.enabled ")]
get = lambda a, b: any(x.startswith(f"config get --json {B}") for x in c[a + 1:b])
sys.exit(0 if st and off < min(st) and max(st) < on and get(off, min(st)) and get(max(st), on)
         and get(on, len(c)) and on == max(i for i, x in enumerate(c) if x.startswith("config set")) else 1)
EOF2
(( rc == 0 && order == 0 )) && same_json "$(entry order)" "$REF_ENABLED" \
  && ok "enable order: set false, read back, stage keys, read back, set true (last write), read back" \
  || { no "enable order (rc $rc, order $order)"; cat "$tmp/order.calls"; }

echo "browseros-neo hermes txn: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
