#!/usr/bin/env bash
# test_hermes_runtime_patch.sh — scripts/hermes-runtime-patch.sh against a throwaway git
# "runtime" checkout and a fixture patch directory: status/check/apply states, idempotent
# apply, refusal on a dirty target, unrelated dirty files ignored, conflict reporting, and
# source separation (profile HOME/HERMES_HOME, passwd home, linked worktrees, subdirectory
# --src, no git index writes), and the review regressions: every patch target (deletions,
# renames, quoted/unicode names) checked staged/unstaged/untracked, no repo clean filter run,
# valid JSON for quote/pipe/tab names, core.worktree identity mismatch refused; second round:
# assume-unchanged/skip-worktree targets, ambiguous both-direction patches, whole-set refusal
# (non-UTF-8 dirty name, chained same-file patches), locale-independent order, split index;
# third round: JSON encoding before any write (non-UTF-8 --src/patch name), repeated blocks
# and duplicate patches never shifted, proven resume of a partly applied checkout and
# check == apply verdict, non-literal ENV_FILE HERMES_HOME refused, terminal-safe text; fourth
# round: ENV_FILE lines where env_file_get disagrees refused, lost stdout report = exit 4;
# fifth round: canonical .env not hidden by ENV_FILE, relative ENV_FILE at kit root and cwd
# (checked against the real installer reader), missing/unreadable/NUL config refused, >&- = exit 4.
# Identity comes from a fixture `getent` on PATH and fixture
# homes, and the script runs from a temp kit copy; it never touches ~/.hermes, /etc/passwd or the real .env.
# PATCH_SCRIPT=<path> runs the same suite against another copy (red/green checks).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
pass=0; fail=0
# ck NAME CODE: CODE is eval'd, so it must reference captured output as \$out (escaped), never
# interpolate it: command output is data, not test code.
ck() { if eval "$2"; then printf '  ok   %s\n' "$1"; pass=$((pass+1)); else printf '  FAIL %s\n' "$1"; fail=$((fail+1)); fi; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
tmp="$(readlink -f "$tmp")"
# Test-side git never reads the operator's global/system config.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
src="$tmp/runtime"; patches="$tmp/patches"; mkdir -p "$patches"
g() { git -C "$src" -c user.email=t@example.com -c user.name=t -c commit.gpgsign=false "$@"; }
git init -q -b main "$src"
printf 'a = 1\n' > "$src/agent.py"; printf 'other\n' > "$src/unrelated.py"
g add . >/dev/null; g commit -q -m "base"
# fixture patch 1: edits agent.py; fixture patch 2: adds tests/new_test.py
printf 'a = 2\n' > "$src/agent.py"; g commit -q -am "feat: a=2"
mkdir -p "$src/tests"; printf 'ok\n' > "$src/tests/new_test.py"; g add tests; g commit -q -m "test: add"
g format-patch -q -2 --no-signature -o "$patches" HEAD >/dev/null
g reset -q --hard HEAD~2            # back to base: both patches applicable
printf 'local change\n' >> "$src/unrelated.py"   # an unrelated dirty file, like memory_manager.py

# Fixture identity: getent answers from $FAKE_PW_HOME (empty = no passwd entry).
bin="$tmp/bin"; mkdir -p "$bin" "$tmp/pwhome" "$tmp/nohome"
cat > "$bin/getent" <<'EOF'
#!/bin/sh
[ "$1" = passwd ] && [ -n "${FAKE_PW_HOME:-}" ] || exit 2
echo "fixture:x:$(id -u):$(id -g)::$FAKE_PW_HOME:/bin/sh"
EOF
chmod +x "$bin/getent"
# The script runs from a throwaway kit copy: it always reads its kit's own .env, so running it
# in place would read the operator's real one. The installers' readers are copied alongside.
kit="$tmp/kit"; mkdir -p "$kit/scripts" "$kit/hermes/profiles/envt"
cp "$(readlink -f "${PATCH_SCRIPT:-scripts/hermes-runtime-patch.sh}")" "$kit/scripts/hermes-runtime-patch.sh"
cp scripts/lib.sh scripts/hermes-profile.sh scripts/knowledge-sync.sh "$kit/scripts/"
printf 'fixture\n' > "$kit/hermes/profiles/envt/SOUL.md"
SCRIPT="$kit/scripts/hermes-runtime-patch.sh"
# mkman PATCHES_DIR SRC_OR_EMPTY OUT [status]: a SYNTHETIC fixture manifest (base = SRC's HEAD at
# call time, every patch in C order hash-pinned, review status default "reviewed" with fixture
# evidence). It exists only so the simulation tests reach the simulator; it is never kit data.
mkman() {
  python3 - "$@" <<'PYM'
import hashlib, json, os, subprocess, sys
d, src, out = sys.argv[1], sys.argv[2], sys.argv[3]
st = sys.argv[4] if len(sys.argv) > 4 else "reviewed"
base = "0" * 40
if src:
    r = subprocess.run(["git", "-C", src, "rev-parse", "--verify", "HEAD^{commit}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode == 0:
        base = r.stdout.decode().strip()
names = sorted(n for n in os.listdir(os.fsencode(d)) if n.endswith(b".patch")) if os.path.isdir(d) else []
rev = {"status": st}
if st == "reviewed":
    rev.update(verdict="approved", reviewer="SYNTHETIC-FIXTURE", reference="test fixture, not a real review", date="1970-01-01")
hs = [hashlib.sha256(open(os.path.join(os.fsencode(d), n), "rb").read()).hexdigest() for n in names]
json.dump({"schema": "hermes-runtime-patches/v1", "base": base, "patches": [
    {"file": os.fsdecode(n), "sha256": h, "review": dict(rev, sha256=h) if st == "reviewed" else dict(rev)}
    for n, h in zip(names, hs)]}, open(out, "w"))
PYM
}
# run [VAR=value ...] -- args: fixture environment, then the overrides. Unless the args name a
# --manifest (or NOMAN=1), a synthetic reviewed manifest for --patches/--src is generated first.
run() {
  local ov=() a s="" p="" i=0
  while [[ $# -gt 0 && "$1" != "--" ]]; do ov+=("$1"); shift; done
  shift
  local args=("$@")
  if [[ "${NOMAN:-0}" != 1 && " ${args[*]} " != *" --manifest "* ]]; then
    for ((i = 0; i < ${#args[@]}; i++)); do
      a="${args[i]}"
      [[ "$a" == --src ]] && s="${args[i+1]:-}"
      [[ "$a" == --patches ]] && p="${args[i+1]:-}"
    done
    [[ -n "$p" ]] || p="$kit/hermes/runtime-patches"
    mkman "$p" "$s" "$tmp/auto-manifest.json" && set -- "$@" --manifest "$tmp/auto-manifest.json"
  fi
  env -u HERMES_HOME -u HERMES_REAL_HOME -u ENV_FILE PATH="$bin:$PATH" HOME="$tmp/nohome" XDG_CONFIG_HOME="$tmp/xdg" \
    FAKE_PW_HOME="$tmp/pwhome" "${ov[@]}" bash "$SCRIPT" "$@"
}
S="run --"

out="$($S status --src "$src" --patches "$patches")"; rc=$?
ck "status exits 0 when all applicable (rc=$rc)"   "[[ $rc -eq 0 ]]"
ck "status lists both as applicable"               "[[ \$(grep -c 'applicable' <<< \"\$out\") -eq 2 ]]"
ck "check passes when applicable"                  "$S check --src '$src' --patches '$patches' >/dev/null"

out="$($S apply --src "$src" --patches "$patches")"; rc=$?
ck "apply exits 0 (rc=$rc)"                        "[[ $rc -eq 0 ]]"
ck "apply reports two applied"                     "grep -q 'applied now: 2' <<< \"\$out\""
ck "agent.py patched"                              "grep -q 'a = 2' '$src/agent.py'"
ck "new test file created"                         "[[ -f '$src/tests/new_test.py' ]]"
ck "unrelated dirty file untouched"                "grep -q 'local change' '$src/unrelated.py'"
ck "nothing committed"                             "[[ \$(g rev-list --count HEAD) -eq 1 ]]"
out="$($S status --src "$src" --patches "$patches" --json)"
n_applied="$(grep -o '"state":"applied"' <<< "$out" | wc -l)"
ck "json status says applied x2 (got $n_applied)"  "[[ $n_applied -eq 2 ]]"
out="$($S apply --src "$src" --patches "$patches")"; rc=$?
ck "second apply is a no-op (rc=$rc)"              "[[ $rc -eq 0 ]] && grep -q 'applied now: 0' <<< \"\$out\""

# dirty target: revert agent.py to base and leave an uncommitted edit on it -> refused
g checkout -q -- agent.py; printf 'a = 1\nmine\n' > "$src/agent.py"
out="$($S apply --src "$src" --patches "$patches")"; rc=$?
ck "apply refuses a dirty/conflicting target (rc=$rc)" "[[ $rc -ne 0 ]]"
ck "agent.py left as the operator had it"          "grep -q '^mine$' '$src/agent.py'"
# a committed divergence of the target makes the patch a conflict: reported, never forced
printf 'b = 3\n' > "$src/agent.py"; g commit -q -am "diverge"
ck "conflict reported by check"                    "! $S check --src '$src' --patches '$patches' >/dev/null"
out="$($S apply --src "$src" --patches "$patches")"; rc=$?
ck "apply refuses the conflict and fails (rc=$rc)" "[[ $rc -ne 0 ]] && grep -q 'refused: conflict' <<< \"\$out\""
ck "diverged file untouched"                       "grep -q '^b = 3$' '$src/agent.py'"
ck "usage error without a command"                 "! $S --src '$src' >/dev/null 2>&1"
ck "missing src is an error"                       "! $S status --src '$tmp/nope' >/dev/null 2>&1"

# Source separation: apply never targets the installed runtime (default, direct path,
# symlink alias, subdirectory). Simulated with a throwaway home; status stays read-only.
home="$tmp/home"; inst="$home/.hermes/hermes-agent"; mkdir -p "$home/.hermes"
gi() { git -C "$inst" -c user.email=t@example.com -c user.name=t -c commit.gpgsign=false "$@"; }
git init -q -b main "$inst"
printf 'a = 1\n' > "$inst/agent.py"; printf 'other\n' > "$inst/unrelated.py"; mkdir -p "$inst/sub"; : > "$inst/sub/.keep"
gi add . >/dev/null; gi commit -q -m base
printf 'local change\n' >> "$inst/unrelated.py"
ln -s "$inst" "$tmp/alias"
# a linked worktree of the installed checkout: its metadata lives in the installed .git
gi worktree add -q -b wt "$tmp/wt" 2>/dev/null
# a gitdir alias: a work tree whose .git file points at the installed git dir
mkdir -p "$tmp/gitalias"; echo "gitdir: $inst/.git" > "$tmp/gitalias/.git"
# an isolated clone whose core.worktree points into the installed checkout
git clone -q "$inst" "$tmp/cwt"; git -C "$tmp/cwt" config core.worktree "$inst"
# stale index stat info: a plain `git status` would refresh and rewrite .git/index
touch -d '1 hour ago' "$inst/agent.py" "$inst/sub/.keep"
snap() { (cd "$inst" && find .git -type f -exec sha256sum {} + | sort; find .git -printf '%p %T@\n' | sort; sha256sum agent.py unrelated.py; stat -c '%n %Y' agent.py); }
before="$(snap)"
P="--patches $patches"
I="run HOME=$home FAKE_PW_HOME=$home --"
idx="$(sha256sum < "$inst/.git/index")"
$I status $P >/dev/null; $I check --src "$inst/sub" $P >/dev/null
ck "status/check leave the installed .git/index unchanged" "[[ \"\$idx\" == \"\$(sha256sum < '$inst/.git/index')\" ]]"
$I apply $P >/dev/null 2>&1; rc=$?
ck "apply without --src refused (rc=$rc)"          "[[ $rc -eq 3 ]]"
$I apply --src "$inst" $P >/dev/null 2>&1; rc=$?
ck "apply --src installed refused (rc=$rc)"        "[[ $rc -eq 3 ]]"
$I apply --src "$tmp/alias" $P >/dev/null 2>&1; rc=$?
ck "apply --src symlink alias refused (rc=$rc)"    "[[ $rc -eq 3 ]]"
$I apply --src "$tmp/alias/sub" $P >/dev/null 2>&1; rc=$?
ck "apply --src installed subdir refused (rc=$rc)" "[[ $rc -eq 3 ]]"
mkdir -p "$tmp/hh"; ln -s "$inst" "$tmp/hh/hermes-agent"
run HERMES_HOME="$tmp/hh" HOME="$home" -- apply --src "$inst" $P >/dev/null 2>&1; rc=$?
ck "HERMES_HOME alias + ~/.hermes both refused (rc=$rc)" "[[ $rc -eq 3 ]]"

# Profile sessions: HERMES_HOME=<root>/profiles/<name>, HOME=$HERMES_HOME/home.
prof="$home/.hermes/profiles/coder"; mkdir -p "$prof/home"
run HERMES_HOME="$prof" HOME="$prof/home" FAKE_PW_HOME="$home" -- apply --src "$inst" $P >/dev/null 2>&1; rc=$?
ck "profile HOME/HERMES_HOME: passwd home still refused (rc=$rc)" "[[ $rc -eq 3 ]]"
run HOME="$prof/home" HERMES_REAL_HOME="$home" -- apply --src "$inst" $P >/dev/null 2>&1; rc=$?
ck "profile HOME: HERMES_REAL_HOME refused (rc=$rc)" "[[ $rc -eq 3 ]]"
run HERMES_HOME="$prof" HOME="$prof/home" -- apply --src "$tmp/alias" $P >/dev/null 2>&1; rc=$?
ck "profile HERMES_HOME: profile root refused (rc=$rc)" "[[ $rc -eq 3 ]]"
printf 'HERMES_HOME="%s"\n' "$home/.hermes" > "$tmp/cfg.env"
run HOME="$prof/home" ENV_FILE="$tmp/cfg.env" -- apply --src "$inst" $P >/dev/null 2>&1; rc=$?
ck "configured HERMES_HOME (ENV_FILE) refused (rc=$rc)" "[[ $rc -eq 3 ]]"

# Shared git metadata: linked worktree, gitdir alias, core.worktree into the installed tree.
$I apply --src "$tmp/wt" $P >/dev/null 2>&1; rc=$?
ck "linked worktree of installed refused (rc=$rc)" "[[ $rc -eq 3 ]]"
run HOME="$prof/home" FAKE_PW_HOME="$home" -- apply --src "$tmp/wt/sub" $P >/dev/null 2>&1; rc=$?
ck "linked worktree subdir refused under profile HOME (rc=$rc)" "[[ $rc -eq 3 ]]"
$I apply --src "$tmp/gitalias" $P >/dev/null 2>&1; rc=$?
ck "gitdir alias of installed .git refused (rc=$rc)" "[[ $rc -eq 3 ]]"
$I apply --src "$tmp/cwt" $P >/dev/null 2>&1; rc=$?
ck "core.worktree into installed refused (rc=$rc)" "[[ $rc -ne 0 ]]"
ck "linked worktree left unpatched"                "grep -q '^a = 1$' '$tmp/wt/agent.py'"

# Unverifiable identity disables apply entirely, even for a genuinely isolated clone.
iso0="$tmp/iso0"; git clone -q "$inst" "$iso0"
run HOME="$home" FAKE_PW_HOME= -- apply --src "$iso0" $P >/dev/null 2>&1; rc=$?
ck "no passwd entry: apply disabled (rc=$rc)"      "[[ $rc -eq 3 ]]"
run HOME="$home" FAKE_PW_HOME="$tmp/missing" -- apply --src "$iso0" $P >/dev/null 2>&1; rc=$?
ck "passwd home missing: apply disabled (rc=$rc)"  "[[ $rc -eq 3 ]]"
ck "isolated clone untouched while disabled"       "grep -q '^a = 1$' '$iso0/agent.py' && [[ ! -e '$iso0/tests' ]]"
run HOME="$home" FAKE_PW_HOME= -- status --src "$iso0" $P >/dev/null 2>&1; rc=$?
ck "status still works without passwd identity (rc=$rc)" "[[ $rc -eq 0 ]]"

# Read-only inspection of the installed checkout, including via subdirectory and profile HOME.
out="$($I status $P)"; rc=$?
ck "status still inspects installed default (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q 'applicable' <<< \"\$out\""
out="$(run HERMES_HOME="$prof" HOME="$prof/home" FAKE_PW_HOME="$home" -- status $P)"; rc=$?
ck "profile status defaults to the real installed runtime" "[[ $rc -eq 0 ]] && grep -q '^runtime: $inst @' <<< \"\$out\""
$I check --src "$tmp/alias" $P >/dev/null; rc=$?
ck "check via alias stays allowed (rc=$rc)"        "[[ $rc -eq 0 ]]"
$I status --src "$inst/sub" $P >/dev/null; rc=$?
ck "status via installed subdir allowed (rc=$rc)"  "[[ $rc -eq 0 ]]"
after="$(snap)"
ck "installed checkout + .git byte-identical (no index refresh)" "[[ \"\$before\" == \"\$after\" ]]"
ck "installed unrelated dirty change preserved"    "grep -q 'local change' '$inst/unrelated.py'"

# Subdirectory --src acts on the canonical toplevel (git apply would skip outside paths).
iso="$tmp/isolated"; git clone -q "$inst" "$iso"; printf 'mine\n' > "$iso/scratch.txt"
out="$($I status --src "$iso/sub" $P)"
ck "subdir status reports applicable, not applied" "[[ \$(grep -c 'applicable' <<< \"\$out\") -eq 2 ]] && grep -q '^runtime: $iso @' <<< \"\$out\""
printf 'a = 1\nmine\n' > "$iso/agent.py"
$I apply --src "$iso/sub" $P >/dev/null 2>&1; rc=$?
ck "subdir apply sees a dirty target at toplevel (rc=$rc)" "[[ $rc -ne 0 ]] && grep -q '^mine$' '$iso/agent.py'"
# Whole-set preflight: the refused first patch stops the clean second one too (no partial set).
ck "refused patch blocks the whole set (no partial apply)" "[[ ! -e '$iso/tests/new_test.py' ]]"
git -C "$iso" checkout -q -- agent.py; rm -rf "$iso/tests"
out="$(GIT_DIR="$inst/.git" GIT_WORK_TREE="$inst" GIT_INDEX_FILE="$inst/.git/index" $I apply --src "$iso/sub" $P)"; rc=$?
ck "subdir apply patches the toplevel (rc=$rc)"    "[[ $rc -eq 0 ]] && grep -q 'applied now: 2' <<< \"\$out\" && grep -q '^a = 2$' '$iso/agent.py' && [[ -f '$iso/tests/new_test.py' ]]"
ck "isolated untracked file preserved"             "grep -q '^mine$' '$iso/scratch.txt'"
ck "caller GIT_DIR/GIT_WORK_TREE ignored: installed unpatched" "grep -q '^a = 1$' '$inst/agent.py' && [[ \"\$before\" == \"\$(snap)\" ]]"
out="$($I status --src "$iso/sub" $P)"
ck "subdir status after apply says applied x2"     "[[ \$(grep -c 'applied' <<< \"\$out\") -eq 2 ]]"

# Review regressions (each was a bypass or wrong output in the previous version; run with
# PATCH_SCRIPT=<old copy> to see them fail). Fixture repo with ten-line files so a patch to
# line 1 still applies over a local change to line 10.
base="$tmp/secbase"; git init -q -b main "$base"
gb() { git -C "$base" -c user.email=t@example.com -c user.name=t -c commit.gpgsign=false "$@"; }
for f in del.py old.py 'sp ä.py' 'q"|p	t.py' mod.py unrelated.py; do seq 1 10 > "$base/$f"; done
gb add . >/dev/null; gb commit -q -m base
sp="$tmp/secpatches"; mkdir -p "$sp/del" "$sp/ren" "$sp/uni" "$sp/json" "$sp/mod" "$sp/new"
gb rm -q del.py; gb commit -q -m del; gb format-patch -q -1 --no-signature -o "$sp/del" HEAD >/dev/null
gb mv old.py new.py; gb commit -q -m ren; gb format-patch -q -1 -M --no-signature -o "$sp/ren" HEAD >/dev/null
sed -i '1s/.*/uni/' "$base/sp ä.py"; gb commit -q -am uni; gb format-patch -q -1 --no-signature -o "$sp/uni" HEAD >/dev/null
sed -i '1s/.*/js/' "$base/q\"|p	t.py"; gb commit -q -am js; gb format-patch -q -1 --no-signature --stdout HEAD > "$sp/json/0009-q\"|p.patch"
sed -i '1s/.*/mod/' "$base/mod.py"; gb commit -q -am mod; gb format-patch -q -1 --no-signature -o "$sp/mod" HEAD >/dev/null
printf 'n\n' > "$base/created.py"; gb add created.py; gb commit -q -m new; gb format-patch -q -1 --no-signature -o "$sp/new" HEAD >/dev/null
gb reset -q --hard HEAD~6
ck "fixture: rename patch has no +++ line"         "! grep -q '^+++ ' '$sp'/ren/*.patch && grep -q '^rename from old.py' '$sp'/ren/*.patch"
ck "fixture: unicode patch path is quoted"          "grep -q '^+++ \"b/sp ' '$sp'/uni/*.patch"
n=0
fresh() { n=$((n+1)); r="$tmp/sec$n"; git clone -q "$base" "$r"; }
gr() { git -C "$r" -c user.email=t@example.com -c user.name=t "$@"; }
# staged change: index differs from HEAD (worktree restored to HEAD content when $2=head)
stage() { printf 'local\n' >> "$r/$1"; gr add -- "$1"; [[ "${2:-}" != head ]] || gr show "HEAD:$1" > "$r/$1"; }

fresh; stage del.py head
$I apply --src "$r" --patches "$sp/del" >/dev/null 2>&1; rc=$?
ck "deletion of a target with a staged change refused (rc=$rc)" "[[ $rc -ne 0 && -f '$r/del.py' ]]"
fresh; stage old.py
$I apply --src "$r" --patches "$sp/ren" >/dev/null 2>&1; rc=$?
ck "rename of a staged-only source refused (rc=$rc)" "[[ $rc -ne 0 && -f '$r/old.py' && ! -e '$r/new.py' ]]"
fresh; printf 'x\n' > "$r/new.py"; echo new.py > "$r/.git/info/exclude"
out="$($I status --src "$r" --patches "$sp/ren" --json)"
ck "ignored file at a rename destination reported" "python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"patches\"][0][\"dirty_targets\"]==[\"new.py\"]' \"\$out\""
fresh; stage 'sp ä.py'
$I apply --src "$r" --patches "$sp/uni" >/dev/null 2>&1; rc=$?
ck "quoted unicode target with a staged change refused (rc=$rc)" "[[ $rc -ne 0 ]] && grep -q '^1$' '$r/sp ä.py'"
fresh; stage mod.py
$I apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "staged-only target (worktree == index) refused (rc=$rc)" "[[ $rc -ne 0 ]] && grep -q '^1$' '$r/mod.py'"
fresh; printf 'mine\n' > "$r/created.py"
out="$($I apply --src "$r" --patches "$sp/new" --json)"; rc=$?
ck "untracked file at a creation target refused (rc=$rc)" "[[ $rc -ne 0 ]] && grep -q '^mine$' '$r/created.py' && python3 -c 'import json,sys; assert json.loads(sys.argv[1])[\"patches\"][0][\"dirty_targets\"]==[\"created.py\"]' \"\$out\""
fresh
out="$($I apply --src "$r" --patches "$sp/ren")"; rc=$?
ck "clean rename still applies (rc=$rc)"           "[[ $rc -eq 0 && -f '$r/new.py' && ! -e '$r/old.py' ]]"
out="$($I apply --src "$r" --patches "$sp/uni")"; rc=$?
ck "clean quoted unicode target still applies (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^uni$' '$r/sp ä.py'"

# JSON: quote, pipe and tab in a dirty target and in the patch file name.
fresh; printf 'local\n' >> "$r/q\"|p	t.py"
out="$($I status --src "$r" --patches "$sp/json" --json)"; rc=$?
ck "json with quote/pipe/tab names parses (rc=$rc)" "python3 -c 'import json,sys; d=json.loads(sys.argv[1]); p=d[\"patches\"][0]; assert p[\"patch\"]==\"0009-q\\\"|p.patch\" and p[\"dirty_targets\"]==[\"q\\\"|p\\tt.py\"], p' \"\$out\""
out="$($I apply --src "$r" --patches "$sp/json" --json)"; rc=$?
ck "json apply refusal parses and names the target" "[[ $rc -ne 0 ]] && python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"exit\"]==1 and d[\"patches\"][0][\"action\"].startswith(\"refused: dirty\")' \"\$out\""

# Repo-configured clean filters never run: not for an unrelated stat-dirty file (no git
# status), and a filtered target makes the patch unsafe before anything reads it.
mark="$tmp/FILTER_RAN"
fresh; gr config filter.evil.clean "sh -c 'touch $mark; cat'"; gr config filter.evil.smudge cat
echo 'unrelated.py filter=evil' > "$r/.git/info/attributes"; touch -d '1 hour ago' "$r/unrelated.py"
$I status --src "$r" --patches "$sp/mod" >/dev/null 2>&1; $I check --src "$r" --patches "$sp/mod" >/dev/null 2>&1
ck "status/check run no clean filter (unrelated file)" "[[ ! -e '$mark' ]]"
$I apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "apply runs no clean filter, still applies (rc=$rc)" "[[ $rc -eq 0 && ! -e '$mark' ]] && grep -q '^mod$' '$r/mod.py'"
fresh; gr config filter.evil.clean "sh -c 'touch $mark; cat'"; echo '*.py filter=evil' > "$r/.git/info/attributes"
touch -d '1 hour ago' "$r/mod.py"
out="$($I status --src "$r" --patches "$sp/mod")"
ck "filtered target reported unsafe"               "grep -q 'unsafe' <<< \"\$out\""
$I check --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "check fails on a filtered target (rc=$rc)"     "[[ $rc -ne 0 ]]"
$I apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "apply refuses a filtered target (rc=$rc)"      "[[ $rc -ne 0 ]] && grep -q '^1$' '$r/mod.py'"
ck "no clean filter ran for a filtered target"     "[[ ! -e '$mark' ]]"

# Unenumerable targets fail closed: copy patches and a garbled header.
cp_dir="$tmp/cppatch"; mkdir -p "$cp_dir"
sed -e 's/^rename from /copy from /' -e 's/^rename to /copy to /' "$sp"/ren/*.patch > "$cp_dir/0001-copy.patch"
fresh; $I apply --src "$r" --patches "$cp_dir" >/dev/null 2>&1; rc=$?
ck "copy patch refused as unsafe (rc=$rc)"         "[[ $rc -ne 0 && ! -e '$r/new.py' ]]"
$I check --src "$r" --patches "$cp_dir" >/dev/null 2>&1; rc=$?
ck "check fails on an unenumerable patch (rc=$rc)" "[[ $rc -ne 0 ]]"

# Identity: --src's own git dir must be the one that runs its toplevel.
fresh; git init -q "$r/inner"; git -C "$r/inner" config core.worktree ../..
$I status --src "$r/inner" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "core.worktree to another repo's tree refused for status (rc=$rc)" "[[ $rc -eq 3 ]]"
$I apply --src "$r/inner" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "core.worktree to another repo's tree refused for apply (rc=$rc)" "[[ $rc -eq 3 ]] && grep -q '^1$' '$r/mod.py'"
fresh; git -C "$r" worktree add -q -b wt2 "$tmp/secwt$n" 2>/dev/null
$I apply --src "$tmp/secwt$n" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "isolated linked worktree still allowed (rc=$rc)" "[[ $rc -eq 0 ]]"

# Second review round. mkp DIR NAME CMD: run CMD in a scratch clone of $base, commit, and
# write that commit as DIR/NAME (CMD is fixture code, not output).
mkp() { local d="$1" nm="$2" w="$tmp/mkp$RANDOM"; git clone -q "$base" "$w"; (cd "$w" && eval "$3") &&
  git -C "$w" -c user.email=t@example.com -c user.name=t -c commit.gpgsign=false commit -q -am fixture &&
  mkdir -p "$d" && git -C "$w" format-patch -q -1 --no-signature --stdout HEAD > "$d/$nm"; rm -rf "$w"; }

# assume-unchanged / skip-worktree hide a local edit from diff-files: such targets are unsafe.
for flag in assume-unchanged skip-worktree; do
  fresh; gr update-index "--$flag" mod.py; printf 'local\n' >> "$r/mod.py"
  out="$($I status --src "$r" --patches "$sp/mod")"
  ck "$flag edited target reported unsafe"          "grep -q '^  unsafe' <<< \"\$out\""
  $I check --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
  ck "$flag edited target fails check (rc=$rc)"     "[[ $rc -ne 0 ]]"
  $I apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
  ck "$flag edited target not patched (rc=$rc)"     "[[ $rc -ne 0 ]] && grep -q '^1$' '$r/mod.py' && grep -q '^local$' '$r/mod.py'"
  fresh; gr update-index "--$flag" old.py; printf 'local\n' >> "$r/old.py"
  $I apply --src "$r" --patches "$sp/ren" >/dev/null 2>&1; rc=$?
  ck "$flag edited rename source refused (rc=$rc)"  "[[ $rc -ne 0 && ! -e '$r/new.py' ]] && grep -q '^local$' '$r/old.py'"
done

# A patch that checks cleanly forward AND in reverse (hunk inside repeated lines) is
# ambiguous, before and after it is applied: never "applied", never applied.
seed() { local k; for ((k = 0; k < $1; k++)); do echo x; done > "$r/mod.py"; gr commit -q -am "seed$1"; }
amb="$tmp/ambp"; mkdir -p "$amb"; fresh; seed 20; seed 19
gr format-patch -q -1 --no-signature --stdout HEAD > "$amb/0001-amb.patch"; gr reset -q --hard HEAD~1
out="$($I status --src "$r" --patches "$amb")"
ck "both-direction patch reported ambiguous"       "grep -q '^  ambiguous' <<< \"\$out\""
$I check --src "$r" --patches "$amb" >/dev/null 2>&1; rc=$?
ck "check fails on an ambiguous patch (rc=$rc)"    "[[ $rc -ne 0 ]]"
$I apply --src "$r" --patches "$amb" >/dev/null 2>&1; rc=$?
ck "apply refuses an ambiguous patch (rc=$rc)"     "[[ $rc -ne 0 && \$(wc -l < '$r/mod.py') -eq 20 ]]"
seed 19; out="$($I status --src "$r" --patches "$amb" --json)"
ck "ambiguous even when it looks applied"          "python3 -c 'import json,sys; assert json.loads(sys.argv[1])[\"patches\"][0][\"state\"]==\"ambiguous\"' \"\$out\""

# Whole set preflighted before any write: a later patch's non-UTF-8 dirty name (an untracked
# directory at a creation target) used to abort --json after the first patch was written.
set2="$tmp/set2"; mkdir -p "$set2"; cp "$sp"/mod/*.patch "$set2/0001-mod.patch"; cp "$sp"/new/*.patch "$set2/0002-new.patch"
fresh; mkdir "$r/created.py"; : > "$r/created.py/$(printf 'bad\377')"
out="$($I apply --src "$r" --patches "$set2" --json 2>&1)"; rc=$?
ck "non-UTF-8 entry below a target: json apply refuses, valid JSON (rc=$rc)" "[[ $rc -eq 1 ]] && python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"patches\"][1][\"dirty_targets\"]==[\"created.py\"]' \"\$out\""
ck "non-UTF-8 entry below a target: earlier patch not written" "grep -q '^1$' '$r/mod.py' && git -C '$r' diff --quiet"
out="$($I apply --src "$r" --patches "$set2")"; rc=$?
ck "non-UTF-8 dirty name: text apply refuses the set" "[[ $rc -eq 1 ]] && grep -q 'not applied' <<< \"\$out\" && grep -q 'refused: dirty' <<< \"\$out\" && grep -q '^1$' '$r/mod.py'"
# Two patches to one file go through one git apply (chained); a set whose combined check
# fails writes nothing.
set3="$tmp/set3"; cp -r "$set2" "$set3"; rm "$set3/0002-new.patch"
mkp "$set3" 0002-mod10.patch "sed -i '10s/.*/ten/' mod.py"
fresh; out="$($I apply --src "$r" --patches "$set3")"; rc=$?
ck "two patches to one file apply as one set (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q 'applied now: 2' <<< \"\$out\" && grep -q '^mod$' '$r/mod.py' && grep -q '^ten$' '$r/mod.py'"
set4="$tmp/set4"; cp -r "$set2" "$set4"; rm "$set4/0002-new.patch"
mkp "$set4" 0002-mod1b.patch "sed -i '1s/.*/other/' mod.py"
fresh; out="$($I apply --src "$r" --patches "$set4")"; rc=$?
ck "second patch over the first's lines: conflict, nothing written (rc=$rc)" "[[ $rc -ne 0 ]] && grep -q 'refused: conflict' <<< \"\$out\" && git -C '$r' diff --quiet"
$I check --src "$r" --patches "$set4" >/dev/null 2>&1; rc=$?
ck "check agrees with apply on that set (rc=$rc)"  "[[ $rc -ne 0 ]]"

# Dirty-target order is byte order whatever the caller's locale.
setl="$tmp/setl"; mkp "$setl" 0001-two.patch "sed -i 1s/.*/x/ mod.py unrelated.py; seq 1 10 > Z.py; git add Z.py"
fresh; printf 'l\n' >> "$r/mod.py"; printf 'l\n' >> "$r/Z.py"
oc="$(run LC_ALL=C -- status --src "$r" --patches "$setl" --json)"; ou="$(run LC_ALL=en_US.UTF-8 -- status --src "$r" --patches "$setl" --json)"
ck "dirty order independent of locale"             "[[ \"\$oc\" == \"\$ou\" ]] && python3 -c 'import json,sys; assert json.loads(sys.argv[1])[\"patches\"][0][\"dirty_targets\"]==[\"Z.py\",\"mod.py\"]' \"\$oc\""

# Split index: every index read refreshes .git/sharedindex.* mtime, so status/check refuse
# to inspect (nothing written); apply on an isolated split-index checkout still works.
fresh; gr update-index --split-index; touch -d '2 days ago' "$r"/.git/sharedindex.*
sisnap() { (cd "$r/.git" && find . -printf '%p %T@ %s\n' | sort); }
s0="$(sisnap)"
$I status --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc1=$?
$I check --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc2=$?
run HERMES_HOME="$tmp/sih" -- status --patches "$sp/mod" >/dev/null 2>&1
mkdir -p "$tmp/sih"; ln -sfn "$r" "$tmp/sih/hermes-agent"
run HERMES_HOME="$tmp/sih" -- status --patches "$sp/mod" >/dev/null 2>&1; rc3=$?
ck "split index: status/check refuse (rc=$rc1/$rc2/$rc3)" "[[ $rc1 -eq 3 && $rc2 -eq 3 && $rc3 -eq 3 ]]"
ck "split index: no .git mtime or size changed"    "[[ \"\$s0\" == \"\$(sisnap)\" ]]"
$I apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "split index: isolated apply still works (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^mod$' '$r/mod.py'"

# Third review round (run with PATCH_SCRIPT=<old copy> to see these fail).
# 1. JSON encodability is settled before any write: a non-UTF-8 --src path or patch name
#    used to be found only after git apply had written the checkout.
bad="$tmp/bad$(printf '\377')src"; git clone -q "$base" "$bad"
out="$($I apply --src "$bad" --patches "$sp/mod" --json 2>/dev/null)"; rc=$?
ck "non-UTF-8 --src: json apply exits 2 (rc=$rc)"  "[[ $rc -eq 2 ]]"
ck "non-UTF-8 --src: nothing written"              "grep -q '^1$' '$bad/mod.py' && git -C '$bad' diff --quiet"
out="$($I apply --src "$bad" --patches "$sp/mod")"; rc=$?
ck "non-UTF-8 --src: text apply works, output escaped (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^mod$' '$bad/mod.py' && grep -qF 'bad\xffsrc' <<< \"\$out\" && ! LC_ALL=C grep -q \$'\\377' <<< \"\$out\""
setn="$tmp/setn"; mkdir -p "$setn"; cp "$sp"/mod/*.patch "$setn/0001-mod.patch"; cp "$sp"/new/*.patch "$setn/0002-$(printf '\377').patch"
fresh; $I apply --src "$r" --patches "$setn" --json >/dev/null 2>&1; rc=$?
ck "non-UTF-8 patch name: json apply exits 2, nothing written (rc=$rc)" "[[ $rc -eq 2 ]] && grep -q '^1$' '$r/mod.py' && [[ ! -e '$r/created.py' ]]"

# 2. Repeated blocks: hunks are never shifted to another copy of their context.
rb="$tmp/repbase"; git init -q -b main "$rb"
{ echo top; for c in 1 2; do seq -f 'b%g' 1 9; echo "sep$c"; done; echo end; } > "$rb/rep.py"
git -C "$rb" add rep.py; git -C "$rb" -c user.email=t@example.com -c user.name=t commit -q -m base
rp="$tmp/repp"; mkdir -p "$rp/dup" "$rp/off" "$rp/uniq"
rw="$tmp/repw"; git clone -q "$rb" "$rw"
rg() { git -C "$rw" -c user.email=t@example.com -c user.name=t -c commit.gpgsign=false "$@"; }
sed -i '6s/.*/B5/' "$rw/rep.py"; rg commit -q -am first
rg format-patch -q -1 --no-signature --stdout HEAD > "$rp/dup/0001-first.patch"
cp "$rp/dup/0001-first.patch" "$rp/dup/0002-again.patch"         # identical patch, same hunk
rg reset -q --hard HEAD~1
printf 'x\ny\n' | cat - "$rw/rep.py" > "$rw/t" && mv "$rw/t" "$rw/rep.py"; rg commit -q -am shifted
sed -i '18s/.*/B5/' "$rw/rep.py"; rg commit -q -am second         # block 2, other base (+2 lines)
rg format-patch -q -1 --no-signature --stdout HEAD > "$rp/off/0001-second-offset.patch"
sed -i 's/^end$/END/' "$rw/rep.py"; rg commit -q -am uniq           # unique context, other base
rg format-patch -q -1 --no-signature --stdout HEAD > "$rp/uniq/0001-uniq-offset.patch"
ck "fixture: duplicate hunk context occurs twice"  "[[ \$(grep -c '^b5$' '$rb/rep.py') -eq 2 ]]"
rclone() { r="$tmp/rep$((n+=1))"; git clone -q "$rb" "$r"; }
rclone; out="$($I apply --src "$r" --patches "$rp/dup")"; rc=$?
ck "identical patches on a repeated block: refused (rc=$rc)" "[[ $rc -ne 0 ]] && grep -q 'ambiguous' <<< \"\$out\""
ck "identical patches: neither block written"      "git -C '$r' diff --quiet && [[ \$(grep -c '^b5$' '$r/rep.py') -eq 2 ]]"
$I check --src "$r" --patches "$rp/dup" >/dev/null 2>&1; rc=$?
ck "identical patches: check fails too (rc=$rc)"   "[[ $rc -ne 0 ]]"
rclone; out="$($I apply --src "$r" --patches "$rp/off")"; rc=$?
ck "repeated context on another base: conflict, not shifted (rc=$rc)" "[[ $rc -ne 0 ]] && grep -q 'unprovable' <<< \"\$out\" && git -C '$r' diff --quiet"
rclone; out="$($I apply --src "$r" --patches "$rp/uniq")"; rc=$?
ck "unique context on another base still applies (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^END$' '$r/rep.py' && [[ \$(git -C '$r' diff --numstat | cut -f1-2) == \$'1\\t1' ]]"

# 3. Resume: changes the set explains (an interrupted or earlier apply) are proven, not
#    "dirty"; check and apply give the same verdict.
part="$tmp/part1"; mkdir -p "$part"; cp "$set3/0001-mod.patch" "$part/"
fresh; $I apply --src "$r" --patches "$part" >/dev/null
out="$($I status --src "$r" --patches "$set3" --json)"
ck "partly applied set: first applied, second applicable" "python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert [p[\"state\"] for p in d[\"patches\"]]==[\"applied\",\"applicable\"] and d[\"patches\"][0][\"dirty_targets\"]==[], d' \"\$out\""
$I check --src "$r" --patches "$set3" >/dev/null 2>&1; rc=$?
ck "partly applied set: check passes (rc=$rc)"     "[[ $rc -eq 0 ]]"
out="$($I apply --src "$r" --patches "$set3")"; rc=$?
ck "partly applied set: apply resumes (rc=$rc)"    "[[ $rc -eq 0 ]] && grep -q 'applied now: 1' <<< \"\$out\" && grep -q '^mod$' '$r/mod.py' && grep -q '^ten$' '$r/mod.py'"
fresh; $I apply --src "$r" --patches "$part" >/dev/null; sed -i '5s/.*/local/' "$r/mod.py"
out="$($I status --src "$r" --patches "$set3")"
ck "partly applied + local edit: dirty"            "[[ \$(grep -c '^  dirty' <<< \"\$out\") -eq 2 ]]"
$I check --src "$r" --patches "$set3" >/dev/null 2>&1; rc1=$?
$I apply --src "$r" --patches "$set3" >/dev/null 2>&1; rc2=$?
ck "partly applied + local edit: check and apply both refuse (rc=$rc1/$rc2)" "[[ $rc1 -ne 0 && $rc2 -ne 0 ]] && grep -q '^local$' '$r/mod.py' && ! grep -q '^ten$' '$r/mod.py'"
fresh; printf 'l\n' >> "$r/mod.py"
$I check --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc1=$?
$I apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc2=$?
ck "unexplained local edit: check fails like apply (rc=$rc1/$rc2)" "[[ $rc1 -ne 0 && $rc2 -ne 0 ]]"
fresh; $I apply --src "$r" --patches "$setl" >/dev/null; git -C "$r" checkout -q -- unrelated.py
out="$($I status --src "$r" --patches "$setl")"
ck "patch only partly present: dirty"              "grep -q 'only partly present' <<< \"\$out\""
$I apply --src "$r" --patches "$setl" >/dev/null 2>&1; rc=$?
ck "patch only partly present: apply refuses (rc=$rc)" "[[ $rc -ne 0 ]] && grep -q '^1$' '$r/unrelated.py'"
fresh; $I apply --src "$r" --patches "$sp/new" >/dev/null
out="$($I status --src "$r" --patches "$sp/new")"
ck "uncommitted creation reported applied"         "grep -q '^  applied' <<< \"\$out\""

# A patch exported on top of the previous one (its context contains the first change, like
# 0004 over 0002 in tools/approval.py) chains on its exact base; resume works mid-chain.
chn="$tmp/chain"; w="$tmp/chainw"; git clone -q "$base" "$w"
cw() { git -C "$w" -c user.email=t@example.com -c user.name=t -c commit.gpgsign=false "$@"; }
sed -i '1s/.*/one/' "$w/mod.py"; cw commit -q -am c1; sed -i '2s/.*/two/' "$w/mod.py"; cw commit -q -am c2
mkdir -p "$chn"; cw format-patch -q -2 --no-signature -o "$chn" HEAD >/dev/null
ck "fixture: second chained patch has the first change as context" "grep -q '^ one$' \"\$(ls '$chn'/0002-*)\""
fresh; out="$($I apply --src "$r" --patches "$chn")"; rc=$?
ck "chained patches apply as a set (rc=$rc)"       "[[ $rc -eq 0 ]] && grep -q 'applied now: 2' <<< \"\$out\" && [[ \$(head -2 '$r/mod.py' | tr '\n' ' ') == 'one two ' ]]"
fresh; mkdir -p "$tmp/chain1"; cp "$chn"/0001-* "$tmp/chain1/"; $I apply --src "$r" --patches "$tmp/chain1" >/dev/null
out="$($I apply --src "$r" --patches "$chn")"; rc=$?
ck "chained set resumes after the first (rc=$rc)"  "[[ $rc -eq 0 ]] && grep -q 'applied now: 1' <<< \"\$out\" && [[ \$(head -2 '$r/mod.py' | tr '\n' ' ') == 'one two ' ]]"
gr commit -q -am "patched"
out="$($I status --src "$r" --patches "$chn")"
ck "committed patches report applied"              "[[ \$(grep -c '^  applied' <<< \"\$out\") -eq 2 ]]"
$I apply --src "$r" --patches "$chn" >/dev/null; rc=$?
ck "apply on committed patches is a no-op (rc=$rc)" "[[ $rc -eq 0 ]] && git -C '$r' diff --quiet"

# Text conversions: text=auto on LF files is fine; CRLF bytes or an ident attribute are unsafe.
fresh; echo '* text=auto' > "$r/.git/info/attributes"
$I apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "text=auto with LF targets applies (rc=$rc)"    "[[ $rc -eq 0 ]] && grep -q '^mod$' '$r/mod.py'"
fresh; echo '* text=auto' > "$r/.git/info/attributes"; sed -i 's/$/\r/' "$r/mod.py"
out="$($I status --src "$r" --patches "$sp/mod")"
ck "text attribute with CR bytes is unsafe"        "grep -q '^  unsafe' <<< \"\$out\""
fresh; echo 'mod.py ident' > "$r/.git/info/attributes"
out="$($I status --src "$r" --patches "$sp/mod")"
ck "ident attribute is unsafe"                     "grep -q '^  unsafe' <<< \"\$out\""

# Untrusted names never reach the terminal raw.
esc="$tmp/escp"; mkdir -p "$esc"; cp "$sp"/mod/*.patch "$esc/0001-$(printf '\033')[31mred.patch"
fresh; out="$($I status --src "$r" --patches "$esc" 2>&1)"
ck "escape sequence in a patch name is neutralised" "! grep -q \$'\\033' <<< \"\$out\" && grep -qF 'x1b[31mred' <<< \"\$out\""
out="$($I status --src "$tmp/no$(printf '\033')pe" 2>&1)"
ck "escape sequence in an error message neutralised" "! grep -q \$'\\033' <<< \"\$out\""
oc="$(run LC_ALL=C -- status --src "$r" --patches "$esc")"; ou="$(run LC_ALL=en_US.UTF-8 -- status --src "$r" --patches "$esc")"
ck "text output independent of locale"             "[[ \"\$oc\" == \"\$ou\" ]]"

# 4. ENV_FILE: only a literal absolute HERMES_HOME is understood; every other assignment form
#    refuses apply (it used to be taken literally, found missing and ignored).
home2="$tmp/home2"; inst2="$home2/.hermes/hermes-agent"; mkdir -p "$home2/.hermes"; git clone -q "$base" "$inst2"
E="run HOME=$prof/home HOME2=$home2 ENV_FILE=$tmp/form.env --"
for form in 'HERMES_HOME=$HOME2/.hermes' 'HERMES_HOME="${HOME2}/.hermes"' 'HERMES_HOME=~/.hermes' \
            'export HERMES_HOME=`echo $HOME2/.hermes`' 'HERMES_HOME = /x' 'declare -x HERMES_HOME=$HOME2/.hermes' \
            ': ${HERMES_HOME:=$HOME2/.hermes}' 'HERMES_HOME=relative/.hermes' 'HERMES_HOME=/x#y' "HERMES_HOME=\"$home2/.hermes\"x"; do
  printf 'A=1\n%s\n' "$form" > "$tmp/form.env"
  $E apply --src "$inst2" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
  ck "ENV_FILE form refused: $form (rc=$rc)"       "[[ $rc -eq 3 ]] && grep -q '^1$' '$inst2/mod.py'"
done
printf 'HERMES_HOME=$HOME2/.hermes\n' > "$tmp/form.env"
$E status --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "unsupported ENV_FILE: status without --src refused (rc=$rc)" "[[ $rc -eq 3 ]]"
out="$($E status --src "$inst2" --patches "$sp/mod" 2>&1)"; rc=$?
ck "unsupported ENV_FILE: status --src warns, still works (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^warning: ' <<< \"\$out\""
printf "export HERMES_HOME='%s'\n" "$home2/.hermes" > "$tmp/form.env"
out="$($E apply --src "$inst2" --patches "$sp/mod" 2>&1)"; rc=$?
ck "literal single-quoted export recognised (rc=$rc)" "[[ $rc -eq 3 ]] && ! grep -q 'will not evaluate' <<< \"\$out\" && grep -q '^1$' '$inst2/mod.py'"
# The installers read HERMES_HOME with scripts/lib.sh env_file_get, which keeps a trailing
# comment/blank (and strips quotes its own way). For each line the fixture install lives at
# the path env_file_get ACTUALLY returns (no parser copy here), symlinked to a clean clone;
# apply --src on that clone must refuse and leave it byte-identical. The old guard stripped
# the comment, protected a different path and wrote the install.
efg() { (source "$kit/scripts/lib.sh" && env_file_get HERMES_HOME "$1"); }
ef="$tmp/ef"; mkdir -p "$ef"; k=0
for line in "HERMES_HOME=$ef/ih # c" "HERMES_HOME=$ef/ih  " "HERMES_HOME=$ef/ih"$'\t' "export HERMES_HOME=$ef/ih #" \
            "HERMES_HOME=\"$ef/ih\" # c" "HERMES_HOME='$ef/ih' " "HERMES_HOME=\"$ef/ih'\"" "HERMES_HOME='$ef/ih\"'" \
            "HERMES_HOME=$ef/ih # c"$'\r'; do
  k=$((k+1)); printf 'A=1\n%s\n' "$line" > "$tmp/ef$k.env"
  eff="$(efg "$tmp/ef$k.env")"; mkdir -p "$eff"
  fresh; ln -sfn "$r" "$eff/hermes-agent"; before="$(cd "$r" && find . -path ./.git -prune -o -type f -exec sha256sum {} + | sort)"
  out="$(run HOME="$prof/home" ENV_FILE="$tmp/ef$k.env" -- apply --src "$r" --patches "$sp/mod" 2>&1)"; rc=$?
  after="$(cd "$r" && find . -path ./.git -prune -o -type f -exec sha256sum {} + | sort)"
  ck "env_file_get mismatch refused: $(printf %q "$line") (rc=$rc)" "[[ \"\$eff\" == /* && $rc -eq 3 ]] && grep -q 'will not evaluate' <<< \"\$out\" && [[ \"\$before\" == \"\$after\" ]]"
  rm -f "$eff/hermes-agent"
done
# A space inside quotes is a literal path on which both readers agree: recognised, protected.
printf 'HERMES_HOME="%s"\n' "$ef/i h" > "$tmp/efs.env"; eff="$(efg "$tmp/efs.env")"; mkdir -p "$eff"
fresh; ln -sfn "$r" "$eff/hermes-agent"
out="$(run HOME="$prof/home" ENV_FILE="$tmp/efs.env" -- apply --src "$r" --patches "$sp/mod" 2>&1)"; rc=$?
ck "quoted path with a space recognised and protected (rc=$rc)" "[[ \"\$eff\" == \"\$ef/i h\" && $rc -eq 3 ]] && ! grep -q 'will not evaluate' <<< \"\$out\" && grep -q '^1$' '$r/mod.py'"
out="$(run HOME="$prof/home" ENV_FILE="$tmp/efs.env" -- status --patches "$sp/mod" 2>/dev/null)"; rc=$?
ck "default status follows the effective ENV_FILE path (rc=$rc)" "[[ $rc -eq 0 ]] && grep -qF '$r' <<< \"\$out\""
rm -f "$eff/hermes-agent"
printf 'HERMES_HOME=%s\nHERMES_HOME=/elsewhere\n' "$home2/.hermes" > "$tmp/form.env"
$E apply --src "$inst2" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "every literal assignment counts, not just the last (rc=$rc)" "[[ $rc -eq 3 ]] && grep -q '^1$' '$inst2/mod.py'"
printf '# HERMES_HOME=$X\nFOO=${HERMES_HOME:-/x}/bar\n' > "$tmp/form.env"
fresh; $E apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "comments and HERMES_HOME reads are not assignments (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^mod$' '$r/mod.py'"

# 5. Report delivery is not part of the operation: stdout failing after the write phase exits
#    4 with a stderr note that the checkout may be changed; status shows what happened.
if [[ -w /dev/full ]]; then
  fresh; $I apply --src "$r" --patches "$sp/mod" > /dev/full 2> "$tmp/full.err"; rc=$?
  ck "apply > /dev/full: exit 4, outcome flagged unknown (rc=$rc)" "[[ $rc -eq 4 ]] && grep -q 'MAY BE CHANGED' '$tmp/full.err' && ! grep -q Traceback '$tmp/full.err'"
  out="$($I status --src "$r" --patches "$sp/mod")"
  ck "after the lost report, status shows the write happened" "grep -q '^  applied' <<< \"\$out\" && grep -q '^mod$' '$r/mod.py'"
  $I apply --src "$r" --patches "$sp/mod" --json > /dev/full 2> "$tmp/full.err"; rc=$?
  ck "no-op apply > /dev/full: exit 4, nothing written (rc=$rc)" "[[ $rc -eq 4 ]] && grep -q 'nothing was written' '$tmp/full.err'"
  $I status --src "$r" --patches "$sp/mod" > /dev/full 2>/dev/null; rc=$?
  ck "status > /dev/full: exit 4 (rc=$rc)"         "[[ $rc -eq 4 ]]"
else
  echo "  skip /dev/full delivery checks (no writable /dev/full)"
fi

# 6. Every configured location is inspected, fail closed. The fixture install sits where the
#    kit's REAL installer reader (hermes-profile.sh in the temp kit, stub hermes) actually put
#    its profile for that configuration, so the test cannot agree with a wrong copy of the rules.
printf '#!/bin/sh\nexit 0\n' > "$bin/hermes"; chmod +x "$bin/hermes"
# inst_home [VAR=value ...] -- CWD: HERMES_HOME the installer used (from its "installed" line).
inst_home() {
  local ov=() d
  while [[ "$1" != "--" ]]; do ov+=("$1"); shift; done
  d="$(cd "$2" && env -u HERMES_HOME -u ENV_FILE PATH="$bin:$PATH" HOME="$tmp/nohome" HERMES_BIN="$bin/hermes" \
        KNOWLEDGE_ROOT="$tmp/noknow" "${ov[@]}" bash "$kit/scripts/hermes-profile.sh" envt 2>/dev/null | sed -n 's/^\[agent-ops-kit\] installed \(.*\)\/profiles\/envt\/SOUL\.md$/\1/p')"
  printf '%s' "$d"
}
# guarded H [VAR=value ...] -- CWD: a fresh clone installed at H/hermes-agent; apply --src on it
# from CWD must refuse (rc 3) and leave it unchanged.
guarded() {
  local h="$1" ov=() c rc; shift
  while [[ "$1" != "--" ]]; do ov+=("$1"); shift; done; c="$2"
  fresh; mkdir -p "$h"; ln -sfn "$r" "$h/hermes-agent"
  (cd "$c" && run HOME="$prof/home" "${ov[@]}" -- apply --src "$r" --patches "$sp/mod") > "$tmp/g.out" 2>&1; rc=$?
  rm -f "$h/hermes-agent"
  [[ "$h" == /* && $rc -eq 3 ]] && grep -q '^1$' "$r/mod.py"
}
mkdir -p "$tmp/cwd" "$tmp/noknow"
# a. ENV_FILE set does not hide the kit's canonical .env (make targets without ENV_FILE use it).
printf 'HERMES_HOME=%s\n' "$tmp/canon" > "$kit/.env"; printf 'HERMES_HOME=%s\n' "$tmp/other" > "$tmp/other.env"
hc="$(inst_home -- "$tmp/cwd")"; ho="$(inst_home ENV_FILE="$tmp/other.env" -- "$tmp/cwd")"
ck "installer reads canonical .env ($hc) and ENV_FILE ($ho)" "[[ \"\$hc\" == '$tmp/canon' && \"\$ho\" == '$tmp/other' ]]"
ck "ENV_FILE set: canonical .env install still refused" "guarded '$hc' ENV_FILE='$tmp/other.env' -- '$tmp/cwd'"
ck "ENV_FILE set: ENV_FILE install refused"        "guarded '$ho' ENV_FILE='$tmp/other.env' -- '$tmp/cwd'"
printf 'HERMES_HOME=$HOME/x\n' > "$kit/.env"
fresh; out="$(run HOME="$prof/home" ENV_FILE="$tmp/other.env" -- apply --src "$r" --patches "$sp/mod" 2>&1)"; rc=$?
ck "unsupported canonical .env refused with ENV_FILE set (rc=$rc)" "[[ $rc -eq 3 ]] && grep -q 'will not evaluate' <<< \"\$out\" && grep -q '^1$' '$r/mod.py'"
rm -f "$kit/.env"
# b. Relative ENV_FILE: the installers cd to the kit root; the caller's cwd copy is checked too.
printf 'HERMES_HOME=%s\n' "$tmp/rel-kit" > "$kit/rel.env"; printf 'HERMES_HOME=%s\n' "$tmp/rel-cwd" > "$tmp/cwd/rel.env"
hk="$(inst_home ENV_FILE=rel.env -- "$tmp/cwd")"
ck "installer resolves relative ENV_FILE at the kit root ($hk)" "[[ \"\$hk\" == '$tmp/rel-kit' ]]"
ck "relative ENV_FILE: kit-root file's install refused" "guarded '$hk' ENV_FILE=rel.env -- '$tmp/cwd'"
ck "relative ENV_FILE: cwd file's install refused" "guarded '$tmp/rel-cwd' ENV_FILE=rel.env -- '$tmp/cwd'"
rm -f "$kit/rel.env"
ck "relative ENV_FILE only in cwd: still refused"  "guarded '$tmp/rel-cwd' ENV_FILE=rel.env -- '$tmp/cwd'"
rm -f "$tmp/cwd/rel.env"
# c. An explicit ENV_FILE that is missing, unreadable or holds a NUL is refused, never skipped.
for ef in "$tmp/missing.env" missing.env; do
  fresh; out="$(cd "$tmp/cwd" && run HOME="$prof/home" ENV_FILE="$ef" -- apply --src "$r" --patches "$sp/mod" 2>&1)"; rc=$?
  ck "missing ENV_FILE $ef refused (rc=$rc)"       "[[ $rc -eq 3 ]] && grep -q 'does not exist' <<< \"\$out\" && grep -q '^1$' '$r/mod.py'"
done
fresh; run HOME="$prof/home" ENV_FILE="$tmp/missing.env" -- status --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "missing ENV_FILE: default status refused (rc=$rc)" "[[ $rc -eq 3 ]]"
if [[ "$(id -u)" != 0 ]]; then
  printf 'A=1\n' > "$tmp/unread.env"; chmod 000 "$tmp/unread.env"
  fresh; run HOME="$prof/home" ENV_FILE="$tmp/unread.env" -- apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
  ck "unreadable ENV_FILE refused (rc=$rc)"        "[[ $rc -eq 3 ]] && grep -q '^1$' '$r/mod.py'"
  chmod 600 "$tmp/unread.env"
else
  echo "  skip unreadable ENV_FILE check (running as root)"
fi
for where in envfile canonical; do
  printf 'A=1\0\nB=2\n' > "$tmp/nul.env"; ov=(ENV_FILE="$tmp/nul.env")
  [[ "$where" == canonical ]] && { mv "$tmp/nul.env" "$kit/.env"; ov=(); }
  fresh; out="$(run HOME="$prof/home" "${ov[@]}" -- apply --src "$r" --patches "$sp/mod" 2>&1)"; rc=$?
  ck "NUL byte in $where config refused (rc=$rc)"  "[[ $rc -eq 3 ]] && grep -q 'NUL byte' <<< \"\$out\" && grep -q '^1$' '$r/mod.py'"
  rm -f "$kit/.env" "$tmp/nul.env"
done
fresh; printf 'A=1\n' > "$kit/.env"; $I apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1; rc=$?
ck "benign canonical .env, no ENV_FILE: apply works (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^mod$' '$r/mod.py'"
rm -f "$kit/.env"

# 7. stdout closed (>&-): exit 4 before anything runs, stderr says so, no traceback, no write.
fresh; $I apply --src "$r" --patches "$sp/mod" >&- 2> "$tmp/cl.err"; rc=$?
ck "apply >&-: exit 4, nothing written (rc=$rc)"   "[[ $rc -eq 4 ]] && grep -q 'stdout is closed' '$tmp/cl.err' && ! grep -q Traceback '$tmp/cl.err' && grep -q '^1$' '$r/mod.py'"
$I apply --src "$r" --patches "$sp/mod" >/dev/null 2>&1
$I apply --src "$r" --patches "$sp/mod" >&- 2> "$tmp/cl.err"; rc=$?
ck "no-op apply >&-: exit 4 (rc=$rc)"              "[[ $rc -eq 4 ]] && grep -q 'stdout is closed' '$tmp/cl.err' && ! grep -q Traceback '$tmp/cl.err' && grep -q '^mod$' '$r/mod.py'"
$I status --src "$r" --patches "$sp/mod" >&- 2> "$tmp/cl.err"; rc=$?
ck "status >&-: exit 4 (rc=$rc)"                   "[[ $rc -eq 4 ]] && ! grep -q Traceback '$tmp/cl.err'"

# Manifest gate: base == HEAD (full id), exact ordered patch list, SHA-256, review status.
# All review evidence below is SYNTHETIC fixture data; nothing here reviews a real patch.
gs="$tmp/gate-src"; gp="$tmp/gate-p"; mkdir -p "$gp"
gg() { git -C "$gs" -c user.email=t@example.com -c user.name=t -c commit.gpgsign=false "$@"; }
git init -q -b main "$gs"; printf '1\n' > "$gs/m.py"; printf 'x\n' > "$gs/n.py"; gg add .; gg commit -q -m base
printf '2\n' > "$gs/m.py"; gg commit -q -am one; printf 'y\n' > "$gs/n.py"; gg commit -q -am two
gg format-patch -q -2 --no-signature -o "$gp" HEAD >/dev/null; gg reset -q --hard HEAD~2
gfresh() { gg checkout -q -- .; }
M="$tmp/gate-m.json"
# gman EXPR: synthetic reviewed manifest for $gp at $gs HEAD, then a python edit of `m` on it.
gman() { mkman "$gp" "$gs" "$M" && python3 -c "import json,sys; m=json.load(open(sys.argv[1])); $1; json.dump(m,open(sys.argv[1],'w'))" "$M"; }
gapply() { gfresh; out="$($S apply --src "$gs" --patches "$gp" --manifest "$M" 2>&1)"; rc=$?; }
untouched='grep -q "^1$" "$gs/m.py" && grep -q "^x$" "$gs/n.py"'

gman "pass"; gapply
ck "gate: synthetic reviewed manifest at exact base applies (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q 'manifest: verified' <<< \"\$out\" && grep -q '^2$' '$gs/m.py' && grep -q 'review=reviewed' <<< \"\$out\""
gfresh; out="$(NOMAN=1 $S apply --src "$gs" --patches "$gp" 2>&1)"; rc=$?
ck "gate: no manifest -> apply refused, nothing written (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'no manifest' <<< \"\$out\" && grep -q 'manifest gate refused' <<< \"\$out\" && $untouched"
out="$(NOMAN=1 $S status --src "$gs" --patches "$gp" 2>&1)"; rc=$?
ck "gate: status without manifest exits 0, says NOT VERIFIED + applicable" "[[ $rc -eq 0 ]] && grep -q 'NOT VERIFIED' <<< \"\$out\" && grep -q 'applicable' <<< \"\$out\" && grep -q 'review=unverified' <<< \"\$out\""
ck "gate: check fails without manifest" "! NOMAN=1 $S check --src '$gs' --patches '$gp' >/dev/null 2>&1"
mkman "$gp" "$gs" "$gp/manifest.json"; gfresh; out="$(NOMAN=1 $S apply --src "$gs" --patches "$gp" 2>&1)"; rc=$?
ck "gate: default <patches>/manifest.json is used (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q 'manifest: verified' <<< \"\$out\""
rm -f "$gp/manifest.json"
gman "m['patches'][0]['review']={'status':'unreviewed'}"; gapply
ck "gate: unreviewed patch refused (rc=$rc)"        "[[ $rc -eq 1 ]] && grep -q 'review status unreviewed' <<< \"\$out\" && grep -q 'review=unreviewed' <<< \"\$out\" && $untouched"
ck "gate: check fails on unreviewed"               "! $S check --src '$gs' --patches '$gp' --manifest '$M' >/dev/null 2>&1"
gman "m['patches'][1]['review']={'status':'rejected','verdict':'approved','reviewer':'x','reference':'x','date':'x'}"; gapply
ck "gate: rejected patch refused (rc=$rc)"          "[[ $rc -eq 1 ]] && grep -q 'review status rejected' <<< \"\$out\" && $untouched"
gman "m['patches'][0]['review']={'status':'reviewed'}"; gapply
ck "gate: reviewed without evidence refused (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'without verdict approved' <<< \"\$out\" && $untouched"
gman "m['patches'][0]['review']['verdict']='changes requested'"; gapply
ck "gate: verdict other than approved refused (rc=$rc)" "[[ $rc -eq 1 ]] && $untouched"
gman "m['patches'][0]['review']['status']='REVIEWED'"; gapply
ck "gate: unknown review status refused (rc=$rc)"   "[[ $rc -eq 1 ]] && grep -q 'review=invalid' <<< \"\$out\" && $untouched"
gman "m['patches'][0]['sha256']='0'*64"; gapply
ck "gate: SHA-256 mismatch (tampered) refused (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'SHA-256 differs' <<< \"\$out\" && $untouched"
gman "pass"; cp "$gp/0001-one.patch" "$tmp/one.bak"; printf '\n' >> "$gp/0001-one.patch"; gapply; mv "$tmp/one.bak" "$gp/0001-one.patch"
ck "gate: patch file edited after manifest refused (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'SHA-256 differs' <<< \"\$out\" && $untouched"
gman "m['patches'].pop()"; gapply
ck "gate: extra patch not in manifest refused (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'not in the manifest' <<< \"\$out\" && grep -q 'review=unlisted' <<< \"\$out\" && $untouched"
gman "m['patches'].append(dict(m['patches'][0], file='0003-gone.patch'))"; gapply
ck "gate: listed patch missing refused (rc=$rc)"   "[[ $rc -eq 1 ]] && grep -q 'is missing: 0003-gone.patch' <<< \"\$out\" && $untouched"
gman "m['patches'].reverse()"; gapply
ck "gate: order differs refused (rc=$rc)"          "[[ $rc -eq 1 ]] && grep -q 'order differs' <<< \"\$out\" && $untouched"
gman "m['patches'].append(m['patches'][0])"; gapply
ck "gate: duplicate entry refused (rc=$rc)"        "[[ $rc -eq 1 ]] && grep -q 'twice' <<< \"\$out\" && $untouched"
gman "m['base']=m['base'][:8]"; gapply
ck "gate: short base refused (rc=$rc)"             "[[ $rc -eq 1 ]] && grep -q 'base unpinned' <<< \"\$out\" && $untouched"
gman "m['base']=None"; gapply
ck "gate: null base refused (rc=$rc)"              "[[ $rc -eq 1 ]] && grep -q 'base unpinned' <<< \"\$out\" && $untouched"
gman "m['base']='f'*40"; gapply
ck "gate: base != HEAD refused (rc=$rc)"           "[[ $rc -eq 1 ]] && grep -q 'is not the manifest base' <<< \"\$out\" && $untouched"
gman "m['schema']='hermes-runtime-patches/v0'"; gapply
ck "gate: unknown schema refused (rc=$rc)"         "[[ $rc -eq 1 ]] && grep -q 'schema' <<< \"\$out\" && $untouched"
printf 'not json' > "$M"; gapply
ck "gate: invalid JSON refused (rc=$rc)"           "[[ $rc -eq 1 ]] && grep -q 'manifest is not valid JSON (line 1 column 1' <<< \"\$out\" && $untouched"
printf '{"schema": "\xff"}' > "$M"; gapply
ck "gate: non-UTF-8 manifest refused as such (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'manifest is not valid UTF-8' <<< \"\$out\" && $untouched"
gman "m['base']='f'*40"; out="$($S status --src "$gs" --patches "$gp" --manifest "$M" --json)"; rc=$?
ck "gate: --json reports manifest verified=false + review" "[[ $rc -eq 0 ]] && python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"manifest\"][\"verified\"] is False and d[\"manifest\"][\"errors\"] and d[\"patches\"][0][\"review\"]==\"reviewed\"' \"\$out\""
gman "m['base']='f'*40"; gfresh; printf '1\nlocal\n' > "$gs/m.py"; out="$($S apply --src "$gs" --patches "$gp" --manifest "$M" 2>&1)"; rc=$?
ck "gate: simulation refusals still reported with gate refusal (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'refused: dirty' <<< \"\$out\" && grep -q 'NOT VERIFIED' <<< \"\$out\" && grep -q '^local$' '$gs/m.py'"
gfresh
# Sixth round (independent review): replace refs, HEAD race, duplicate keys, review bound to
# the patch hash, deep/unrepresentable JSON. All fixtures SYNTHETIC.
# refs/replace: rev-parse pins HEAD's commit, but a replacement object would make ls-tree and
# cat-file read another tree. Every git read must ignore replace refs.
gman "pass"; gfresh
hc="$(gg rev-parse HEAD)"
ev="$(printf 'evil\n' | gg hash-object -w --stdin)"
et="$(printf '100644 blob %s\tm.py\n100644 blob %s\tn.py\n' "$ev" "$(gg rev-parse HEAD:n.py)" | gg mktree)"
ec="$(gg commit-tree -m evil "$et")"; gg replace "$hc" "$ec"
ck "fixture: replace ref is active for plain git" "[[ \$(git -C '$gs' show HEAD:m.py) == evil ]]"
out="$($S status --src "$gs" --patches "$gp" --manifest "$M" 2>&1)"; rc=$?
ck "replace ref: status reads the real HEAD tree (rc=$rc)" "[[ $rc -eq 0 ]] && [[ \$(grep -c '^  applicable' <<< \"\$out\") -eq 2 ]] && grep -q 'manifest: verified' <<< \"\$out\""
gapply
ck "replace ref: apply uses the real tree (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^2$' '$gs/m.py'"
gg replace -d "$hc" >/dev/null; gfresh
# HEAD race: a fixture git wrapper moves the branch the first time a given argument appears.
# HEAD-dependent reads are bound to the captured full id and HEAD is rechecked, so a move is
# reported as changed (outcome unknown), never classified or written against another commit.
rb="$tmp/racebin"; mkdir -p "$rb"; realgit="$(command -v git)"
printf '%s\n' '#!/bin/sh' \
  'case " $* " in *" $RACE_ON "*) [ -e "$RACE_FLAG" ] || { : > "$RACE_FLAG"; "$REAL_GIT" -C "$RACE_REPO" update-ref refs/heads/main "$RACE_TO"; } ;; esac' \
  'exec "$REAL_GIT" "$@"' > "$rb/git"
chmod +x "$rb/git"
gg commit -q --allow-empty -m moved; moved="$(gg rev-parse HEAD)"; gg reset -q --hard HEAD~1; gman "pass"
race() { rm -f "$tmp/race.flag"; out="$(run PATH="$rb:$bin:$PATH" REAL_GIT="$realgit" RACE_ON="$1" RACE_FLAG="$tmp/race.flag" RACE_REPO="$gs" RACE_TO="$moved" -- "${@:2}" 2>&1)"; rc=$?; }
race ls-tree status --src "$gs" --patches "$gp" --manifest "$M"
ck "HEAD race during reads: status reports HEAD changed (rc=$rc)" "[[ $rc -eq 2 ]] && grep -q 'HEAD changed' <<< \"\$out\" && [[ -e '$tmp/race.flag' ]]"
gg reset -q --hard "$hc"; gfresh
race --whitespace=nowarn apply --src "$gs" --patches "$gp" --manifest "$M"
ck "HEAD race before write: apply refuses, nothing written (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'HEAD changed' <<< \"\$out\" && $untouched"
gg reset -q --hard "$hc"; gfresh
# Duplicate JSON keys: a last-wins parse lets a hidden "reviewed" override a visible "unreviewed".
gman "pass"
python3 -c "import json,sys; s=json.dumps(json.load(open(sys.argv[1]))); open(sys.argv[1],'w').write(s.replace('\"status\": \"reviewed\"', '\"status\": \"unreviewed\", \"status\": \"reviewed\"', 1))" "$M"
gapply
ck "duplicate JSON key refused (rc=$rc)"          "[[ $rc -eq 1 ]] && grep -q 'duplicate key' <<< \"\$out\" && $untouched"
# Review evidence bound to the patch hash (self-asserted equality, not a signature).
gman "del m['patches'][0]['review']['sha256']"; gapply
ck "review without sha256 refused (rc=$rc)"       "[[ $rc -eq 1 ]] && grep -q 'review sha256' <<< \"\$out\" && grep -q 'review=invalid' <<< \"\$out\" && $untouched"
gman "m['patches'][0]['review']['sha256']='0'*64"; gapply
ck "review sha256 != patch hash refused (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'review sha256' <<< \"\$out\" && $untouched"
gman "m['patches'][0]['review']={'status':'unreviewed'}"; gapply
ck "unreviewed status semantics preserved (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'review=unreviewed' <<< \"\$out\" && $untouched"
# Deep nesting, lone surrogates and NaN: graceful refusal, no traceback; status still exits 0.
python3 -c "import sys; open(sys.argv[1],'w').write('{\"schema\":\"hermes-runtime-patches/v1\",\"x\":' + '['*200000 + ']'*200000 + '}')" "$M"; gapply
ck "deeply nested manifest refused gracefully (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'manifest' <<< \"\$out\" && ! grep -q Traceback <<< \"\$out\" && $untouched"
out="$($S status --src "$gs" --patches "$gp" --manifest "$M" 2>&1)"; rc=$?
ck "deeply nested manifest: status exits 0, NOT VERIFIED (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q 'NOT VERIFIED' <<< \"\$out\" && ! grep -q Traceback <<< \"\$out\""
gman "m['patches'].append(dict(m['patches'][0], file='\\ud800.patch'))"; gapply
ck "lone surrogate in manifest refused gracefully (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'manifest' <<< \"\$out\" && ! grep -q Traceback <<< \"\$out\" && $untouched"
out="$($S status --src "$gs" --patches "$gp" --manifest "$M" --json 2>&1)"; rc=$?
ck "lone surrogate: --json status is valid JSON (rc=$rc)" "[[ $rc -eq 0 ]] && python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"manifest\"][\"verified\"] is False' \"\$out\""
gman "pass"; sed -i 's/"patches": \[/"nan": NaN, "patches": [/' "$M"; gapply
ck "NaN constant in manifest refused (rc=$rc)"    "[[ $rc -eq 1 ]] && grep -q 'manifest' <<< \"\$out\" && $untouched"
# A float that overflows (1e99999 -> inf) is not a finite JSON number: refused even when unused.
gman "pass"; sed -i 's/"patches": \[/"big": 1e99999, "patches": [/' "$M"; gapply
ck "overflowing float (unused key) refused (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q 'non-finite number' <<< \"\$out\" && $untouched"
gman "pass"; sed -i 's/"patches": \[/"big": -1e99999, "patches": [/' "$M"; gapply
ck "overflowing negative float refused (rc=$rc)"  "[[ $rc -eq 1 ]] && grep -q 'non-finite number' <<< \"\$out\" && $untouched"
gman "pass"; sed -i 's/"patches": \[/"small": 1.5e3, "patches": [/' "$M"; gapply
ck "finite float elsewhere still accepted (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^2$' '$gs/m.py'"
gfresh
# Write outcome. A wrapper fails or moves HEAD only around the REAL `git apply` (cwd == the
# checkout; the scratch replay runs elsewhere), after it returns. Once the real write began the
# outcome is unknown: no "applicable"/"applied now: 0" claim; a refusal before it is not_attempted.
wb="$tmp/writebin"; mkdir -p "$wb"
printf '%s\n' '#!/bin/sh' \
  'case " $* " in *" rev-parse "*) [ -e "$WRITE_FLAG.nohead" ] && exit 128 ;; esac' \
  'case " $* " in *" --whitespace=nowarn "*) if [ "$(pwd -P)" = "$WRITE_REPO" ] && [ ! -e "$WRITE_FLAG" ]; then' \
  '  : > "$WRITE_FLAG"' \
  '  if [ "$WRITE_MODE" = fail ]; then echo "fixture: real apply failed" >&2; exit 1; fi' \
  '  if [ "$WRITE_MODE" = nohead ]; then "$REAL_GIT" "$@"; rc=$?; : > "$WRITE_FLAG.nohead"; exit $rc; fi' \
  '  "$REAL_GIT" "$@"; rc=$?; "$REAL_GIT" -C "$WRITE_REPO" update-ref refs/heads/main "$WRITE_TO"; exit $rc' \
  'fi ;; esac' \
  'exec "$REAL_GIT" "$@"' > "$wb/git"
chmod +x "$wb/git"
gman "pass"
wrun() { gfresh; rm -f "$tmp/write.flag" "$tmp/write.flag.nohead"; out="$(run PATH="$wb:$bin:$PATH" REAL_GIT="$realgit" WRITE_MODE="$1" WRITE_FLAG="$tmp/write.flag" WRITE_REPO="$gs" WRITE_TO="$moved" -- apply --src "$gs" --patches "$gp" --manifest "$M" ${2:-} 2>/dev/null)"; rc=$?; }
wrun move
ck "after-write HEAD move: real write ran, bytes changed (rc=$rc)" "[[ $rc -eq 1 && -e '$tmp/write.flag' ]] && grep -q '^2$' '$gs/m.py' && grep -q '^y$' '$gs/n.py' && [[ \$(gg rev-parse HEAD) == '$moved' ]]"
ck "after-write HEAD move: text says unknown, no applicable/applied-now-0" "[[ \$(grep -c '^  unknown	' <<< \"\$out\") -eq 2 ]] && grep -q '^outcome: unknown' <<< \"\$out\" && grep -q '^applied now: unknown' <<< \"\$out\" && ! grep -q 'applied now: 0' <<< \"\$out\" && ! grep -q '^  applicable' <<< \"\$out\" && grep -q 'HEAD changed during the write phase' <<< \"\$out\""
gg reset -q --hard "$hc"; wrun move --json
ck "after-write HEAD move: json outcome unknown, applied_now null" "[[ $rc -eq 1 ]] && grep -q '^2$' '$gs/m.py' && python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"outcome\"]==\"unknown\" and d[\"write_attempted\"] is True and d[\"applied_now\"] is None and d[\"exit\"]==1, d; assert [p[\"state\"] for p in d[\"patches\"]]==[\"unknown\"]*2, d; assert all(p[\"action\"].startswith(\"unknown: \") for p in d[\"patches\"]), d' \"\$out\""
gg reset -q --hard "$hc"; wrun fail
ck "real write failure: outcome unknown, nothing claimed (rc=$rc)" "[[ $rc -eq 1 && -e '$tmp/write.flag' ]] && $untouched && grep -q '^outcome: unknown' <<< \"\$out\" && grep -q '^applied now: unknown' <<< \"\$out\" && [[ \$(grep -c '^  unknown	' <<< \"\$out\") -eq 2 ]] && grep -q 'git apply rejected the set' <<< \"\$out\""
wrun fail --json
ck "real write failure: json write_attempted, applied_now null" "[[ $rc -eq 1 ]] && python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"outcome\"]==\"unknown\" and d[\"write_attempted\"] is True and d[\"applied_now\"] is None, d' \"\$out\""
# HEAD unreadable after the write (fixture rev-parse fails once the real apply ran): reported
# as unreadable, not as moved; outcome stays unknown.
wrun nohead
ck "after-write HEAD unreadable: says unreadable, not changed (rc=$rc)" "[[ $rc -eq 1 && -e '$tmp/write.flag.nohead' ]] && grep -q '^2$' '$gs/m.py' && grep -q 'HEAD could not be read after the write' <<< \"\$out\" && ! grep -q 'HEAD changed' <<< \"\$out\" && grep -q '^outcome: unknown' <<< \"\$out\" && grep -q '^applied now: unknown' <<< \"\$out\""
wrun nohead --json
ck "after-write HEAD unreadable: json unknown, applied_now null" "[[ $rc -eq 1 ]] && python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"outcome\"]==\"unknown\" and d[\"write_attempted\"] is True and d[\"applied_now\"] is None, d; assert all(\"HEAD could not be read after the write\" in p[\"action\"] for p in d[\"patches\"]), d' \"\$out\""
gfresh
# Undelivered report: the stderr note follows whether the real write began, not whether
# patches were pending. A pre-write refusal says nothing was written; an after-write failure
# says the checkout may be changed (and the missing report itself means unknown to the caller).
if [[ -w /dev/full ]]; then
  gfresh; rm -f "$tmp/race.flag"; run PATH="$rb:$bin:$PATH" REAL_GIT="$realgit" RACE_ON=--whitespace=nowarn RACE_FLAG="$tmp/race.flag" RACE_REPO="$gs" RACE_TO="$moved" -- apply --src "$gs" --patches "$gp" --manifest "$M" > /dev/full 2> "$tmp/full.err"; rc=$?
  ck "pre-write refusal > /dev/full: exit 4, nothing written (rc=$rc)" "[[ $rc -eq 4 && -e '$tmp/race.flag' ]] && $untouched && grep -q 'nothing was written' '$tmp/full.err' && ! grep -q 'MAY BE CHANGED' '$tmp/full.err' && ! grep -q Traceback '$tmp/full.err'"
  gg reset -q --hard "$hc"; gfresh; rm -f "$tmp/write.flag" "$tmp/write.flag.nohead"
  run PATH="$wb:$bin:$PATH" REAL_GIT="$realgit" WRITE_MODE=move WRITE_FLAG="$tmp/write.flag" WRITE_REPO="$gs" WRITE_TO="$moved" -- apply --src "$gs" --patches "$gp" --manifest "$M" --json > /dev/full 2> "$tmp/full.err"; rc=$?
  ck "after-write failure > /dev/full: exit 4, MAY BE CHANGED (rc=$rc)" "[[ $rc -eq 4 && -e '$tmp/write.flag' ]] && grep -q '^2$' '$gs/m.py' && grep -q 'real write began' '$tmp/full.err' && grep -q 'MAY BE CHANGED' '$tmp/full.err' && ! grep -q 'nothing was written' '$tmp/full.err'"
  gg reset -q --hard "$hc"
else
  echo "  skip /dev/full write-phase delivery checks (no writable /dev/full)"
fi
gfresh; rm -f "$tmp/race.flag"; out="$(run PATH="$rb:$bin:$PATH" REAL_GIT="$realgit" RACE_ON=--whitespace=nowarn RACE_FLAG="$tmp/race.flag" RACE_REPO="$gs" RACE_TO="$moved" -- apply --src "$gs" --patches "$gp" --manifest "$M" --json 2>/dev/null)"; rc=$?
ck "pre-write HEAD move: not_attempted, states kept, 0 applied (rc=$rc)" "[[ $rc -eq 1 ]] && $untouched && python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"outcome\"]==\"not_attempted\" and d[\"write_attempted\"] is False and d[\"applied_now\"]==0, d; assert [p[\"state\"] for p in d[\"patches\"]]==[\"applicable\"]*2 and \"nothing written\" in d[\"patches\"][0][\"action\"], d' \"\$out\""
gg reset -q --hard "$hc"; gman "m['base']='f'*40"; gfresh; out="$($S apply --src "$gs" --patches "$gp" --manifest "$M" 2>&1)"; rc=$?
ck "gate refusal: outcome not_attempted in text (rc=$rc)" "[[ $rc -eq 1 ]] && grep -q '^outcome: not_attempted' <<< \"\$out\" && grep -q '^applied now: 0' <<< \"\$out\" && $untouched"
gman "pass"; gfresh; out="$($S apply --src "$gs" --patches "$gp" --manifest "$M" --json 2>&1)"; rc=$?
ck "verified write: json outcome verified, applied_now 2 (rc=$rc)" "[[ $rc -eq 0 ]] && python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d[\"outcome\"]==\"verified\" and d[\"write_attempted\"] is True and d[\"applied_now\"]==2 and [p[\"state\"] for p in d[\"patches\"]]==[\"applied\"]*2, d' \"\$out\""
out="$($S apply --src "$gs" --patches "$gp" --manifest "$M" 2>&1)"; rc=$?
ck "no-op apply: outcome not_attempted, applied now 0 (rc=$rc)" "[[ $rc -eq 0 ]] && grep -q '^outcome: not_attempted' <<< \"\$out\" && grep -q '^applied now: 0' <<< \"\$out\""
gfresh
# The kit's own manifest: consistent with the patch files (no extra/missing/tampered entry),
# and nothing in it is marked reviewed without the full evidence fields.
km="hermes/runtime-patches/manifest.json"
ck "kit manifest exists"                           "[[ -f '$km' ]]"
ck "kit manifest matches the patch files and hashes" "python3 - '$km' <<'PYK'
import hashlib, json, os, sys
m = json.load(open(sys.argv[1])); d = os.path.dirname(sys.argv[1])
assert m['schema'] == 'hermes-runtime-patches/v1'
assert [e['file'] for e in m['patches']] == sorted(n for n in os.listdir(d) if n.endswith('.patch'))
for e in m['patches']:
    assert e['sha256'] == hashlib.sha256(open(os.path.join(d, e['file']), 'rb').read()).hexdigest(), e['file']
    r = e['review']
    assert r['status'] in ('unreviewed', 'reviewed', 'rejected')
    if r['status'] == 'reviewed':
        assert r.get('verdict') == 'approved' and all(str(r.get(k, '')).strip() for k in ('reviewer', 'reference', 'date'))
        assert r.get('sha256') == e['sha256'], e['file']
PYK"

# The kit's real patches are well-formed git patches naming only runtime files.
for p in hermes/runtime-patches/*.patch; do
  ck "$(basename "$p") is a git patch"             "grep -q '^diff --git a/' '$p'"
  ck "$(basename "$p") touches no .env or secrets" "! grep -E '^\+\+\+ b/.*(\.env|secret|credential)' '$p' >/dev/null"
  one="$tmp/one/$(basename "$p" .patch)"; mkdir -p "$one"; cp "$p" "$one/"
  ck "$(basename "$p") targets enumerate (not unsafe)" "! $S status --src '$src' --patches '$one' | grep -q unsafe"
done

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
