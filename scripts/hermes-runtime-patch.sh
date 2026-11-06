#!/usr/bin/env bash
# hermes-runtime-patch.sh — track the kit's patches to the installed Hermes runtime
# (hermes/runtime-patches/*.patch) so a Hermes reinstall or upgrade can re-apply them.
#
#   scripts/hermes-runtime-patch.sh status            # per patch: applied | applicable | conflict | ambiguous | blocked | dirty | unsafe
#   scripts/hermes-runtime-patch.sh check             # exit 0 iff every patch is applied or applicable (exactly when apply would not refuse)
#   scripts/hermes-runtime-patch.sh apply --src DIR   # apply the applicable ones to an ISOLATED checkout
#
# Options: --src DIR (any path inside the checkout; every operation runs at its canonical git
#          toplevel. status/check default to the installed runtime; apply requires it),
#          --patches DIR (default hermes/runtime-patches), --manifest FILE (default
#          <patches>/manifest.json), --json.
#
# Manifest gate (hermes/runtime-patches/README.md): apply refuses the set and check fails unless
# the v1 manifest pins base == the checkout's full HEAD id, lists exactly the patch files in
# order with matching SHA-256, and marks each "reviewed" with verdict/reviewer/reference/date.
# status reports "manifest: verified | NOT VERIFIED" and review=<status> per patch, exit 0.
#
# Source separation (docs/hermes-source-management.md): status/check may inspect the installed
# runtime and are written to change nothing in it: GIT_OPTIONAL_LOCKS=0, fsmonitor and the
# untracked cache off, no `git status`, and a split-index checkout is refused (any index read
# rewrites the mtime of .git/sharedindex.*). Only the patch's own target paths are read, and a
# patch is "unsafe" (never checked or applied) when its targets cannot all be enumerated, its
# hunks cannot be parsed exactly (binary, copy, symlink/submodule modes, overlapping hunks), one
# carries a filter/ident/working-tree-encoding/eol=crlf attribute (or a text attribute with CR
# bytes), is a submodule, or is assume-unchanged/skip-worktree, so no repo-configured
# clean/smudge or process filter runs and git's read/write conversions are the identity.
# apply never targets the installed runtime:
# it needs an explicit --src and refuses when the user's real home cannot be resolved
# (getent passwd), when the checkout's path, dev/inode, git dir or common dir matches or
# nests with any installed location (passwd home, HERMES_REAL_HOME, HOME, HERMES_HOME and its
# profile root, HERMES_HOME in the kit's .env and in ENV_FILE), when either sets HERMES_HOME in
# any form other than a literal absolute path (no shell is evaluated), when an explicit ENV_FILE
# is missing or unreadable or any of them holds a NUL byte, or when anything is mounted below the
# checkout. There is no opt-out. These are pathname checks by a same-UID process: they stop
# honest mistakes, not a hostile process or root, and they race with concurrent path changes.
# Every git read ignores replace refs (GIT_NO_REPLACE_OBJECTS=1 and --no-replace-objects, in
# this shell and in the simulator's subprocesses): a refs/replace/<HEAD> object would otherwise
# let ls-tree/cat-file read another tree than the commit id the gate pinned. HEAD is captured
# once as a full id; every tree/blob read uses that id, HEAD is rechecked after the reads and
# before and after the write, and a move is reported (exit 2, or for apply: refused before the
# write, or state "unknown" with outcome unknown once the real write began).
# A snapshot cannot exclude a hostile same-UID process racing the index, worktree or refs.
#
# Classification is a deterministic simulation, not `git apply --check`: the patch set is
# replayed in file order on the HEAD blobs of its targets. A hunk lands only at its stated line
# when the file is the patch's exact base (blob id from the `index` line), otherwise only where
# its preimage occurs exactly once in the current state AND at most once in HEAD; a repeated
# block, an overlapping or duplicate hunk, or a "this change is already there" claim over lines
# an earlier patch in the set wrote is refused (conflict/ambiguous), never shifted. The worktree
# of each target must then equal exactly one state of that chain: changes the set explains
# (an interrupted or earlier apply, uncommitted) are "applied" and the rest of the chain is
# "applicable"; anything else (a local edit, a staged change, a partly present patch) is
# "dirty" and blocks the set. Patches after a refused patch on the same file are "blocked".
#
# apply is all-or-nothing up to the write: any state other than applied/applicable refuses
# the set; before any write the report's paths and names are checked to be JSON-encodable
# (--json; the report itself is rendered after the write phase); the remaining
# patches are run through `git apply` in a scratch copy of the targets and must produce
# exactly the simulated bytes; the targets are re-read and must be unchanged; then ONE
# `git apply` (content conversions forced off) writes the checkout, and the written targets
# are compared with the simulation. A write-phase error or a concurrent writer is detected
# and reported, never rolled back: this is not a transaction. The report is written to stdout
# only after the write phase: when stdout fails apply exits 4 (best-effort stderr note; a stdout
# closed at startup exits 4 before anything runs), and
# any nonzero exit or missing/partial report means the outcome is unknown to the caller; run
# status before retrying. The report states the outcome: not_attempted (refused or no-op,
# nothing written), verified, or unknown (the real write began and then failed or was not
# verified: those patches are state "unknown", applied now is "unknown" / --json applied_now
# null, write_attempted true). Nothing is committed, reset, stashed or restarted. Patch order is
# the file order.
set -euo pipefail
# The report goes to stdout. If fd 1 is closed, stop before anything runs: the report could
# not be delivered, and a file this process or a child opens later could be given fd 1 and
# receive output meant for it. Exit 4 as for any undelivered report; nothing was read or written.
{ exec 9>&1; } 2>/dev/null || { echo "report not delivered (stdout is closed); nothing was run or written" >&2 || true; exit 4; }
exec 9>&-
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Caller git environment (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, ...) would redirect every
# call away from --src; drop it, then forbid optional locks.
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep '^GIT_' || true)
# Replace refs off for every git call, including the simulator's (it inherits this environment).
export GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1
# Every git invocation: no optional index writes, no fsmonitor hook/daemon from repo config.
gitq() { GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1 git --no-replace-objects -c core.fsmonitor=false -c core.untrackedCache=false "$@"; }
# Terminal-safe text: every byte outside printable ASCII becomes \xNN (paths, names and
# reasons are untrusted; an escape sequence or a newline in them must not reach the terminal).
tclean() {
  local LC_ALL=C s="$1" out="" c i
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    if [[ "$c" == [\ -~] ]]; then out+="$c"; else printf -v c '\\x%02x' "'$c"; out+="$c"; fi
  done
  printf '%s' "$out"
}
die2() { echo "$(tclean "$*")" >&2; exit 2; }

SRC=""
SRC_GIVEN=0
PATCHES="$ROOT/hermes/runtime-patches"
JSON=0
MANIFEST=""
CMD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    status|check|apply) CMD="$1" ;;
    --src) SRC="${2:?--src needs a directory}"; SRC_GIVEN=1; shift ;;
    --patches) PATCHES="${2:?--patches needs a directory}"; shift ;;
    --manifest) MANIFEST="${2:?--manifest needs a file}"; shift ;;
    --json) JSON=1 ;;
    -h|--help) sed -n '2,66p' "$0"; exit 0 ;;
    *) die2 "unknown argument: $1" ;;
  esac
  shift
done
[[ -n "$CMD" ]] || { echo "usage: $0 status|check|apply [--src DIR] [--patches DIR] [--manifest FILE] [--json]" >&2; exit 2; }
[[ -n "$MANIFEST" ]] || MANIFEST="$PATCHES/manifest.json"
command -v python3 >/dev/null || die2 "python3 not found (needed to parse and simulate patches)"
refuse() { echo "refused: $(tclean "$*")" >&2; exit 3; }

# Installed-runtime candidates, most trusted first. A profile session runs with
# HERMES_HOME=<root>/profiles/<name> and HOME=$HERMES_HOME/home, so neither alone finds it.
PW_HOME=""
if pw="$(getent passwd "$(id -u)" 2>/dev/null)"; then PW_HOME="$(cut -d: -f6 <<< "$pw")"; fi
[[ "$PW_HOME" == /* && -d "$PW_HOME" ]] || PW_HOME=""
INSTALLED=()
add_inst() { [[ -n "$1" ]] && INSTALLED+=("$1"); return 0; }
add_home() { [[ -n "$1" ]] && add_inst "$1/.hermes/hermes-agent"; return 0; }
add_hermes_home() {
  [[ -n "$1" ]] || return 0
  add_inst "$1/hermes-agent"
  case "$1" in */profiles/*) add_inst "${1%/profiles/*}/hermes-agent" ;; esac
}
add_hermes_home "${HERMES_HOME:-}"
add_home "${HERMES_REAL_HOME:-}"
add_home "$PW_HOME"
add_home "${HOME:-}"
# HERMES_HOME in ENV_FILE: only a literal absolute path is understood (unquoted, '...' or "..."
# without $ ` \ or the other quote, optional `export `), ending the line. Every value is used, not just the last.
# The grammar is the subset on which this parser and the installers' reader (scripts/lib.sh
# env_file_get, which keeps a trailing comment or blank as part of the value) yield the same
# path, so a trailing comment or whitespace is refused rather than guessed. Anything else that
# assigns HERMES_HOME ($VAR, ${...}, ~, `cmd`, relative, spaces around =, declare/readonly,
# ${HERMES_HOME:=...}) is never evaluated: ENV_UNSUPPORTED.
ENV_UNSUPPORTED=""
# Which files: the installers (hermes-install.sh, hermes-profile.sh) cd to the kit root and read
# ${ENV_FILE:-.env}, so a relative ENV_FILE means $ROOT/$ENV_FILE to them. Every location that
# could be the configuration is inspected, not just the effective one: the canonical
# $ROOT/.env whenever it exists (ENV_FILE does not hide it), an absolute ENV_FILE, and a
# relative ENV_FILE both under the kit root and under the caller's cwd. An explicit ENV_FILE
# that exists in none of its locations, or any of them that is not a readable regular file,
# is ENV_UNSUPPORTED (fail closed, never skipped). A NUL byte anywhere is refused too: bash
# `read` and grep (env_file_get) handle it differently, so the two readers could disagree.
scan_env() {
  local f="$1" ln=0 line val
  if [[ ! -f "$f" || ! -r "$f" ]]; then ENV_UNSUPPORTED="ENV_FILE $f is not a readable file"; return 1; fi
  if [[ "$(LC_ALL=C tr -dc '\000' < "$f" | wc -c)" != 0 ]]; then
    ENV_UNSUPPORTED="ENV_FILE $f contains a NUL byte (readers could disagree on its HERMES_HOME)"; return 1
  fi
  lit_uq='[A-Za-z0-9._/+@%:,=-]+'; lit_sq="'[^'\"]*'"; lit_dq='"[^"'"'"'$`\\]*"'
  re_ok="^[[:space:]]*(export[[:space:]]+)?HERMES_HOME=($lit_uq|$lit_sq|$lit_dq)$"
  re_assign='(^|[^A-Za-z0-9_])HERMES_HOME[[:space:]]*(\+?=|:=)'
  while IFS= read -r line || [[ -n "$line" ]]; do
    ln=$((ln + 1)); line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" =~ $re_assign ]] || continue
    if [[ "$line" =~ $re_ok ]]; then
      val="${BASH_REMATCH[2]}"
      case "$val" in \'*\') val="${val:1:${#val}-2}" ;; \"*\") val="${val:1:${#val}-2}" ;; esac
      if [[ "$val" == /* ]]; then add_hermes_home "$val"; continue; fi
    fi
    ENV_UNSUPPORTED="ENV_FILE $f line $ln sets HERMES_HOME in a form this script will not evaluate (use a literal absolute path, no trailing comment or whitespace)"
    return 1
  done < "$f"
}
env_files=()
[[ -e "$ROOT/.env" || -L "$ROOT/.env" ]] && env_files+=("$ROOT/.env")
if [[ -n "${ENV_FILE:-}" ]]; then
  if [[ "$ENV_FILE" == /* ]]; then env_cands=("$ENV_FILE"); else env_cands=("$ROOT/$ENV_FILE" "$PWD/$ENV_FILE"); fi
  found=0
  for f in "${env_cands[@]}"; do [[ -e "$f" || -L "$f" ]] && { env_files+=("$f"); found=1; }; done
  [[ "$found" == 1 ]] || ENV_UNSUPPORTED="ENV_FILE $ENV_FILE does not exist (looked in: ${env_cands[*]})"
fi
if [[ -z "$ENV_UNSUPPORTED" ]]; then
  for f in "${env_files[@]}"; do scan_env "$f" || break; done
fi
if [[ -n "$ENV_UNSUPPORTED" ]]; then
  [[ "$CMD" == "apply" ]] && refuse "$ENV_UNSUPPORTED; the installed runtime's identity is unverified, apply is disabled"
  [[ "$SRC_GIVEN" == "1" ]] || refuse "$ENV_UNSUPPORTED; the default installed runtime is unknown, pass --src"
  echo "warning: $(tclean "$ENV_UNSUPPORTED")" >&2
fi

if [[ "$SRC_GIVEN" == "0" ]]; then
  for inst in "${INSTALLED[@]}"; do [[ -d "$inst" ]] && { SRC="$inst"; break; }; done
  [[ -n "$SRC" ]] || SRC="${INSTALLED[0]:-$HOME/.hermes/hermes-agent}"
fi
[[ -d "$SRC" ]] || die2 "runtime source not found: $SRC (pass --src)"
[[ "$(gitq -C "$SRC" rev-parse --is-inside-work-tree 2>/dev/null)" == "true" ]] || die2 "not a git checkout: $SRC"
[[ -d "$PATCHES" ]] || die2 "no patch directory: $PATCHES"
# Canonical toplevel: git apply in a subdirectory silently skips paths outside it (so both
# --check directions pass and a patch looks "applied"); status pathspecs are cwd-relative.
SRC_ARG="$SRC"
SRC="$(readlink -f -- "$(gitq -C "$SRC" rev-parse --show-toplevel)")"
[[ -d "$SRC" ]] || die2 "cannot resolve git toplevel of $SRC_ARG"

# Human-readable path: shell-quoted unless plain (C locale: byte ranges, ASCII-only output).
qpath() { local LC_ALL=C; if [[ "$1" =~ ^[A-Za-z0-9._/+-]+$ ]]; then printf '%s' "$1"; else printf '%q' "$1"; fi; }
# The toplevel must be run by the same repository that --src resolved to: a core.worktree
# (or gitfile) pointing elsewhere would make every later `git -C toplevel` use another repo.
arg_gd="$(readlink -f -- "$(gitq -C "$SRC_ARG" rev-parse --absolute-git-dir 2>/dev/null)")" || arg_gd=""
top_gd="$(readlink -f -- "$(gitq -C "$SRC" rev-parse --absolute-git-dir 2>/dev/null)")" || top_gd=""
top_top="$(readlink -f -- "$(gitq -C "$SRC" rev-parse --show-toplevel 2>/dev/null)")" || top_top=""
[[ -n "$arg_gd" && "$arg_gd" == "$top_gd" && "$top_top" == "$SRC" ]] ||
  refuse "--src $SRC_ARG uses git dir ${arg_gd:-?} but its toplevel $SRC resolves to ${top_gd:-no repository} (core.worktree or gitfile mismatch)"
# Split index: every index read (ls-files, diff-files, check-attr, apply) refreshes the mtime
# of .git/sharedindex.* (git does this unconditionally, core.splitIndex=false doesn't stop it),
# so status/check could not inspect such a checkout without writing to its git dir. Refuse.
if [[ "$CMD" != "apply" ]]; then
  top_cd="$(readlink -f -- "$(gitq -C "$SRC" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")" || top_cd=""
  [[ -d "$top_gd" && -d "$top_cd" ]] || refuse "cannot resolve the git dir of $SRC"
  shopt -s nullglob; si=("$top_gd"/sharedindex.* "$top_cd"/sharedindex.*); shopt -u nullglob
  [[ ${#si[@]} -eq 0 ]] || refuse "$SRC uses a split index (${si[0]}); reading it would rewrite that file's mtime, so $CMD will not inspect it (no read-only guarantee)"
fi
devino() { stat -L -c '%d:%i' -- "$1" 2>/dev/null; }
# dev:inode of a path and each of its ancestors, one per line (fails if any stat fails).
chain() {
  local p="$1" id
  while :; do
    id="$(devino "$p")" || return 1
    printf '%s\n' "$id"
    [[ "$p" == / ]] && return 0
    p="$(dirname -- "$p")"
  done
}
# Git dir and common dir of an installed checkout from its files alone (no git command
# runs against the live runtime). Prints nothing when it has no .git.
inst_gitdirs() {
  local g="$1/.git" gd c
  if [[ -d "$g" ]]; then gd="$g"
  elif [[ -f "$g" ]]; then
    gd="$(sed -n 's/^gitdir: //p' "$g" | head -n1)"; [[ "$gd" == /* ]] || gd="$1/$gd"
  else return 0; fi
  gd="$(readlink -f -- "$gd")"; printf '%s\n' "$gd"
  if [[ -f "$gd/commondir" ]]; then
    c="$(head -n1 "$gd/commondir")"; [[ "$c" == /* ]] || c="$gd/$c"
    readlink -f -- "$c"
  fi
}
nested() { case "$1/" in "$2"/*) return 0 ;; esac; return 1; }  # $1 is $2 or below it

if [[ "$CMD" == "apply" ]]; then
  [[ "$SRC_GIVEN" == "1" ]] || refuse "apply needs an explicit isolated --src (the installed runtime is never a patch target)"
  [[ -n "$PW_HOME" ]] || refuse "cannot resolve this user's home from the passwd database (getent passwd $(id -u)); installed-runtime identity is unverified, apply is disabled"
  src_gd="$(readlink -f -- "$(gitq -C "$SRC" rev-parse --path-format=absolute --git-dir)")"
  src_cd="$(readlink -f -- "$(gitq -C "$SRC" rev-parse --path-format=absolute --git-common-dir)")"
  [[ -d "$src_gd" && -d "$src_cd" ]] || refuse "cannot resolve the git dir of $SRC"
  src_ids="$(chain "$SRC")" || refuse "cannot stat $SRC or an ancestor"
  src_top_id="${src_ids%%$'\n'*}"
  src_git_ids=""
  for d in "$src_gd" "$src_cd"; do src_git_ids+="$(chain "$d")"$'\n' || refuse "cannot stat $d or an ancestor"; done
  for inst in "${INSTALLED[@]}"; do
    [[ -e "$inst" ]] || continue
    inst_real="$(readlink -f -- "$inst")" || refuse "cannot resolve installed runtime $inst"
    inst_id="$(devino "$inst_real")" || refuse "cannot stat installed runtime $inst_real"
    inst_ids="$(chain "$inst_real")" || refuse "cannot stat installed runtime $inst_real or an ancestor"
    nested "$SRC" "$inst_real" && refuse "--src $SRC_ARG is the installed runtime $inst_real or inside it"
    nested "$inst_real" "$SRC" && refuse "--src $SRC_ARG checkout contains the installed runtime $inst_real"
    grep -qxF "$inst_id" <<< "$src_ids" && refuse "--src $SRC_ARG is the installed runtime $inst_real (same dev/inode, e.g. a bind mount) or inside it"
    grep -qxF "$src_top_id" <<< "$inst_ids" && refuse "--src $SRC_ARG checkout contains the installed runtime $inst_real (dev/inode)"
    for d in "$src_gd" "$src_cd"; do
      nested "$d" "$inst_real" && refuse "--src $SRC_ARG keeps its git metadata ($d) in the installed runtime $inst_real"
    done
    grep -qxF "$inst_id" <<< "$src_git_ids" && refuse "--src $SRC_ARG keeps its git metadata inside the installed runtime $inst_real (dev/inode)"
    while IFS= read -r igd; do
      [[ -n "$igd" ]] || continue
      igd_id="$(devino "$igd")" || refuse "cannot stat installed git dir $igd"
      for d in "$src_gd" "$src_cd"; do
        nested "$d" "$igd" && refuse "--src $SRC_ARG shares git metadata with the installed runtime ($igd; linked worktree or gitdir alias)"
      done
      grep -qxF "$igd_id" <<< "$src_git_ids" && refuse "--src $SRC_ARG shares git metadata with the installed runtime ($igd, dev/inode)"
    done < <(inst_gitdirs "$inst_real")
  done
  # A bind mount below the checkout could alias part of the installed runtime.
  [[ -r /proc/self/mountinfo ]] || refuse "cannot read /proc/self/mountinfo to rule out mounts below $SRC"
  while read -r _ _ _ _ mp _; do
    mp="$(printf '%b' "$mp")"
    [[ "$mp" == "$SRC"/* ]] && refuse "a filesystem is mounted at $mp inside --src $SRC_ARG"
  done < /proc/self/mountinfo
fi

TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
# Patch parser and simulator (python3, bytes throughout).
#   targets SRC PATCH       every path the patch reads or writes (old and new side), NUL-separated,
#                           or exit 1 with the reason: anything it cannot account for exactly
#                           (copies, binary, submodule/symlink modes, unparseable or unsafe names,
#                           malformed/overlapping hunks, or a file list or line counts that differ
#                           from git's own `git apply --numstat -z`).
#   run SRC TMPD CMD JSON HEAD N
#                           classify, render the output, and (apply) write; see the header.
read -r -d '' PY <<'PY' || true
import hashlib, json, math, os, re, stat, subprocess, sys, unicodedata

class Unsafe(Exception):
    pass

# Same protected environment as the shell's gitq: no replace refs, no optional locks.
GIT_ENV = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")

def git(src, *args, check=True, inp=None, env=None):
    r = subprocess.run(["git", "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args],
                       cwd=src, input=inp, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       env=GIT_ENV if env is None else env)
    if check and r.returncode != 0:
        raise Unsafe("git %s failed" % args[0])
    return r

ESC = {97: 7, 98: 8, 116: 9, 110: 10, 118: 11, 102: 12, 114: 13, 34: 34, 92: 92}
def unquote(s):  # git C-style quoted name at the start of s -> (bytes, rest)
    out, i = bytearray(), 1
    while i < len(s):
        c = s[i]
        if c == 34:
            return bytes(out), s[i + 1:]
        if c == 92:
            o = s[i + 1:i + 4]
            if len(o) == 3 and all(48 <= x <= 55 for x in o):
                out.append(int(o, 8) & 0xFF); i += 4; continue
            if i + 1 < len(s) and s[i + 1] in ESC:
                out.append(ESC[s[i + 1]]); i += 2; continue
            raise Unsafe("bad escape in a quoted patch path")
        out.append(c); i += 1
    raise Unsafe("unterminated quoted patch path")
def field(s, tab_ends):  # name field of a header line
    if s.startswith(b'"'):
        v, rest = unquote(s)
        if rest and not rest.startswith(b"\t"):
            raise Unsafe("trailing data after a quoted patch path")
        return v
    return s.split(b"\t", 1)[0] if tab_ends else s
def strip(v, pre):
    if not v.startswith(pre):
        raise Unsafe("patch path without the %r prefix" % pre.decode())
    return v[len(pre):]
def git_line(rest):  # "a/X b/Y" of a diff --git line (only needed without ---/+++/rename)
    if rest.startswith(b'"'):
        a, r = unquote(rest)
        if not r.startswith(b" "):
            raise Unsafe("unparseable diff --git line")
        r = r[1:]
        b = unquote(r)[0] if r.startswith(b'"') else r
        return strip(a, b"a/"), strip(b, b"b/")
    n = (len(rest) - 5) // 2
    if len(rest) == 2 * n + 5 and rest[:2] == b"a/" and rest[2 + n:5 + n] == b" b/" and rest[2:2 + n] == rest[5 + n:]:
        return rest[2:2 + n], rest[2:2 + n]
    raise Unsafe("unparseable diff --git line")

HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
INDEX = re.compile(rb"^index ([0-9a-f]{7,64})\.\.([0-9a-f]{7,64})(?: (\d+))?$")
MODES = (b"100644", b"100755")

def parse(src, path):
    blocks, cur, st = [], None, None
    lines = open(path, "rb").read().split(b"\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(b"diff --git "):
            cur = {"git": line[11:], "old": None, "new": None, "seen": False, "del": False, "add": False,
                   "oldmode": None, "newmode": None, "oldid": None, "newid": None, "hunks": []}
            blocks.append(cur); st = "header"; i += 1; continue
        if cur is None or st == "tail":
            i += 1; continue
        if st == "header" and not line.startswith(b"@@"):
            if line.startswith(b"GIT binary patch") or line.startswith(b"Binary files "):
                raise Unsafe("binary patches are not supported")
            if line.endswith(b"\r"):
                raise Unsafe("CRLF in a patch header")
            if line.startswith(b"copy from ") or line.startswith(b"copy to "):
                raise Unsafe("copy patches are not supported")
            m = INDEX.match(line)
            if line.startswith(b"index "):
                if not m:
                    raise Unsafe("unparseable index line")
                cur["oldid"], cur["newid"] = m.group(1).decode(), m.group(2).decode()
                if m.group(3):
                    cur["oldmode"] = cur["oldmode"] or m.group(3); cur["newmode"] = cur["newmode"] or m.group(3)
            for k, keys in ((b"old mode ", ("oldmode",)), (b"new mode ", ("newmode",)),
                            (b"new file mode ", ("newmode",)), (b"deleted file mode ", ("oldmode",))):
                if line.startswith(k):
                    for key in keys:
                        cur[key] = line[len(k):].strip()
            if line.startswith(b"deleted file mode "):
                cur["del"] = True
            elif line.startswith(b"new file mode "):
                cur["add"] = True
            elif line.startswith(b"rename from "):
                cur["old"] = field(line[12:], False); cur["seen"] = True
            elif line.startswith(b"rename to "):
                cur["new"] = field(line[10:], False); cur["seen"] = True
            elif line.startswith(b"--- "):
                v = field(line[4:], True); cur["seen"] = True
                cur["old"] = None if v == b"/dev/null" else strip(v, b"a/")
            elif line.startswith(b"+++ "):
                v = field(line[4:], True); cur["seen"] = True
                cur["new"] = None if v == b"/dev/null" else strip(v, b"b/")
            i += 1; continue
        # st is "header" or "hunks" and the line may start a hunk
        if not line.startswith(b"@@"):
            st = "tail"; i += 1; continue  # end of this file's hunks (e.g. the mail signature)
        st = "hunks"
        m = HUNK.match(line)
        if not m:
            raise Unsafe("unparseable hunk header")
        h = {"op": int(m.group(1)), "oc": int(m.group(2) or b"1"), "np": int(m.group(3)),
             "nc": int(m.group(4) or b"1"), "lines": []}
        ro, rn = h["oc"], h["nc"]
        i += 1
        while ro > 0 or rn > 0 or (i < len(lines) and lines[i].startswith(b"\\")):
            if i >= len(lines):
                raise Unsafe("truncated hunk")
            l = lines[i]; i += 1
            if l.startswith(b"\\"):
                if not h["lines"] or not h["lines"][-1][1].endswith(b"\n"):
                    raise Unsafe("misplaced no-newline marker")
                h["lines"][-1][1] = h["lines"][-1][1][:-1]; continue
            tag, body = (b" ", b"") if l == b"" else (l[:1], l[1:])
            if tag == b" ":
                ro -= 1; rn -= 1
            elif tag == b"-":
                ro -= 1
            elif tag == b"+":
                rn -= 1
            else:
                raise Unsafe("unexpected line in a hunk")
            if ro < 0 or rn < 0:
                raise Unsafe("hunk line counts do not match its header")
            h["lines"].append([tag, body + b"\n"])
        cur["hunks"].append(h)
    if not blocks:
        raise Unsafe("no file diffs in the patch")
    primary, targets, seen = [], set(), set()
    for b in blocks:
        if not b["seen"]:  # mode-only change or empty new/deleted file
            a, n = git_line(b["git"])
            if a != n:
                raise Unsafe("diff --git names differ without rename headers")
            b["old"] = None if b["add"] else a
            b["new"] = None if b["del"] else n
        if b["old"] is None and b["new"] is None:
            raise Unsafe("file diff without a path")
        if (b["old"] is None) != b["add"] or (b["new"] is None) != b["del"]:
            raise Unsafe("creation/deletion headers disagree with the file names")
        for k in ("oldmode", "newmode"):
            if b[k] is not None and b[k] not in MODES:
                raise Unsafe("file mode %s is not supported (submodules, symlinks)" % b[k].decode(errors="replace"))
        for v in {b["old"], b["new"]}:
            if v is None:
                continue
            try:
                t = v.decode("utf-8")
            except UnicodeDecodeError:
                raise Unsafe("non-UTF-8 patch path")
            parts = t.split("/")
            if "\0" in t or t.startswith("/") or any(x in ("", ".", "..") or x.lower() == ".git" for x in parts):
                raise Unsafe("unsafe patch path %a" % t)
            if v in seen:
                raise Unsafe("a path appears in more than one file diff of the patch")
            seen.add(v); targets.add(v)
        prev_end = 0
        for h in b["hunks"]:
            if h["op"] < prev_end:
                raise Unsafe("overlapping or unordered hunks")
            prev_end = h["op"] + h["oc"]
        if b["add"] and any(h["oc"] for h in b["hunks"]) or b["del"] and any(h["nc"] for h in b["hunks"]):
            raise Unsafe("creation/deletion hunk with content on the wrong side")
        primary.append((b["new"] if b["new"] is not None else b["old"],
                        sum(1 for h in b["hunks"] for t, _ in h["lines"] if t == b"+"),
                        sum(1 for h in b["hunks"] for t, _ in h["lines"] if t == b"-")))
    ns = git(src, "apply", "--numstat", "-z", path, check=False)
    if ns.returncode != 0:
        raise Unsafe("git apply --numstat rejected the patch")
    items, got, i = ns.stdout.split(b"\0"), [], 0
    while i < len(items) and items[i]:
        rec = items[i].split(b"\t", 2)
        if len(rec) != 3 or not rec[0].isdigit() or not rec[1].isdigit():
            raise Unsafe("unexpected git apply --numstat output")
        if rec[2]:
            name = rec[2]; i += 1
        else:  # "A\tD\t\0old\0new" form
            name = items[i + 2]; i += 3
        got.append((name, int(rec[0]), int(rec[1])))
    if got != primary:
        raise Unsafe("patch file list or line counts do not match git apply --numstat")
    return blocks, sorted(targets)

def split_lines(c):
    parts = c.split(b"\n")
    out = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out

class NoApply(Exception):
    pass

def blob_id(fmt, c):
    return hashlib.new(fmt, b"blob %d\0" % len(c) + c).hexdigest()

def reverse_block(b):
    r = dict(b)
    for x, y in (("old", "new"), ("add", "del"), ("oldmode", "newmode"), ("oldid", "newid")):
        r[x], r[y] = b[y], b[x]
    swap = {b"-": b"+", b"+": b"-", b" ": b" "}
    r["hunks"] = [{"op": h["np"], "oc": h["nc"], "np": h["op"], "nc": h["oc"],
                   "lines": [[swap[t], v] for t, v in h["lines"]]} for h in b["hunks"]]
    return r

def place(img, h, exact, head_img, lo):
    pre = [v for t, v in h["lines"] if t != b"+"]
    trail = 0
    for t, _ in reversed(h["lines"]):
        if t != b" ":
            break
        trail += 1
    begin, end, n = h["op"] <= 1, trail == 0, len(pre)
    def ok(im, p):
        return 0 <= p and p + n <= len(im) and not (begin and p != 0) and not (end and p + n != len(im)) and im[p:p + n] == pre
    want = h["np"] - 1 if h["np"] else 0
    if exact:  # the file is the patch's own base: its line numbers are authoritative
        if ok(img, want) and want >= lo:
            return want
        raise NoApply("hunk at line %d does not match its exact base" % h["op"])
    found = [p for p in range(len(img) + 1) if ok(img, p)]
    in_head = sum(1 for p in range(len(head_img) + 1) if ok(head_img, p)) if head_img is not None else 0
    if not found:
        raise NoApply("hunk at line %d: context not found" % h["op"])
    if len(found) > 1 or in_head > 1:
        raise NoApply("hunk at line %d: context occurs %d times (%d in HEAD); location unprovable"
                      % (h["op"], len(found), in_head))
    if found[0] < lo:
        raise NoApply("hunk at line %d overlaps an earlier hunk" % h["op"])
    return found[0]

def run_block(states, b, ctx, reverse):
    """Apply one file diff to states (path -> (bytes, exec) | None) in place, or raise NoApply."""
    if reverse:
        b = reverse_block(b)
    o, n = b["old"], b["new"]
    if o is None:
        if states.get(n) is not None:
            raise NoApply("file to create already exists")
        cur, ex = b"", False
    else:
        if states.get(o) is None:
            raise NoApply("file is missing")
        cur, ex = states[o]
        if ctx["filemode"] and b["oldmode"] is not None and (b["oldmode"] == b"100755") != ex:
            raise NoApply("file mode differs")
    if o is not None and n is not None and o != n and states.get(n) is not None:
        raise NoApply("rename target already exists")
    full = blob_id(ctx["fmt"], cur)
    exact = b["oldid"] is not None and full.startswith(b["oldid"])
    head = ctx["head"].get(o if o is not None else n)
    head_img = split_lines(head[0]) if head is not None else None
    img, lo = split_lines(cur), 0
    for h in b["hunks"]:
        p = place(img, h, exact, head_img, lo)
        post = [v for t, v in h["lines"] if t != b"-"]
        pre_n = sum(1 for t, _ in h["lines"] if t != b"+")
        img[p:p + pre_n] = post
        lo = p + len(post)
    out = b"".join(img)
    if n is None:
        if out:
            raise NoApply("deletion leaves content")
        states[o] = None
        return
    if b["newmode"] is not None:
        ex = b["newmode"] == b"100755"
    if o is not None and o != n:
        states[o] = None
    states[n] = (out, ex)

def run_patch(states, blocks, ctx, reverse):
    trial = dict(states)
    try:
        for b in blocks:
            run_block(trial, b, ctx, reverse)
    except NoApply as e:
        return None, str(e)
    return trial, ""

def wt_read(src, path):
    """Worktree state of a target: None (absent), (bytes, exec), or "odd" (symlink, dir, ...)."""
    parts, p = path.split(b"/"), os.fsencode(src)
    for k, part in enumerate(parts):
        p = p + b"/" + part
        try:
            s = os.lstat(p)
        except FileNotFoundError:
            return None
        except NotADirectoryError:
            return "odd"
        if k < len(parts) - 1:
            if not stat.S_ISDIR(s.st_mode):
                return "odd"
        elif not stat.S_ISREG(s.st_mode):
            return "odd"
    with open(p, "rb") as f:
        return f.read(), bool(s.st_mode & 0o100)

def same(a, b, filemode):
    if a is None or b is None or a == "odd" or b == "odd":
        return a is None and b is None
    return a[0] == b[0] and (not filemode or a[1] == b[1])

def qpath(b):
    if re.fullmatch(rb"[A-Za-z0-9._/+-]+", b):
        return b.decode()
    return "$'" + "".join(chr(c) if 32 <= c < 127 and c not in (39, 92) else
                          "\\" + chr(c) if c in (39, 92) else "\\%03o" % c for c in b) + "'"

def tclean(s):  # terminal-safe text
    if isinstance(s, bytes):
        s = s.decode("utf-8", "backslashreplace")
    return "".join(c if unicodedata.category(c) not in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") else
                   "\\x%02x" % ord(c) if ord(c) < 256 else "\\u%04x" % ord(c) for c in s)

MANIFEST_SCHEMA = "hermes-runtime-patches/v1"
REVIEW_FIELDS = ("reviewer", "reference", "date")
MANIFEST_DEPTH = 32

class BadManifest(Exception):
    pass

def _no_dup(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise BadManifest("manifest has a duplicate key %s (ambiguous; refused)" % ascii(k))
        seen.add(k)
    return dict(pairs)

def _no_const(c):
    raise BadManifest("manifest uses the non-JSON constant %s" % c)

def _finite(t):
    f = float(t)
    if not math.isfinite(f):
        raise BadManifest("manifest holds a non-finite number %s (out of range)" % ascii(t[:40]))
    return f

def load_manifest(raw):
    """Strict JSON: UTF-8, no duplicate keys, no NaN/Infinity or overflowing float, nesting <= MANIFEST_DEPTH, every
    string valid Unicode (no lone surrogates). Raises BadManifest with a printable reason."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BadManifest("manifest is not valid UTF-8")
    try:
        m = json.loads(text, object_pairs_hook=_no_dup, parse_constant=_no_const, parse_float=_finite)
    except RecursionError:
        raise BadManifest("manifest is nested too deeply")
    except json.JSONDecodeError as e:
        raise BadManifest("manifest is not valid JSON (line %d column %d: %s)" % (e.lineno, e.colno, e.msg))
    except ValueError:
        raise BadManifest("manifest is not valid JSON")
    stack = [(m, 0)]
    while stack:
        v, d = stack.pop()
        if d > MANIFEST_DEPTH:
            raise BadManifest("manifest is nested too deeply")
        items = list(v.items()) if isinstance(v, dict) else [(None, x) for x in v] if isinstance(v, list) else [(None, v)]
        for k, x in items:
            for t in (k, x):
                if isinstance(t, str):
                    try:
                        t.encode("utf-8")
                    except UnicodeEncodeError:
                        raise BadManifest("manifest holds a string that is not valid Unicode (lone surrogate)")
            if isinstance(x, (dict, list)):
                stack.append((x, d + 1))
    return m

def head_now(src):
    r = git(src, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False)
    return r.stdout.strip().decode("ascii", "replace") if r.returncode == 0 else None

def manifest_gate(tmpd, names, head_full):
    """Provenance gate, independent of the simulation. Returns (errors, review): errors are
    set-level reasons apply refuses; review[i] is the manifest's review status of patch i
    ("unlisted" when absent). Only a v1 manifest pinning the full base == HEAD, listing exactly
    the patch files in order with their SHA-256, each status "reviewed" with verdict "approved"
    and non-empty reviewer/reference/date and review.sha256 equal to the patch's SHA-256, passes.
    That equality binds the self-asserted review record to these bytes; it is not a signature.
    A hash match alone never makes a patch reviewed."""
    n = len(names)
    review = ["unverified"] * n
    try:
        raw = open(os.path.join(tmpd, "manifest.json"), "rb").read()
    except OSError:
        return ["no manifest (provenance unverified; apply refuses)"], review
    try:
        m = load_manifest(raw)
    except BadManifest as e:
        return [str(e)], review
    if not isinstance(m, dict) or m.get("schema") != MANIFEST_SCHEMA:
        return ["manifest schema is not %s" % MANIFEST_SCHEMA], review
    errs = []
    base, ents = m.get("base"), m.get("patches")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base):
        errs.append("manifest base is not a full lowercase commit id (base unpinned)")
    elif base != head_full:
        errs.append("checkout HEAD %s is not the manifest base %s" % (head_full, base))
    if not isinstance(ents, list) or not all(isinstance(e, dict) and isinstance(e.get("file"), str) for e in ents):
        return errs + ["manifest patches must be a list of objects with a file name"], review
    listed = [e["file"] for e in ents]
    have = [os.fsdecode(x) for x in names]
    if len(set(listed)) != len(listed):
        errs.append("manifest lists a patch twice")
    missing = [f for f in listed if f not in have]
    extra = [f for f in have if f not in listed]
    if missing:
        errs.append("patch listed in the manifest is missing: " + " ".join(missing))
    if extra:
        errs.append("patch not in the manifest: " + " ".join(extra))
    if not missing and not extra and listed != have:
        errs.append("patch order differs from the manifest")
    by = {e["file"]: e for e in ents}
    for i, f in enumerate(have):
        e = by.get(f)
        if e is None:
            review[i] = "unlisted"; continue
        got = hashlib.sha256(open(os.path.join(tmpd, "p/%d.patch" % i), "rb").read()).hexdigest()
        if e.get("sha256") != got:
            errs.append("%s: SHA-256 differs from the manifest (tampered or regenerated)" % f)
        r = e.get("review")
        st = r.get("status") if isinstance(r, dict) else None
        review[i] = st if st in ("unreviewed", "reviewed", "rejected") else "invalid"
        if review[i] == "reviewed" and not (r.get("verdict") == "approved" and all(
                isinstance(r.get(k), str) and r.get(k).strip() for k in REVIEW_FIELDS)):
            review[i] = "invalid"
            errs.append("%s: review marked reviewed without verdict approved and reviewer/reference/date" % f)
        elif review[i] == "reviewed" and r.get("sha256") != got:
            review[i] = "invalid"
            errs.append("%s: review sha256 is missing or differs from the patch's SHA-256 (review not bound to these bytes)" % f)
        elif review[i] != "reviewed":
            errs.append("%s: review status %s (only reviewed patches are applied)" % (f, review[i]))
    return errs, review

def main_run(src, tmpd, cmd, as_json, head_sha, npatch, head_full):
    rd = lambda f: open(os.path.join(tmpd, f), "rb").read()
    names = [rd("m/%d.name" % i) for i in range(npatch)]
    gate_errs, review = manifest_gate(tmpd, names, head_full)
    mpath = rd("manifest.path")
    unsafe = [rd("m/%d.unsafe" % i).decode("utf-8", "backslashreplace").strip() for i in range(npatch)]
    text_attr = [set(x for x in rd("m/%d.text" % i).split(b"\0") if x) for i in range(npatch)]
    filemode = git(src, "config", "--bool", "--get", "core.filemode", check=False).stdout.strip() != b"false"
    fmt = git(src, "rev-parse", "--show-object-format", check=False).stdout.strip().decode() or "sha1"
    blocks, targets = [None] * npatch, [[] for _ in range(npatch)]
    for i in range(npatch):
        if unsafe[i]:
            continue
        try:
            blocks[i], targets[i] = parse(src, os.path.join(tmpd, "p/%d.patch" % i))
            if not filemode and any(b["oldmode"] != b["newmode"] for b in blocks[i]
                                    if b["oldmode"] is not None and b["newmode"] is not None):
                raise Unsafe("core.filemode=false: a mode change is unverifiable")
        except Unsafe as e:
            unsafe[i] = "cannot parse the patch exactly: %s" % e
    allp = sorted({t for i in range(npatch) for t in targets[i]})
    # HEAD and index entries of the targets (plumbing reads only; no filters, no index refresh).
    headent, idxent = {}, {}
    want = set(allp)
    # Bound to the captured full id, never the symbolic HEAD (it may move between reads).
    for rec in git(src, "ls-tree", "-r", "-z", "--full-tree", head_full).stdout.split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            if path in want:
                headent[path] = tuple(meta.split(b" "))  # mode, type, id
    if allp:
        out = git(src, "ls-files", "-s", "-z", "--", *[b":(top,literal)" + p for p in allp]).stdout
        for rec in out.split(b"\0"):
            if rec:
                meta, path = rec.split(b"\t", 1)
                m, oid, _stage = meta.split(b" ")
                idxent[path] = (m, oid)
    ids = sorted({e[2] for e in headent.values() if e[0] in MODES})
    blobs = {}
    if ids:
        data = git(src, "cat-file", "--batch", inp=b"".join(x + b"\n" for x in ids)).stdout
        k = 0
        while k < len(data):
            nl = data.index(b"\n", k)
            oid, typ, size = data[k:nl].split(b" ")
            size = int(size); blobs[oid] = data[nl + 1:nl + 1 + size]; k = nl + 2 + size
    head, staged = {}, set()
    for p in allp:
        e = headent.get(p)
        if e is not None and e[0] not in MODES:
            head[p] = "odd"
        else:
            head[p] = None if e is None else (blobs[e[2]], e[0] == b"100755")
        ie = idxent.get(p)
        if (ie is None) != (e is None) or (ie is not None and (ie[0], ie[1]) != (e[0], e[2])):
            staged.add(p)
    wt = {p: wt_read(src, p) for p in allp}
    now = head_now(src)
    if now != head_full:
        sys.stderr.write("HEAD changed during classification (was %s, now %s); result unknown, nothing written\n"
                         % (head_full, tclean("(unreadable)" if now is None else now)))
        sys.exit(2)
    # Text-converted targets: the conversions are the identity only without CR bytes.
    for i in range(npatch):
        if unsafe[i]:
            continue
        for p in text_attr[i]:
            body = b"".join(v for b in blocks[i] for h in b["hunks"] for _, v in h["lines"]
                            if p in (b["old"], b["new"]))
            hs, ws = head.get(p), wt.get(p)
            if b"\r" in body or (isinstance(hs, tuple) and b"\r" in hs[0]) or (isinstance(ws, tuple) and b"\r" in ws[0]):
                unsafe[i] = "target %s has a text/eol attribute and CR bytes (conversion would change bytes)" % qpath(p)
        if not unsafe[i] and any(head[p] == "odd" for p in targets[i]):
            unsafe[i] = "a target is not a regular file in HEAD"
    # Peel: walk the set backwards from HEAD, removing every patch whose change HEAD carries
    # (committed patches, also when a later one rewrote their context), to get the base.
    head_ok = {p: v for p, v in head.items() if v != "odd"}
    kind, reason, poisoned = [None] * npatch, [""] * npatch, set()
    for i in range(npatch):
        if unsafe[i]:
            kind[i] = "unsafe"; poisoned.update(targets[i])
    ctx = {"fmt": fmt, "filemode": filemode, "head": head_ok}
    base = dict(head_ok)
    for i in reversed(range(npatch)):
        if kind[i] or poisoned.intersection(targets[i]):
            continue
        rev, _ = run_patch(base, blocks[i], ctx, True)
        if rev is None:
            continue
        if run_patch(base, blocks[i], ctx, False)[0] is not None:
            kind[i] = "ambiguous"; reason[i] = "applies both forward and in reverse; resolve by hand"
            poisoned.update(targets[i]); continue
        base = rev
    # Replay forward from the base: every patch must apply in order, in one direction only.
    ctx["head"] = base
    state = dict(base)
    cands = {p: [state.get(p)] for p in allp}
    pendpos = {}
    for i in range(npatch):
        if kind[i]:
            continue
        if poisoned.intersection(targets[i]):
            kind[i] = "blocked"; reason[i] = "another patch on the same file is unsafe, conflicting or ambiguous, so this patch's base is unknown"
            poisoned.update(targets[i]); continue
        fwd, why_f = run_patch(state, blocks[i], ctx, False)
        rev, _ = run_patch(state, blocks[i], ctx, True)
        if fwd is not None and rev is None:
            kind[i] = "pending"; state = fwd
            for p in targets[i]:
                cands[p].append(state.get(p)); pendpos[(i, p)] = len(cands[p]) - 1
            continue
        if fwd is not None:
            kind[i] = "ambiguous"; reason[i] = "applies both forward and in reverse; resolve by hand"
        elif rev is not None:
            kind[i] = "ambiguous"; reason[i] = "its change is already present where the earlier patches leave the file (duplicate?)"
        else:
            kind[i] = "conflict"; reason[i] = why_f
        poisoned.update(targets[i])
    # Reconcile: HEAD and the worktree must each equal exactly one state of every target's chain.
    def locate(get, extra_bad):
        pos, bad, multi = {}, set(), set()
        for p in allp:
            v = get(p)
            if p in extra_bad or v == "odd":
                bad.add(p); continue
            hits = [k for k, c in enumerate(cands[p]) if same(c, v, filemode)]
            if not hits:
                bad.add(p)
            elif len(hits) > 1:
                multi.add(p)
            else:
                pos[p] = hits[0]
        return pos, bad, multi
    hpos, hbad, hmulti = locate(head_ok.get, set())
    wpos, unexplained, wmulti = locate(wt.get, staged)
    unexplained.update(p for p in wpos if p in hpos and wpos[p] < hpos[p])  # worktree undoes a commit
    states, actions, dirty = [], [], []
    for i in range(npatch):
        k, d, t = kind[i], [], targets[i]
        st, act = k, "none"
        if k == "unsafe":
            act = "unsafe: " + unsafe[i]
        elif k != "pending":
            act = "%s: %s" % (k, reason[i])
        elif any(p in hbad for p in t):
            st, act = "conflict", "conflict: HEAD is not a state of the patch chain at " + " ".join(qpath(p) for p in t if p in hbad)
        elif any(p in hmulti or p in wmulti for p in t):
            st, act = "ambiguous", "ambiguous: HEAD or the worktree matches several states of the patch chain"
        else:
            d = sorted(p for p in t if p in unexplained)
            in_head = [hpos[p] >= pendpos[(i, p)] for p in t]
            in_wt = [p in wpos and wpos[p] >= pendpos[(i, p)] for p in t]
            if any(in_head) and not all(in_head):
                st, act = "conflict", "conflict: only partly committed in HEAD"
            elif d:
                st, act = "dirty", "dirty: local changes the patch set does not explain: " + " ".join(qpath(p) for p in d)
            elif all(in_head):
                st = "applied"
            elif all(in_wt):
                st, act = "applied", "applied: present in the worktree, uncommitted (proven against the simulation)"
            elif any(in_wt):
                st, act = "dirty", "dirty: only partly present in the worktree"
            else:
                st = "applicable"
        states.append(st); actions.append(act); dirty.append(d)
    sim_good = all(s in ("applied", "applicable") for s in states)
    good = sim_good and not gate_errs
    rc = 0 if cmd == "status" or good else 1
    todo = [i for i in range(npatch) if states[i] == "applicable"] if cmd == "apply" else []
    if cmd == "apply":
        for i in range(npatch):
            if not good and states[i] == "applicable":
                actions[i] = ("not applied: another patch in the set was refused (nothing written)" if not sim_good
                              else "not applied: the manifest gate refused the set (nothing written)")
            elif states[i] not in ("applied", "applicable"):
                actions[i] = "refused: " + (actions[i] if actions[i] != "none" else states[i])
        if not good:
            todo = []
    # Render everything that depends on untrusted bytes BEFORE any write.
    def enc(b):
        return b.decode("utf-8") if isinstance(b, bytes) else b
    try:
        jfix = {"src": enc(os.fsencode(src)), "head": enc(head_sha), "names": [enc(x) for x in names],
                "mpath": enc(mpath), "gate": [enc(os.fsencode(e)) for e in gate_errs],
                "dirty": [[enc(p) for p in d] for d in dirty], "actions": [enc(a) for a in actions]}
    except UnicodeDecodeError:
        if as_json:
            sys.stderr.write("cannot JSON-encode the runtime path, a patch name or a target (non-UTF-8); nothing was applied\n")
            sys.exit(2)
        jfix = None
    # Write outcome: not_attempted (refused or nothing to do; no byte written), verified (the
    # written targets equal the simulation) or unknown (the real write began and then failed or
    # could not be verified; the checkout MAY BE CHANGED, applied_now is then None).
    applied_now, outcome, trace = 0, "not_attempted", {"attempted": False}
    if todo:
        try:
            ok, why = write_set(src, tmpd, todo, targets, cands, wt, filemode, head_full, trace)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            ok, why = False, ("error during the write phase (inspect the checkout; nothing is rolled back): %s" % e
                              if trace["attempted"] else "error before the write (nothing written): %s" % e)
        if ok:
            outcome = "verified"
        elif trace["attempted"]:
            outcome, applied_now = "unknown", None
        for i in todo:
            if ok:
                actions[i] = "applied"; states[i] = "applied"; applied_now += 1
            elif trace["attempted"]:
                actions[i] = "unknown: " + why; states[i] = "unknown"
            else:
                actions[i] = "failed: " + why
        if not ok:
            rc = 1
    if as_json:
        doc = {"src": jfix["src"], "head": jfix["head"], "patches": [
            {"patch": jfix["names"][i], "state": states[i], "action": enc(actions[i]),
             "dirty_targets": jfix["dirty"][i], "review": review[i]} for i in range(npatch)],
            "manifest": {"path": jfix["mpath"], "verified": not gate_errs, "errors": jfix["gate"]}, "exit": rc}
        if cmd == "apply":
            doc.update(outcome=outcome, write_attempted=trace["attempted"], applied_now=applied_now)
        emit(json.dumps(doc, separators=(",", ":")) + "\n", trace["attempted"])
    else:
        out = ["runtime: %s @ %s" % (tclean(os.fsencode(src)), tclean(head_sha))]
        if gate_errs:
            out.append("manifest: NOT VERIFIED (%s)" % tclean(mpath))
            out.extend("  manifest refusal: %s" % tclean(os.fsencode(e)) for e in gate_errs)
        else:
            out.append("manifest: verified (%s; base == HEAD, every patch reviewed and hash-pinned)" % tclean(mpath))
        for i in range(npatch):
            line = "  %s\t%s\treview=%s" % (states[i], tclean(names[i]), review[i])
            if actions[i] != "none":
                line += "  [%s]" % tclean(actions[i])
            out.append(line)
        if cmd == "apply":
            out.append("outcome: %s" % outcome)
            if applied_now is None:
                out.append("applied now: unknown (the write began and is unverified; the checkout MAY BE CHANGED: "
                           "run status before any retry, nothing is rolled back)")
            else:
                out.append("applied now: %d (isolated checkout only; deploying a reviewed build is a separate authorized operation)" % applied_now)
        emit("\n".join(out) + "\n", trace["attempted"])
    sys.exit(rc)

def emit(text, attempted):
    """Write the report. Delivery is not part of the operation: when stdout fails (full disk,
    closed pipe) after the real write began (attempted), the checkout may already be changed. Say so on stderr
    (best effort) and exit 4; the caller must inspect with `status` before any retry."""
    try:
        if sys.stdout is None:  # fd 1 closed at startup (the shell guard normally stops this earlier)
            raise OSError("stdout is closed")
        sys.stdout.write(text); sys.stdout.flush()
    except (OSError, ValueError) as e:
        try:
            msg = ("report not delivered (%s); the real write began, the checkout MAY BE CHANGED: "
                   "run status before any retry\n" if attempted else "report not delivered (%s); nothing was written\n")
            sys.stderr.write(msg % tclean(str(e))); sys.stderr.flush()
        except (OSError, ValueError, AttributeError):
            pass
        os._exit(4)  # skip the interpreter's own retry of the failed flush

def write_set(src, tmpd, todo, targets, cands, wt, filemode, head_full, trace):
    """Scratch replay, re-read, one real git apply, post-verify. (ok, reason); trace["attempted"]
    is set just before the real git apply, so any failure after it is an unknown outcome."""
    paths = sorted({p for i in todo for p in targets[i]})
    expect = {p: cands[p][-1] for p in paths}
    setp = os.path.join(tmpd, "set.patch")
    with open(setp, "wb") as f:
        for i in todo:
            body = open(os.path.join(tmpd, "p/%d.patch" % i), "rb").read()
            f.write(body if body.endswith(b"\n") else body + b"\n")
    conv = ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "apply.whitespace=nowarn",
            "-c", "apply.ignoreWhitespace=no"]
    scratch, tmpl = os.path.join(tmpd, "scratch"), os.path.join(tmpd, "tmpl")
    os.makedirs(tmpl)
    env = dict(GIT_ENV, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(["git", "init", "-q", "--template=" + tmpl, scratch], check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sb = os.fsencode(scratch)
    for p in paths:
        if wt[p] is None:
            continue
        fp = sb + b"/" + p
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as f:
            f.write(wt[p][0])
        os.chmod(fp, 0o755 if wt[p][1] else 0o644)
    r = subprocess.run(["git", "-c", "core.attributesFile=" + os.devnull, *conv, "apply", "--whitespace=nowarn", setp],
                       cwd=scratch, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0 or any(not same(wt_read(scratch, p), expect[p], filemode) for p in paths):
        return False, "git apply disagrees with the simulation in a scratch copy (nothing written)"
    if any(not same(wt_read(src, p), wt[p], filemode) for p in paths):
        return False, "a target changed during classification (nothing written)"
    now = head_now(src)
    if now != head_full:
        return False, ("HEAD could not be read before the write (nothing written)" if now is None
                       else "HEAD changed before the write (nothing written)")
    trace["attempted"] = True
    r = git(src, *conv, "apply", "--whitespace=nowarn", setp, check=False)
    if r.returncode != 0:
        return False, "git apply rejected the set (check the checkout: a write-phase error is not rolled back)"
    now = head_now(src)
    if now is None:
        return False, "HEAD could not be read after the write (outcome unknown; run status, nothing is rolled back)"
    if now != head_full:
        return False, "HEAD changed during the write phase (outcome unknown; run status, nothing is rolled back)"
    bad = [p for p in paths if not same(wt_read(src, p), expect[p], filemode)]
    if bad:
        return False, "written targets differ from the simulation (not rolled back): " + " ".join(qpath(p) for p in bad)
    return True, ""

mode = sys.argv[1]
if mode == "targets":
    try:
        _, t = parse(sys.argv[2], sys.argv[3])
    except Unsafe as e:
        sys.stderr.write(tclean(str(e)) + "\n"); sys.exit(1)
    sys.stdout.buffer.write(b"".join(x + b"\0" for x in t))
else:
    try:
        main_run(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == "1", os.fsencode(sys.argv[6]), int(sys.argv[7]),
                 sys.argv[8])
    except (Unsafe, ValueError, KeyError, OSError) as e:
        sys.stderr.write("cannot read the checkout: %s\n" % tclean(str(e))); sys.exit(2)
PY

# patch_targets PATCH: TARGETS=(every path the patch reads or writes); else UNSAFE=reason.
patch_targets() {
  TARGETS=()
  if ! python3 -c "$PY" targets "$SRC" "$1" > "$TMPD/t" 2> "$TMPD/e"; then
    UNSAFE="cannot enumerate patch targets: $(head -n1 "$TMPD/e")"; return 1
  fi
  mapfile -d '' TARGETS < "$TMPD/t"
  [[ ${#TARGETS[@]} -gt 0 ]] || { UNSAFE="patch names no files"; return 1; }
  SPECS=(); local t
  for t in "${TARGETS[@]}"; do SPECS+=(":(top,literal)$t"); done
}

# Before anything reads a target from the worktree: no target may carry a filter, ident,
# working-tree-encoding or non-LF eol attribute or be a submodule. TEXT=(targets with a text
# or crlf attribute; the simulator requires them CR-free so conversion is the identity).
target_gate() {
  local -a a; local i v
  TEXT=()
  gitq -C "$SRC" check-attr -z filter ident working-tree-encoding eol text crlf -- "${TARGETS[@]}" > "$TMPD/a" 2>/dev/null || { UNSAFE="cannot read the targets' attributes"; return 1; }
  mapfile -d '' a < "$TMPD/a"
  [[ ${#a[@]} -eq $((18 * ${#TARGETS[@]})) ]] || { UNSAFE="unexpected git check-attr output"; return 1; }
  for ((i = 0; i < ${#a[@]}; i += 3)); do
    v="${a[i+2]}"
    [[ "$v" == unspecified || "$v" == unset ]] && continue
    case "${a[i+1]}" in
      filter) UNSAFE="a target has filter=$v (repo-configured filters are never run)"; return 1 ;;
      eol) [[ "$v" == lf ]] || { UNSAFE="a target has eol=$v (conversion would change bytes)"; return 1; }; TEXT+=("${a[i]}") ;;
      text|crlf) TEXT+=("${a[i]}") ;;
      *) UNSAFE="a target has the ${a[i+1]} attribute (conversion would change bytes)"; return 1 ;;
    esac
  done
  gitq -C "$SRC" ls-files -z -s -- "${SPECS[@]}" > "$TMPD/s" 2>/dev/null || { UNSAFE="cannot list the targets in the index"; return 1; }
  mapfile -d '' a < "$TMPD/s"
  for v in "${a[@]}"; do [[ "$v" != 160000\ * ]] || { UNSAFE="a target is a submodule"; return 1; }; done
  # assume-unchanged (lowercase tag) and skip-worktree (S) entries are invisible to
  # diff-files, so a local edit on them would pass the dirty check: refuse those targets.
  gitq -C "$SRC" ls-files -z -v -- "${SPECS[@]}" > "$TMPD/v" 2>/dev/null || { UNSAFE="cannot list the targets' index flags"; return 1; }
  mapfile -d '' a < "$TMPD/v"
  for v in "${a[@]}"; do
    [[ "$v" == "H "* ]] || { UNSAFE="target $(qpath "${v:2}") has index tag '${v%% *}' (assume-unchanged, skip-worktree or unmerged; dirtiness is unverifiable)"; return 1; }
  done
}

shopt -s nullglob
patches=("$PATCHES"/*.patch)
shopt -u nullglob
[[ ${#patches[@]} -gt 0 ]] || die2 "no *.patch files in $PATCHES"
gitq -C "$SRC" rev-parse -q --verify 'HEAD^{commit}' >/dev/null 2>&1 || die2 "no HEAD commit in $SRC to compare with"
# One read of HEAD: the short form shown is derived from it, and the simulator reads trees and
# blobs by this id and rechecks HEAD against it.
head_full="$(gitq -C "$SRC" rev-parse --verify 'HEAD^{commit}')"
head_sha="${head_full:0:10}"
# Private copy of the manifest (absent = the gate refuses); the gate hashes the private patch copies.
printf '%s' "$MANIFEST" > "$TMPD/manifest.path"
if [[ -f "$MANIFEST" ]]; then cp -- "$MANIFEST" "$TMPD/manifest.json" 2>/dev/null || rm -f "$TMPD/manifest.json"; fi
# Work on private copies: what is checked is exactly what is applied. Per patch the simulator
# gets its file name, the gate's verdict and its text-attribute targets.
mkdir "$TMPD/p" "$TMPD/m"
for i in "${!patches[@]}"; do
  cp -- "${patches[i]}" "$TMPD/p/$i.patch" || die2 "cannot read ${patches[i]}"
  printf '%s' "$(basename -- "${patches[i]}")" > "$TMPD/m/$i.name"
  UNSAFE=""; TEXT=()
  patch_targets "$TMPD/p/$i.patch" && target_gate || true
  printf '%s' "$UNSAFE" > "$TMPD/m/$i.unsafe"
  : > "$TMPD/m/$i.text"; for t in "${TEXT[@]}"; do printf '%s\0' "$t" >> "$TMPD/m/$i.text"; done
done
rc=0
python3 -c "$PY" run "$SRC" "$TMPD" "$CMD" "$JSON" "$head_sha" "${#patches[@]}" "$head_full" || rc=$?
exit "$rc"
