#!/usr/bin/env python3
"""Repository lease, competing-writer classification and task-owned change manifests for
the kanban-workflow coordinator working in an operator-designated checkout (`--checkout`).

    repo_lease.py preflight  --task T --checkout DIR --repo URL --branch B [--base main] [--json]
    repo_lease.py checkpoint --task T --checkout DIR            # record the task's own dirty files
    repo_lease.py adopt      --task T --checkout DIR --manifest FILE   # explicit provenance bootstrap
    repo_lease.py release    --task T --checkout DIR            # give the lease back
    repo_lease.py register-scanner DIR -- sudo -n /usr/local/libexec/<helper>
    repo_lease.py status     --checkout DIR [--json]

The owner of a run is never taken from the environment: it is the card's board-recorded
`worker_bound` interpreter of its current run (`bound_worker`), an ancestor of this process
with the recorded start time, and it fails closed (exit 3) when that is ambiguous (a shared
owner process, a nested worker, a sibling card bound to the same chain).

One central writer per working tree. The lease is a small JSON file outside the
repository (under the shared Kanban root) keyed by the canonical git directory and
owned by board/task/run plus the worker process identity (host, pid and its kernel
start time, so a reused pid is not mistaken for a live owner). Every acquire, renew
and release of one repository runs under a per-repository advisory lock (flock on a
private, symlink-refusing lock file), so two recoverers can never both take over a
stale lease. Recovery is bounded: a lease is taken over only when its owner process is
provably gone; an expired lease whose owner is still alive stays a conflict for the
operator.

A process whose CWD is inside the checkout is an observation, not proof of a
competing writer. Matches from the registered privileged scanner (or the unprivileged
fallback) are classified by ancestry and process facts. Only a small, explicit set of
passive tools is accepted by name; everything else fails closed:

  self-chain / own-descendant  this run's own process chain (up to its verified anchor,
                               the card's board-bound managed worker) and its descendants -> fine
  gone                         exited between scan and inspection (ENOENT/ESRCH)  -> fine
  editor / language-server /   known passive tools: editors, LSP servers, dev servers
  dev-server / shell           in serve/watch mode, idle shells; without an observer
                               grant only (classify_cmdline_ungranted), and only when
                               /proc/<pid>/exe backs the name (ungranted_image_mismatch);
                               under a grant see classify_cmdline              -> reported
  git-readonly                 git status/log/diff/show/... (ungranted) without write
                               forms (--output, grep -O, -c core.pager, ...); the exact
                               GIT_SAFE_READS forms under a grant                 -> reported
  VS Code Remote server        bundled JSON/Markdown/CSS/HTML language servers, only
                               when /proc proves it: the server's own node binary,
                               parented by that server's extension host; otherwise
                               unknown                                            -> reported
  git-mutation                 git commit/merge/rebase/checkout/reset/stash/...  -> blocked
  agent-writer                 another autonomous coding agent (hermes worker, claude,
                               codex, aider, ...) not in our chain                -> blocked
  unknown                      anything else: arbitrary python/node/scripts, every
                               interactive shell (the VS Code integrated terminal too:
                               traps, PROMPT_COMMAND or `read -t` then a write look like
                               a prompt to /proc; see SHELL_REMEDY), a live process with
                               an empty command line, a tty-attached tool that is not a
                               known editor, /proc unreadable, incomplete scan     -> blocked

Uncommitted changes are resumed only with provenance. The protocol is preflight ->
edit -> checkpoint: `preflight` (exit 0) records a baseline of the dirty tree under this
run's lease; `checkpoint` requires that live lease, a clean writer scan and that
baseline, and records as this task's own only what changed since the baseline (plus
what was already the task's). Dirty paths that were never this task's cannot pass
preflight, so they are never laundered by checkpoint; the operator's explicit `adopt`
with a {path: sha256} file, or with the strict inventory of pre-existing work bound to the
task, root, gitdir, remote, branch, HEAD and exact `git status` (files: [{path, status, type,
mode, sha256, index_oid, index_mode, origin}]; see _inventory_entries), is the only other way in. Entries carry the full identity of
a change (status, worktree type, permission bits, git mode and hash from one inode, every
index stage's object and mode, rename origin), so a
foreign `git add`, a chmod or a symlink swap on a recorded path is detected. Paths are
kept literally (a dotfile is a dotfile). Limits: everything here is cooperative
same-uid evidence; a checkpoint proves the lease was held and no competing writer was
observed, not who typed the bytes. Nothing is ever reset, stashed, cleaned or deleted.
Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.dont_write_bytecode = True

LEASE_VERSION = 2
MANIFEST_VERSION = 2
BASELINE_VERSION = 1
DEFAULT_TTL = 1800  # seconds without renewal before an ALIVE owner is reported as stale (never taken over)
PROTECTED_BRANCHES = {"main", "master", "develop", "release", "production"}
MUTATING_GIT = {"commit", "merge", "rebase", "cherry-pick", "revert", "am", "apply", "pull", "checkout",
                "switch", "reset", "stash", "restore", "clean", "mv", "rm", "add", "worktree", "push", "filter-branch",
                "bisect"}
# Pre-PR28 (d468701) read-only git subcommands: the classification used when no observer policy
# grants this checkout (see classify_cmdline_ungranted).
READONLY_GIT = {"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "ls-remote", "cat-file", "blame",
                "describe", "rev-list", "grep", "shortlog", "for-each-ref", "name-rev", "merge-base", "show-ref",
                "diff-tree", "diff-index", "diff-files", "count-objects", "var", "version", "help", "check-ignore",
                "check-attr", "symbolic-ref", "fsck", "reflog"}
# Ungranted read-only git that still writes or starts a program the invoker chose (git accepts any
# unique prefix of a long option, so abbreviations count): --output (diff/log/show... write a file),
# grep -O/--open-files-in-pager, fsck --lost-found, reflog expire/delete/drop, symbolic-ref
# update/delete, ls-remote --upload-pack, and command-line configuration (-c, --config-env, the
# GIT_CONFIG_PARAMETERS/GIT_CONFIG_COUNT environment) other than these display keys (core.pager,
# diff.external, core.fsmonitor=<hook>, alias.*, *.textconv, credential.helper... all run programs).
UNGRANTED_GIT_CONFIG_KEYS = {"core.quotepath", "core.precomposeunicode", "core.longpaths", "core.abbrev", "color.ui",
                             "column.ui", "diff.mnemonicprefix", "diff.noprefix", "diff.renames", "diff.relative",
                             "i18n.logoutputencoding", "log.decorate", "log.date", "safe.directory",
                             "core.ignorecase", "status.showuntrackedfiles", "status.relativepaths"}
UNGRANTED_GIT_CONFIG_PREFIXES = ("color.", "advice.")
REFLOG_WRITES = {"expire", "delete", "drop"}
GREP_SHORT_VALUE = set("ABCefm")  # grep short options whose value is the rest of the cluster
# Dev servers/watchers stay observations without a grant only in a serve/watch mode: a one-shot
# build writes its output. Subcommand tools: any of these positionals is a build/write command.
DEV_SERVER_WRITE_COMMANDS = {"vite": {"build", "optimize"},
                             "next": {"build", "export", "lint", "experimental-compile", "experimental-generate"},
                             "nuxt": {"build", "generate", "prepare", "cleanup", "upgrade", "add", "module", "init",
                                      "analyze"},
                             "astro": {"build", "sync", "add", "preferences", "telemetry"},
                             "parcel": {"build"}}
# Flag tools: a build unless one of these (case-insensitive; `--opt=` forms too) is present.
DEV_SERVER_WATCH_FLAGS = {"tsc": {"-w", "--watch", "--noemit"}, "rollup": {"-w", "--watch"},
                          "swc": {"-w", "--watch"}, "esbuild": {"--watch", "--serve"},
                          "webpack": {"-w", "--watch"}}
DEV_SERVER_SERVE_POSITIONALS = {"webpack": {"serve", "server", "s", "watch", "w"}}
TURBO_WATCH_TASKS = {"dev", "start", "serve"}
# Foreign git reads accepted as passive under a granted observer policy: only these subcommands, each with an explicit list of exact
# option tokens (and `--opt=` prefixes). Anything else (another subcommand, an unlisted or abbreviated
# option such as `--open-fil`, `--del`, `--lost`, a short-option cluster) is unknown. Excluded on
# purpose: status, diff*, log, show, grep, blame, describe, ls-files, reflog, fsck (worktree reads
# run the repository's clean filters, fsmonitor, textconv or external diff; porcelain runs a pager),
# ls-remote/fetch (transport). None of the listed ones reads worktree content; a listed read whose
# stdout is a terminal still blocks (pager.<cmd> config), see classify_process.
GIT_SAFE_GLOBALS = {"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs"}  # plus `-C <dir>`
GIT_SAFE_READS = {
    "rev-parse": ({"--verify", "-q", "--quiet", "--short", "--abbrev-ref", "--symbolic", "--symbolic-full-name",
                   "--show-toplevel", "--git-dir", "--absolute-git-dir", "--git-common-dir", "--is-inside-work-tree",
                   "--is-inside-git-dir", "--is-bare-repository", "--is-shallow-repository", "--show-prefix",
                   "--show-cdup", "--show-superproject-working-tree", "--git-path", "--all", "--branches", "--tags",
                   "--remotes", "--show-object-format"},
                  ("--short=", "--abbrev-ref=", "--branches=", "--tags=", "--remotes=", "--show-object-format=")),
    "cat-file": ({"-t", "-s", "-e", "-p", "--batch", "--batch-check", "--batch-all-objects", "--buffer", "--unordered",
                  "-z", "-Z"}, ("--batch=", "--batch-check=")),
    "ls-tree": ({"-r", "-t", "-d", "-l", "-z", "--name-only", "--name-status", "--object-only", "--full-name",
                 "--full-tree", "--long", "--abbrev"}, ("--format=", "--abbrev=")),
    "for-each-ref": ({"--count", "--sort", "--format", "--points-at", "--merged", "--no-merged", "--contains",
                      "--no-contains", "--ignore-case"},
                     ("--count=", "--sort=", "--format=", "--points-at=", "--merged=", "--no-merged=", "--contains=",
                      "--no-contains=")),
    "show-ref": ({"--heads", "--tags", "--head", "-d", "--dereference", "-s", "--hash", "--verify", "-q", "--quiet",
                  "--abbrev", "--exists"}, ("--hash=", "--abbrev=")),
    "rev-list": ({"--count", "--all", "--branches", "--tags", "--remotes", "-n", "--max-count", "--reverse",
                  "--left-right", "--first-parent", "--no-merges", "--merges", "--topo-order", "--date-order",
                  "--parents", "--ancestry-path", "--boundary", "--quiet", "--not"},
                 ("--max-count=", "--since=", "--until=", "--after=", "--before=", "--branches=", "--tags=",
                  "--remotes=")),
    "merge-base": ({"--is-ancestor", "--all", "--octopus", "--independent", "--fork-point"}, ()),
    "symbolic-ref": ({"--short", "-q", "--quiet"}, ()),  # read form only: exactly one name, see _git_safe_read
    "var": ({"-l"}, ()),
    "version": (set(), ()),
}
# Autonomous coding agents by executable name. Blocking only when the process is NOT in our own
# chain. PASSIVE_EDITORS (with a root-owned image, see passive_image_mismatch), GIT_SAFE_READS and
# the /proc-verified VS Code bundled language servers are the ONLY foreign processes that are
# non-blocking; they are observations (reported), never writers by CWD alone.
AGENT_EXECUTABLES = {"hermes", "claude", "codex", "aider", "gemini", "cursor-agent", "copilot", "goose", "opencode"}
AGENT_CMDLINE_HINTS = ("work kanban task", "hermes_cli.main", "delegate_task")
PASSIVE_EDITORS = {"vim", "nvim", "vi", "view", "nano", "pico", "emacs", "emacsclient", "hx", "helix", "micro", "kak",
                   "code", "code-server", "code-insiders", "zed", "subl", "sublime_text"}
# Known by name but never accepted by name: a user copy of python named gopls has the image name
# gopls, and build tools/dev servers write output or run commands (tsc, rollup -c, vite build,
# esbuild --outfile, nodemon --exec). Listed only to report them precisely; they are unknown.
PASSIVE_LANGUAGE_SERVERS = {"gopls", "pyright-langserver", "pylsp", "pyls", "jedi-language-server", "typescript-language-server",
                            "tsserver", "rust-analyzer", "clangd", "sourcekit-lsp", "lua-language-server",
                            "bash-language-server", "yaml-language-server", "vscode-json-language-server",
                            "vscode-css-language-server", "vscode-html-language-server", "kotlin-language-server", "jdtls",
                            "eslint_d", "prettierd", "copilot-language-server", "tailwindcss-language-server", "ruff", "ruff-lsp"}
PASSIVE_DEV_SERVERS = {"vite", "webpack-dev-server", "webpack", "next", "nuxt", "astro", "nodemon", "air", "esbuild",
                       "browser-sync", "live-server", "http-server", "serve", "parcel", "rollup", "tsc", "turbo", "swc"}
INTERACTIVE_SHELLS = {"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh"}
# Shell arguments of a plain interactive/login shell (reported as such; it still blocks, see
# SHELL_REMEDY). Anything else (-c, -s/--stdin, combined -lc/-sc, a script, ...) is reported as a
# shell running a command.
IDLE_SHELL_OPTIONS = {"-i", "-l", "--login", "--interactive"}
SCRIPT_INTERPRETERS = {"node", "nodejs", "deno", "bun", "python", "python3", "ruby", "perl"}
# Observations that are not writers; everything not listed here is "unknown" and blocks.
NONBLOCKING_KINDS = {"self-chain", "own-descendant", "gone", "editor", "language-server", "git-readonly",
                     "trusted-terminal"}
# VS Code Remote server components, accepted only after /proc confirms them (classify_vscode); by
# command line alone they stay unknown. The server root is .../.vscode-server/cli/servers/
# Stable-<commit>/server (or the legacy .../.vscode-server/bin/<commit>); its own `node` runs
# the extension host and pty host (`out/bootstrap-fork --type=...`).
VSCODE_SERVER_RE = re.compile(r"^(/.+/\.vscode-server(?:-insiders)?/(?:cli/servers/(?:Stable|Insiders)-[0-9a-f]{40}/server"
                              r"|bin/[0-9a-f]{40}))/")
# Bundled, read-only language servers by exact entry script under the server root.
VSCODE_LANGUAGE_SERVERS = {"extensions/json-language-features/server/dist/node/jsonServerMain",
                           "extensions/markdown-language-features/dist/serverWorkerMain",
                           "extensions/css-language-features/server/dist/node/cssServerMain",
                           "extensions/html-language-features/server/dist/node/htmlServerMain"}
VSCODE_BASH_INTEGRATION = "out/vs/workbench/contrib/terminal/common/scripts/shellIntegration-bash.sh"
MATCH_RE = re.compile(r"^MATCH:\s*PID=(\d+)\s+CWD=(\S.*)$")
# The registered helper's exact protocol (see parse_scan_output).
PASS_INSPECTED_RE = re.compile(r"^PASS: Inspected ([1-9]\d*) process directories\.$")
PASS_NO_MATCH = "PASS: No repository CWD matches found."
BLOCKED_RE = re.compile(r"^BLOCKED: ([1-9]\d*) matching processes\.$")
# Where `sudo` is taken from for a registered privileged scanner: never the caller's PATH.
TRUSTED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
TASK_RE = re.compile(r"^t_[0-9a-f]{6,16}$|^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

GitRunner = Callable[[list, str], subprocess.CompletedProcess]
ScanRunner = Callable[[list], subprocess.CompletedProcess]


class LeaseError(RuntimeError):
    pass


class ProcVisibilityError(OSError):
    """/proc entry exists but cannot be read (EACCES/EPERM): unknown, never 'gone'."""


O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


# ------------------------------------------------------------------ state ---

def state_root() -> Path:
    """Shared Kanban run state for every profile on this host (same rule as workflow.py)."""
    override = os.environ.get("HERMES_WORKFLOW_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if db:
        return Path(db).expanduser().parent / "kanban" / "workflow-runs"
    root = os.environ.get("HERMES_KANBAN_HOME", "").strip() or os.environ.get("HERMES_HOME", "").strip()
    root_path = Path(root).expanduser() if root else Path.home() / ".hermes"
    if root_path.parent.name == "profiles":
        root_path = root_path.parent.parent
    return root_path / "kanban" / "workflow-runs"


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise LeaseError(f"{path} is a symlink; refusing to use it for state")
    return path


def _read_json_nofollow(path: Path) -> Optional[dict]:
    """Read a JSON object without following a symlink at the final path component."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LeaseError(f"{path} is a symlink; refusing to read it")
        raise
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _write_private_tmp(directory: Path, payload: dict) -> Path:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(directory))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True, indent=1)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    return Path(tmp)


def _atomic_replace(directory: Path, name: str, payload: dict) -> Path:
    target = directory / name
    if target.is_symlink():
        raise LeaseError(f"{target} is a symlink; refusing to replace it")
    tmp = _write_private_tmp(directory, payload)
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------- repository ---

def git_text(git: GitRunner, argv: list, cwd: str) -> str:
    r = git(argv, cwd)
    if r.returncode != 0:
        raise LeaseError(f"git {' '.join(argv[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return (r.stdout or "").strip()


# Inherited variables that would point git at another repository, index or object store than the
# checkout given as cwd. Only these are dropped; everything else is inherited as before.
GIT_REDIRECT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES")


FILTER_DRIVER_RE = re.compile(r"^filter\.(.+)\.(clean|process)$", re.S)
# Own git subcommands that read worktree content and so apply clean filters (`status` hashes a
# stat-dirty file through its filter). Our other calls (rev-parse, ls-files -s, cat-file blob,
# rev-list, worktree list, branch, remote) never run a filter.
FILTERED_OWN_GIT = {"status"}


def own_git(argv: list, cwd: str, env: dict, **kw) -> subprocess.CompletedProcess:
    """Run this module's (or workflow.py's) own git call. core.fsmonitor=false always. Only for a
    checkout a valid observer policy grants (_granted_checkout; otherwise git applies its filters
    as before PR28), without repository-configured filters:
    core.fsmonitor=false always; for FILTERED_OWN_GIT, every `filter.<driver>.clean`/`.process`
    configured anywhere (as `git config --get-regexp` sees it, includes and command scope) is
    overridden empty on the command line, so git applies no filter. A `required` filter then fails
    the call (git refuses), and a driver a tracked path uses fails it as unsupported
    (_filters_in_use) instead of reporting un-normalized files as changed. A driver name the override
    cannot express, or an unreadable config, fails the call instead of running git unneutered.
    Not covered: submodule repositories' own filter drivers (git recurses into them with their own
    config); see docs."""
    base = ["git", "-c", "core.fsmonitor=false"]
    if _git_split(["git", *argv])[1] in FILTERED_OWN_GIT and _granted_checkout(cwd, env):
        drivers = set()
        listed = subprocess.run([*base, "config", "-z", "--get-regexp", r"^filter\..*\.(clean|process)$"], cwd=cwd,
                                capture_output=True, timeout=120, env=env)
        if listed.returncode not in (0, 1):
            return subprocess.CompletedProcess(argv, 128, "", "cannot list filter drivers: "
                                               + listed.stderr.decode("utf-8", "replace")[:300])
        for entry in filter(None, listed.stdout.split(b"\0")):
            key = entry.split(b"\n", 1)[0].decode("utf-8", "surrogateescape")
            m = FILTER_DRIVER_RE.match(key)
            if not m or "=" in key or "\n" in key:
                return subprocess.CompletedProcess(argv, 128, "", f"filter driver {key[:80]!r} cannot be disabled")
            base += ["-c", f"filter.{m.group(1)}.clean=", "-c", f"filter.{m.group(1)}.process="]
            drivers.add(m.group(1))
        used = _filters_in_use(base, cwd, env, drivers)
        if used:
            return subprocess.CompletedProcess(argv, 128, "", used)
    return subprocess.run([*base, *argv], cwd=cwd, capture_output=True, timeout=120, env=env, **kw)


def _granted_checkout(cwd: str, env: dict) -> bool:
    """True only when a valid observer policy grants the checkout at *cwd* on this host and uid
    (observer_grant). rev-parse and remote get-url run no filter or fsmonitor."""
    def out(*argv):
        r = subprocess.run(["git", "-c", "core.fsmonitor=false", *argv], cwd=cwd, capture_output=True, text=True,
                           timeout=120, env=env)
        return r.stdout.strip() if r.returncode == 0 else ""
    root, gitdir = out("rev-parse", "--show-toplevel"), out("rev-parse", "--absolute-git-dir")
    if not root or not gitdir:
        return False
    identity = {"root": str(Path(root).resolve()), "gitdir": str(Path(gitdir).resolve()),
                "remote": out("remote", "get-url", "origin")}
    return observer_grant(identity).get("status") == "granted"


def _filters_in_use(base: list, cwd: str, env: dict, drivers: set) -> Optional[str]:
    """Why a granted own `git status` cannot run: a configured clean/process driver is the filter
    attribute of a tracked path. Disabling it would report normalized files as changed; running it
    runs repository-configured code; so the read fails closed as unsupported. None: no tracked path
    uses one; a symlink or gitlink never runs a filter, so only regular files count. check-attr and
    ls-files run no filter."""
    if not drivers:
        return None
    staged = subprocess.run([*base, "ls-files", "-s", "-z"], cwd=cwd, capture_output=True, timeout=120, env=env)
    # git converts only regular files: a symlink is hashed as its target, a gitlink is a commit id.
    regular = [entry.split(b"\t", 1)[1] for entry in staged.stdout.split(b"\0")
               if b"\t" in entry and entry.startswith((b"100644 ", b"100755 "))]
    attrs = subprocess.run([*base, "check-attr", "-z", "--stdin", "filter"], cwd=cwd,
                           input=b"".join(path + b"\0" for path in regular), capture_output=True, timeout=120, env=env)
    if staged.returncode != 0 or attrs.returncode != 0:
        return "cannot read filter attributes of tracked paths"
    parts = attrs.stdout.split(b"\0")
    for i in range(0, len(parts) - 2, 3):
        value = parts[i + 2].decode("utf-8", "surrogateescape")
        if value in drivers:
            path = parts[i].decode("utf-8", "replace")
            return (f"unsupported: filter driver {value!r} applies to tracked path {path[:120]!r}; a trusted-observer "
                    "status neither runs repository filters nor reports their un-normalized content as changes")
    return None


def default_git(argv: list, cwd: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in GIT_REDIRECT_ENV}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    # surrogateescape: output round-trips to git's exact bytes (status digests, non-UTF-8 paths).
    # own_git: never a repository-configured fsmonitor hook or clean filter on our reads.
    return own_git(argv, cwd, env, text=True, encoding="utf-8", errors="surrogateescape")


def git_bytes(out) -> bytes:
    """Exact bytes of a runner's stdout (str from default_git round-trips via surrogateescape)."""
    if isinstance(out, bytes):
        return out
    return (out or "").encode("utf-8", "surrogateescape")


def same_remote(a: str, b: str) -> bool:
    norm = lambda url: url.strip().rstrip("/").removesuffix(".git")  # noqa: E731
    return norm(a) == norm(b)


def repo_identity(checkout: str, git: GitRunner) -> dict:
    """Canonical facts of the working tree at *checkout* (must be its top level)."""
    path = str(Path(checkout).resolve())
    if not Path(path).is_dir():
        raise LeaseError(f"checkout '{checkout}' is not a directory on this host")
    top = git_text(git, ["rev-parse", "--show-toplevel"], path)
    if str(Path(top).resolve()) != path:
        raise LeaseError(f"'{checkout}' is not the top of a git working tree (top is '{top}')")
    gitdir = str(Path(git_text(git, ["rev-parse", "--absolute-git-dir"], path)).resolve())
    r = git(["remote", "get-url", "origin"], path)
    remote = (r.stdout or "").strip() if r.returncode == 0 else ""
    branch = git_text(git, ["branch", "--show-current"], path)
    r = git(["rev-parse", "HEAD"], path)
    head = (r.stdout or "").strip() if r.returncode == 0 else ""
    return {"root": path, "gitdir": gitdir, "remote": remote, "branch": branch, "head": head}


def repo_key(identity: dict) -> str:
    return hashlib.sha256(identity["gitdir"].encode("utf-8")).hexdigest()[:32]


# -------------------------------------------------------------- /proc ---

def _proc_read(pid: int, name: str) -> Optional[bytes]:
    """Bytes of /proc/<pid>/<name>; None when the process is gone; ProcVisibilityError when
    it exists but may not be read."""
    try:
        with open(f"/proc/{pid}/{name}", "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        raise ProcVisibilityError(errno.EACCES, f"/proc/{pid}/{name}: {exc.strerror}")
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ESRCH):
            return None
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise ProcVisibilityError(exc.errno, f"/proc/{pid}/{name}: {exc.strerror}")
        raise


def proc_stat_fields(pid: int) -> Optional[list]:
    raw = _proc_read(pid, "stat")
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace")
    close = text.rfind(")")
    if close < 0:
        return None
    head, tail = text[:close], text[close + 1:].split()
    comm = head.split("(", 1)[1] if "(" in head else ""
    return [head.split()[0], comm, *tail]


def proc_start_ticks(pid: int) -> Optional[int]:
    """Kernel start time of *pid* (clock ticks since boot; /proc/<pid>/stat field 22).
    None when the process is gone. Together with the pid this identifies one process
    incarnation, so a reused pid never passes as the lease owner."""
    fields = proc_stat_fields(pid)
    if not fields or len(fields) < 22:
        return None
    try:
        return int(fields[21])
    except ValueError:
        return None


def proc_parent(pid: int) -> Optional[int]:
    fields = proc_stat_fields(pid)
    if not fields or len(fields) < 4:
        return None
    try:
        return int(fields[3])
    except ValueError:
        return None


def proc_has_tty(pid: int) -> Optional[bool]:
    fields = proc_stat_fields(pid)
    if not fields or len(fields) < 7:
        return None
    try:
        return int(fields[6]) != 0
    except ValueError:
        return None


def proc_cmdline(pid: int) -> Optional[list]:
    """argv of *pid* exactly as /proc reports it, empty elements included (an empty argv[0]
    must not promote argv[1] to the program name); [] when /proc reports nothing (an exited
    leader thread, a zombie, a rewritten argv: not proof that the process is gone); None when
    gone."""
    raw = _proc_read(pid, "cmdline")
    if raw is None:
        return None
    if not raw:
        return []
    if raw.endswith(b"\0"):
        raw = raw[:-1]
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0")]


def proc_exited(pid: int) -> Optional[bool]:
    """True when *pid* can no longer run code: gone, or a zombie whose only task is its exited
    leader. False when any task of it may still run. ProcVisibilityError when not readable."""
    fields = proc_stat_fields(pid)
    if not fields or len(fields) < 3:
        return True
    if fields[2] not in ("Z", "X"):
        return False
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise ProcVisibilityError(exc.errno, f"/proc/{pid}/task: {exc.strerror}")
    for tid in tasks:
        sub = _proc_read(pid, f"task/{tid}/stat")
        if sub is None:
            continue
        text = sub.decode("utf-8", "replace")
        state = text[text.rfind(")") + 1:].split()[:1]
        if state not in (["Z"], ["X"]):
            return False
    return True


def proc_exe(pid: int) -> Optional[str]:
    """Resolved /proc/<pid>/exe; None when gone; ProcVisibilityError when not readable."""
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ESRCH):
            return None
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise ProcVisibilityError(exc.errno, f"/proc/{pid}/exe: {exc.strerror}")
        raise


def proc_children(pid: int) -> Optional[list]:
    """Child pids of every thread of *pid* (/proc/<pid>/task/*/children); None when gone."""
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ESRCH):
            return None
        raise ProcVisibilityError(exc.errno, f"/proc/{pid}/task: {exc.strerror}")
    kids = []
    for tid in tids:
        raw = _proc_read(pid, f"task/{tid}/children")
        if raw is None:
            continue
        kids += [int(x) for x in raw.split()]
    return kids


def proc_alive(pid: int, start_ticks: Optional[int]) -> Optional[bool]:
    """True/False when decidable; None when /proc is not visible for *pid*."""
    try:
        current = proc_start_ticks(pid)
    except ProcVisibilityError:
        return None
    if current is None:
        return False
    return start_ticks is None or current == start_ticks


def ancestors(pid: int, limit: int = 64) -> list:
    """pid's ancestors, nearest first (excluding pid), stopping at 1/0 or an unreadable link."""
    out = []
    current = pid
    for _ in range(limit):
        try:
            parent = proc_parent(current)
        except ProcVisibilityError:
            break
        if parent is None or parent <= 0 or parent == current:
            break
        out.append(parent)
        if parent == 1:
            break
        current = parent
    return out


# Upstream's per-attempt private interpreter (`python -m agent.managed_worker`): the process
# `bind_worker_context` binds to exactly one card run and records as that run's `worker_bound`
# event. The profile's owner (gateway) process is not one: it hosts many cards at once.
MANAGED_WORKER_MODULE = "agent.managed_worker"


class IdentityError(LeaseError):
    """No task-scoped worker identity for this shell: fail closed (exit 3, capability)."""


def is_managed_worker(cmd: Optional[list]) -> bool:
    cmd = cmd or []
    return any(a == "-m" and b == MANAGED_WORKER_MODULE for a, b in zip(cmd, cmd[1:4])) or \
        any(part.endswith("agent/managed_worker.py") for part in cmd[:3])


def instantiation_epoch() -> str:
    """`<boot_id>:<pid 1 start ticks>`, the epoch half of upstream's worker fingerprint."""
    boot_id = pid1 = ""
    with contextlib.suppress(OSError):
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    with contextlib.suppress(OSError):
        pid1 = str(proc_start_ticks(1) or "")
    return f"{boot_id}:{pid1}" if (boot_id or pid1) else ""


def _payload(event: dict) -> dict:
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def current_run(show: dict) -> dict:
    """The card's one live run from `hermes kanban show --json` (the task dict carries no
    current_run_id): the card is running and exactly one run is running."""
    task = show.get("task") or {}
    if task.get("status") != "running":
        raise IdentityError(f"card {task.get('id')} is {task.get('status')}, not running")
    live = [r for r in show.get("runs") or [] if r.get("status") == "running" and not r.get("ended_at")]
    if len(live) != 1:
        raise IdentityError(f"card {task.get('id')} has {len(live)} running runs; its current run is not decidable")
    return live[0]


def run_bindings(show: dict, run_id) -> tuple:
    """(latest worker_bound payload or None, owner_admitted pids) of one run."""
    bound, owners = None, set()
    for e in show.get("events") or []:
        if str(e.get("run_id")) != str(run_id):
            continue
        if e.get("kind") == "worker_bound":
            bound = _payload(e)
        elif e.get("kind") == "owner_admitted" and isinstance(_payload(e).get("pid"), int):
            owners.add(_payload(e)["pid"])
    return bound, owners


def live_binding_pids(show: dict) -> set:
    """Pids another running card is bound to (its bound interpreter and its owner); empty
    when it is not running. Used only to refuse ambiguity, never to grant anything."""
    try:
        run = current_run(show)
    except IdentityError:
        return set()
    bound, owners = run_bindings(show, run.get("id"))
    pids = set(owners)
    if bound and isinstance(bound.get("pid"), int):
        pids.add(bound["pid"])
    return pids


def bound_worker(task_id: str, show: dict, others: Iterable[dict] = (), *, explicit_pid: Optional[int] = None,
                 self_pid: Optional[int] = None, cmdline: Callable[[int], Optional[list]] = None,
                 epoch: Callable[[], str] = None) -> dict:
    """Lease owner of this shell for card *task_id*, from board facts only (no environment
    variable is identity): the card's current run R, the latest `worker_bound` event of R
    (pid P, fingerprint `<epoch>|<start ticks>`), P an ancestor of this process with that
    exact start time and epoch, and P a per-attempt managed worker interpreter.

    Fails closed (IdentityError) on anything ambiguous: P is the profile's owner process or
    not a managed worker (an owner hosts many cards, so its descendants are not attributable),
    another managed worker sits between this process and P, or another running card of
    *others* is bound to P or to a process in that chain. Cooperative observation only: it
    shows which process tree is this card's worker, it does not exclude another writer."""
    cmdline = cmdline or proc_cmdline
    epoch = epoch or instantiation_epoch
    task = show.get("task") or {}
    if task.get("id") != task_id:
        raise IdentityError(f"board show is for {task.get('id')}, not {task_id}")
    run_id = current_run(show).get("id")
    bound, owners = run_bindings(show, run_id)
    if not bound:
        raise IdentityError(f"run {run_id} of {task_id} has no worker_bound event (not a managed worker attempt)")
    pid, started = bound.get("pid"), bound.get("started_at")
    ep, sep, ticks = str(started or "").rpartition("|")
    if not isinstance(pid, int) or pid <= 1 or not sep or not ticks.isdigit():
        raise IdentityError(f"run {run_id} of {task_id}: worker_bound has no verifiable fingerprint ({started!r})")
    self_pid = self_pid or os.getpid()
    lineage = ancestors(self_pid)
    if pid not in lineage:
        raise IdentityError(f"no board-recorded bound worker of run {run_id} of {task_id} is an ancestor of this process")
    try:
        if proc_start_ticks(pid) != int(ticks):
            raise IdentityError(f"bound worker pid {pid} of run {run_id} has another start time (pid reused)")
        if ep and ep != epoch():
            raise IdentityError(f"bound worker pid {pid} of run {run_id} was recorded in another boot/instantiation")
        if not is_managed_worker(cmdline(pid)):
            raise IdentityError(f"bound pid {pid} of run {run_id} is not a per-attempt managed worker "
                                f"({MANAGED_WORKER_MODULE}); a shared owner process cannot attribute this shell to one card")
        chain = lineage[:lineage.index(pid)]
        nested = [p for p in chain if is_managed_worker(cmdline(p))]
    except ProcVisibilityError as exc:
        raise IdentityError(f"cannot inspect the bound worker chain: {exc}") from exc
    if pid in owners:
        raise IdentityError(f"bound pid {pid} of run {run_id} is the owner process that admitted it; ambiguous")
    if nested:
        raise IdentityError(f"another managed worker (pid {nested[0]}) sits between this process and pid {pid}; ambiguous")
    mine = set(chain) | {pid}
    for other in others:
        oid = (other.get("task") or {}).get("id")
        if oid != task_id and live_binding_pids(other) & mine:
            raise IdentityError(f"running card {oid} is bound to this process chain too; ambiguous, refusing")
    if explicit_pid is not None and int(explicit_pid) != pid:
        raise IdentityError(f"--owner-pid {explicit_pid} is not the bound worker pid {pid} of run {run_id}")
    return {"board": (os.environ.get("HERMES_KANBAN_BOARD") or "default").strip(), "task": task_id,
            "run_id": run_id, "pid": pid, "pid_start": int(ticks), "host": socket.gethostname()}


def self_chain(self_pid: int, anchors: Iterable[int]) -> set:
    """Ancestors of *self_pid* up to and including the nearest anchor. Without an anchor among
    the ancestors the chain is empty: a shared ancestor (gateway, terminal, init) is never
    part of "us", so a foreign worker's processes under it stay foreign."""
    anchors = set(anchors)
    chain = set()
    for pid in ancestors(self_pid):
        chain.add(pid)
        if pid in anchors:
            return chain
    return set()


# -------------------------------------------------------------- lease ---

def leases_dir() -> Path:
    return _private_dir(state_root() / "leases")


def lease_path(identity: dict) -> Path:
    return leases_dir() / f"{repo_key(identity)}.json"


@contextlib.contextmanager
def repo_lock(identity: dict, attempts: int = 5):
    """Per-repository advisory lock serializing acquire/renew/release/checkpoint. The lock
    file lives in the private leases directory, is opened without following symlinks and
    must still be the same inode after the flock is held (a replaced or unlinked lock file
    means another party raced the directory; retry on a fresh descriptor)."""
    directory = leases_dir()
    path = directory / f"{repo_key(identity)}.lock"
    for _ in range(attempts):
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LeaseError(f"{path} is a symlink; refusing to lock through it")
            raise
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
                raise LeaseError(f"{path} is not a private regular file; refusing to lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                now = os.lstat(path)
            except FileNotFoundError:
                continue  # unlinked while we waited: the lock we hold guards nothing
            if (now.st_ino, now.st_dev) != (opened.st_ino, opened.st_dev):
                continue  # replaced while we waited
            yield
            return
        finally:
            os.close(fd)  # releases the flock
    raise LeaseError(f"could not obtain a stable lock on {path}; retry")


def read_lease(identity: dict) -> Optional[dict]:
    data = _read_json_nofollow(lease_path(identity))
    return data or None


def same_owner(a: dict, b: dict) -> bool:
    """Same run of the same card on the same process incarnation. The run id alone never
    decides: host, pid and pid start time must match too (a copied run id in another process,
    or a reused pid, is not the owner)."""
    if not a or not b:
        return False
    for key in ("task", "board", "host", "pid", "pid_start"):
        if a.get(key) != b.get(key):
            return False
    if a.get("run_id") and b.get("run_id") and str(a["run_id"]) != str(b["run_id"]):
        return False
    return True


PID_MAX_LIMIT = 4194304  # Linux PID_MAX_LIMIT: no pid is ever larger


def _strict_int(value, low: int, high: Optional[int] = None) -> bool:
    return type(value) is int and value >= low and (high is None or value <= high)


def owner_alive(owner: dict) -> Optional[bool]:
    """True/False when decidable; None (undecidable: never recovered) when the recorded owner is
    malformed (pid not an int in 2..PID_MAX_LIMIT, bool/str/float pids, no host), on another host,
    not inspectable, or alive with a missing/malformed start time. False only when a well-formed
    pid on this host is gone, or alive with a well-formed start time of another incarnation."""
    pid, start, host = owner.get("pid"), owner.get("pid_start"), owner.get("host")
    if not _strict_int(pid, 2, PID_MAX_LIMIT) or not isinstance(host, str) or not host:
        return None
    if host != socket.gethostname():
        return None  # another host: not decidable here
    try:
        current = proc_start_ticks(pid)
    except ProcVisibilityError:
        return None
    if current is None:
        return False
    if not _strict_int(start, 0):
        return None
    return current == start


def _lease_payload(identity: dict, owner: dict, ttl: int, now: float, acquired: float) -> dict:
    return {"version": LEASE_VERSION, "root": identity["root"], "gitdir": identity["gitdir"],
            "remote": identity.get("remote", ""), "branch": identity.get("branch", ""),
            "owner": owner, "acquired_at": acquired, "renewed_at": now, "ttl": int(ttl)}


def _exclusive_create(directory: Path, path: Path, payload: dict) -> bool:
    tmp = _write_private_tmp(directory, payload)
    try:
        os.link(tmp, path)  # exclusive create: fails when any lease exists
        return True
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)


def acquire_lease(identity: dict, owner: dict, ttl: int = DEFAULT_TTL, now: Optional[float] = None,
                  renew_only: bool = False) -> dict:
    """Acquire or renew the working-tree lease under the per-repository lock. Returns
    {"status": acquired|renewed|recovered|conflict|none, "lease": <current lease>, "reason": str}.
    Never removes a live owner's lease. With *renew_only* nothing is ever taken: the result is
    "renewed" when this owner already holds the lease, else "none"/"conflict"."""
    now = time.time() if now is None else now
    directory = leases_dir()
    path = directory / f"{repo_key(identity)}.json"
    fresh = _lease_payload(identity, owner, ttl, now, now)
    with repo_lock(identity):
        current = _read_json_nofollow(path)
        if current is None:
            if renew_only:
                return {"status": "none", "lease": {}, "reason": "no lease held by this run"}
            if _exclusive_create(directory, path, fresh):
                return {"status": "acquired", "lease": fresh, "reason": "no previous lease"}
            current = _read_json_nofollow(path) or {}
        holder = current.get("owner") or {}
        if same_owner(holder, owner):
            renewed = _lease_payload(identity, owner, ttl, now, float(current.get("acquired_at") or now))
            _atomic_replace(directory, path.name, renewed)
            return {"status": "renewed", "lease": renewed, "reason": "same task, run and process"}
        if renew_only:
            return {"status": "conflict", "lease": current,
                    "reason": f"lease held by task {holder.get('task')} run {holder.get('run_id')} (pid {holder.get('pid')})"}
        alive = owner_alive(holder)
        age = now - float(current.get("renewed_at") or 0)
        expired = age > int(current.get("ttl") or DEFAULT_TTL)
        if alive is None:
            return {"status": "conflict", "lease": current,
                    "reason": f"lease held by {holder.get('task')} run {holder.get('run_id')} on {holder.get('host')}: "
                              "owner liveness not decidable from this host"}
        if alive:
            state = "stale heartbeat, process alive" if expired else "active"
            return {"status": "conflict", "lease": current,
                    "reason": f"lease held by task {holder.get('task')} run {holder.get('run_id')} "
                              f"(pid {holder.get('pid')}, {state}, renewed {int(age)}s ago); an alive owner is never "
                              "taken over automatically"}
        # Owner process is gone (exited, or its pid was reused by another process): bounded recovery,
        # serialized by the repository lock so exactly one recoverer replaces the stale lease.
        _atomic_replace(directory, path.name, fresh)
        return {"status": "recovered", "lease": fresh,
                "reason": f"previous owner task {holder.get('task')} run {holder.get('run_id')} "
                          f"(pid {holder.get('pid')}) is gone"}


def release_lease(identity: dict, owner: dict) -> dict:
    path = lease_path(identity)
    with repo_lock(identity):
        current = _read_json_nofollow(path)
        if not current:
            return {"status": "none", "reason": "no lease"}
        if not same_owner(current.get("owner") or {}, owner):
            return {"status": "conflict", "lease": current, "reason": "lease is owned by another task/run/process; not released"}
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return {"status": "released", "lease": current, "reason": "owner released"}


# ---------------------------------------------------------- scanners ---

def scanner_registry_path() -> Path:
    return state_root() / "cwd-scanners.json"


def scanner_registry() -> dict:
    data = _read_json_nofollow(scanner_registry_path()) or {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v)}


def validate_scanner_argv(argv: list) -> list:
    """`sudo -n /abs/helper` or `/abs/helper`: the helper must exist, be owned by root and be
    writable by nobody else, so the registration cannot promote a user-writable file to a
    privileged observer. No arguments to the helper (the approved sudoers entry takes none)."""
    if not argv:
        raise LeaseError("scanner argv is empty")
    if argv[0] == "sudo":
        if len(argv) != 3 or argv[1] != "-n":
            raise LeaseError("a privileged scanner must be exactly: sudo -n /absolute/helper")
        helper = argv[2]
    elif len(argv) == 1:
        helper = argv[0]
    else:
        raise LeaseError("a scanner takes no arguments: register `/absolute/helper` or `sudo -n /absolute/helper`")
    _root_owned_executable(helper, "scanner helper")
    return list(argv)


def _root_owned_executable(path: str, what: str) -> str:
    if not path.startswith("/") or path != os.path.normpath(path):
        raise LeaseError(f"{what} '{path}' must be a normalized absolute path")
    try:
        st = os.stat(path)
    except OSError as exc:
        raise LeaseError(f"{what} '{path}' is not accessible: {exc.strerror}")
    if not stat.S_ISREG(st.st_mode) or not (st.st_mode & stat.S_IXUSR):
        raise LeaseError(f"{what} '{path}' is not an executable regular file")
    if st.st_uid != 0 or (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise LeaseError(f"{what} '{path}' must be owned by root and not group/world writable")
    return path


def declared_repository(text: str) -> Optional[str]:
    """The checkout a helper's source binds it to: exactly one assignment to REPOSITORY in the
    whole file, at module level, of a normalized absolute string literal (the approved helper's
    `REPOSITORY = "/opt/repos/validate"`). Anything else (no declaration, two, a computed value,
    a nested or augmented assignment, not Python) is None: no binding."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    stores = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "REPOSITORY"
              and isinstance(n.ctx, (ast.Store, ast.Del))]
    globals_ = [n for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal)) and "REPOSITORY" in n.names]
    top = [n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
           and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "REPOSITORY"]
    if len(stores) != 1 or globals_ or len(top) != 1:
        return None
    value = top[0].value
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    root = value.value
    return root if root.startswith("/") and os.path.normpath(root) == root else None


def helper_repository(helper: str) -> Optional[str]:
    """The repository a root-owned helper declares it scans (declared_repository of its source);
    None when it declares none. The helper's output names no root, so its PASS is about this
    declaration and nothing else. Root wrote the file; this module never infers a binding."""
    try:
        _root_owned_executable(helper, "scanner helper")
        with open(helper, "rb") as fh:
            data = fh.read(256 * 1024 + 1)
    except (OSError, LeaseError):
        return None
    if len(data) > 256 * 1024:
        return None
    try:
        return declared_repository(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def scanner_root_mismatch(argv: list, root: str) -> Optional[str]:
    """None when the registered helper is bound to exactly *root*; otherwise why its result cannot
    speak for *root*. A helper with no verifiable binding needs a root-owned helper (or binding)
    for this checkout; that is a privileged change this module does not make."""
    helper = argv[-1]
    bound = helper_repository(helper)
    if bound is None:
        return (f"scanner helper {helper} declares no repository it is bound to; a root-installed helper "
                f"bound to {root} is needed")
    if bound != root:
        return f"scanner helper {helper} scans {bound}, not {root}"
    return None


def trusted_sudo() -> str:
    """The system sudo from TRUSTED_PATH (root-owned, not group/world writable), never one found
    on the caller's PATH, which a same-uid process could shadow with a fake that prints PASS."""
    for d in TRUSTED_PATH.split(":"):
        cand = os.path.join(d, "sudo")
        if os.path.isfile(cand):
            return _root_owned_executable(os.path.realpath(cand), "sudo")
    raise LeaseError(f"no sudo in {TRUSTED_PATH}")


def register_scanner(checkout: str, argv: list, git: GitRunner = default_git) -> dict:
    identity = repo_identity(checkout, git)
    argv = validate_scanner_argv(argv)
    mismatch = scanner_root_mismatch(argv, identity["root"])
    if mismatch:
        raise LeaseError(mismatch)
    registry = scanner_registry()
    registry[identity["root"]] = argv
    _private_dir(state_root())
    _atomic_replace(state_root(), scanner_registry_path().name, registry)
    return {"checkout": identity["root"], "scanner": argv}


def default_scan_runner(argv: list) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k in ("LANG", "LC_ALL", "HOME", "USER")}
    env["PATH"] = TRUSTED_PATH
    argv = list(argv)
    if argv and argv[0] == "sudo":
        argv[0] = trusted_sudo()
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env,
                          cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL)


def parse_scan_output(returncode: int, stdout: str, stderr: str = "") -> dict:
    """The registered helper's exact protocol; anything else is incomplete (never a clean scan):

      exit 0  exactly `PASS: Inspected N process directories.` (N >= 1) and
              `PASS: No repository CWD matches found.`, nothing else       -> complete, no matches
      exit 1  one or more `MATCH: PID=<n> CWD=/abs` lines, then exactly
              `BLOCKED: <count of them> matching processes.`, nothing else  -> complete, matches
      any stderr (a sudo refusal, a warning), any other exit code, a missing, extra or malformed
      line                                                                  -> incomplete

    `sudo -n` without a matching rule exits 1 with only stderr: that is a failed scan, not "no
    matches"."""
    lines = (stdout or "").splitlines()
    matches, problem = [], None
    if (stderr or "").strip():
        problem = "scanner wrote to stderr"
    elif returncode == 0:
        if len(lines) != 2 or not PASS_INSPECTED_RE.match(lines[0]) or lines[1] != PASS_NO_MATCH:
            problem = "exit 0 without exactly the two PASS markers"
    elif returncode == 1:
        blocked = BLOCKED_RE.match(lines[-1]) if lines else None
        for line in lines[:-1]:
            m = MATCH_RE.match(line)
            if not m or not m.group(2).startswith("/"):
                problem = f"malformed scanner line: {line[:80]!r}"
                break
            matches.append({"pid": int(m.group(1)), "cwd": m.group(2)})
        if problem is None and (not blocked or int(blocked.group(1)) != len(matches)):
            problem = "exit 1 without a BLOCKED count equal to the MATCH lines"
    else:
        problem = f"scanner exit {returncode}"
    tail = "\n".join(lines[-3:] + (stderr or "").splitlines()[-3:])[:600]
    if problem:
        return {"status": "incomplete", "matches": matches, "returncode": returncode,
                "tail": f"{problem}; {tail}"[:600]}
    return {"status": "complete", "matches": matches, "returncode": returncode, "tail": tail}


def fallback_scan(root: str) -> dict:
    """Unprivileged /proc scan for processes whose CWD is inside *root*. Processes of other
    users are unreadable here; each such process makes the result incomplete (unknown)."""
    matches, unreadable, inspected = [], 0, 0
    try:
        names = os.listdir("/proc")
    except OSError as exc:
        return {"status": "incomplete", "matches": [], "returncode": 2, "tail": f"cannot list /proc: {exc}"}
    for name in names:
        if not name.isdigit():
            continue
        try:
            cwd = os.readlink(f"/proc/{name}/cwd")
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESRCH, errno.ENOTDIR):
                continue
            if exc.errno in (errno.EACCES, errno.EPERM):
                unreadable += 1
                continue
            raise
        inspected += 1
        if cwd == root or cwd.startswith(root + "/"):
            matches.append({"pid": int(name), "cwd": cwd})
    status = "incomplete" if unreadable else "complete"
    return {"status": status, "matches": matches, "returncode": 2 if unreadable else (1 if matches else 0),
            "tail": f"inspected {inspected}, unreadable {unreadable}"}


def run_scan(root: str, runner: ScanRunner = default_scan_runner, registry: Optional[dict] = None) -> dict:
    registry = scanner_registry() if registry is None else registry
    argv = registry.get(root)
    if not argv:
        out = fallback_scan(root)
        out["scanner"] = "fallback:/proc"
        return out
    label = " ".join(shlex.quote(str(a)) for a in argv)
    try:
        # Re-checked at every execution: a registry edited after register-scanner (a user-owned
        # helper, arguments, no -n) is never run.
        validate_scanner_argv(list(argv))
        mismatch = scanner_root_mismatch(argv, root)
        if mismatch:  # a clean PASS about another tree is not a scan of this checkout
            raise LeaseError(mismatch)
        r = runner(argv)
    except (OSError, subprocess.SubprocessError, LeaseError) as exc:
        return {"status": "incomplete", "matches": [], "returncode": 2, "tail": str(exc)[:300], "scanner": label}
    out = parse_scan_output(r.returncode, r.stdout or "", r.stderr or "")
    outside = [m for m in out["matches"] if m["cwd"] != root and not m["cwd"].startswith(root + "/")]
    if outside:  # the helper scans some other tree than this checkout
        out.update(status="incomplete", tail=f"match outside {root}: {outside[0]['cwd'][:200]}")
    out["scanner"] = label
    return out


def _is_agent(cmd: list) -> bool:
    if not cmd:
        return False
    names = {Path(part).name for part in cmd[:3]}
    if names & AGENT_EXECUTABLES:
        return True
    joined = " ".join(cmd)
    return any(hint in joined for hint in AGENT_CMDLINE_HINTS)


_GIT_VALUE_OPTIONS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix",
                      "--config-env"}


def _git_subcommand(cmd: list) -> Optional[str]:
    """The git subcommand of a git invocation (None when *cmd* is not git or has none)."""
    if not cmd or Path(cmd[0]).name != "git":
        return None
    parts = iter(cmd[1:])
    for part in parts:
        if part in _GIT_VALUE_OPTIONS:
            next(parts, None)  # the option's value is not the subcommand
            continue
        if part.startswith("-"):
            continue
        return part
    return None


def _git_mutation(cmd: list) -> Optional[str]:
    """The git subcommand when *cmd* is a git invocation of a tree/ref-mutating subcommand."""
    sub = _git_subcommand(cmd)
    return sub if sub in MUTATING_GIT else None


def program_name(cmd: list) -> str:
    """The tool a command line stands for: the executable's basename, or for a script
    interpreter (node, python, ...) the basename of the script it runs without its extension,
    so `node .../node_modules/vite/bin/vite.js` is `vite`."""
    if not cmd:
        return ""
    name = Path(cmd[0]).name.lstrip("-")
    if name in SCRIPT_INTERPRETERS or re.match(r"^python3\.\d+$", name):
        for part in cmd[1:]:
            if part.startswith("-"):
                if part in ("-c", "-e", "-m", "--eval", "-p", "--print"):
                    return f"{name} {part}"  # inline code: not a known script
                continue
            script = Path(part).name
            return re.sub(r"\.(m?js|cjs|py|rb|pl|ts)$", "", script)
    return name


# Arguments that turn a passive name into a writer or a command runner. vi-family short options are
# letter clusters (vim parses `-Rc` as -R -c): -e/-E/-s ex/silent/script modes, -c/-S/-u run
# commands or files, -w/-W record keystrokes, -l (nvim) runs Lua. `+N` and `+/pattern` only move
# the cursor; any other `+cmd` is an Ex command.
VIM_FAMILY = {"vim", "nvim", "vi", "view"}
VIM_REFUSED_SHORT = set("eEsScuwWl")
VIM_REFUSED_LONG = {"--headless", "--cmd", "--remote-send", "--remote-expr"}
EMACS_REFUSED = {"--batch", "-batch", "--script", "-script", "--eval", "-eval", "--execute", "-execute",
                 "-l", "--load", "-load", "-f", "--funcall", "-funcall", "-x"}
EMACSCLIENT_REFUSED = {"-e", "--eval"}
KAK_REFUSED = {"-f", "-e", "-p"}
def _git_split(cmd: list) -> tuple:
    """(global options, subcommand, subcommand arguments) of a git command line."""
    globals_, i = [], 1
    while i < len(cmd):
        part = cmd[i]
        if part in _GIT_VALUE_OPTIONS:
            globals_ += cmd[i:i + 2]
            i += 2
            continue
        if part.startswith("-"):
            globals_.append(part)
            i += 1
            continue
        return globals_, part, cmd[i + 1:]
    return globals_, None, []


def _options_until_dashdash(args: list) -> list:
    return args[:args.index("--")] if "--" in args else args


def _git_safe_read(cmd: list) -> Optional[str]:
    """None when *cmd* is one of the explicit safe read forms (GIT_SAFE_GLOBALS, GIT_SAFE_READS);
    otherwise why not. Exact tokens only: git's parse-options accepts any unique prefix of a long
    option, so a token is never matched by prefix of a dangerous name, only listed exactly."""
    globals_, sub, rest = _git_split(cmd)
    i = 0
    while i < len(globals_):
        g = globals_[i]
        if g == "-C" and i + 1 < len(globals_):
            i += 2
            continue
        if g not in GIT_SAFE_GLOBALS:
            return f"global option {g.split('=', 1)[0][:40]} is not in the safe read forms"
        i += 1
    if sub not in GIT_SAFE_READS:
        return f"git {sub or '?'}: not a safe read form (may run filters, hooks, pagers or write)"
    flags, prefixes = GIT_SAFE_READS[sub]
    opts = _options_until_dashdash(rest)
    for a in opts:
        if a.startswith("-") and a != "-" and a not in flags and not any(a.startswith(p) for p in prefixes):
            return f"git {sub} {a[:40]}: not a listed option of the safe read form"
    if sub == "symbolic-ref" and len([a for a in rest if not a.startswith("-")]) != 1:
        return "git symbolic-ref with other than one name updates a ref"
    return None


def _passive_refusal(name: str, cmd: list) -> Optional[str]:
    """Why a passive tool's arguments make this invocation a possible writer; None when plain."""
    args = cmd[1:] if Path(cmd[0]).name.lstrip("-") == name else cmd[2:]
    opts = _options_until_dashdash(args)
    if name in VIM_FAMILY:
        for a in opts:
            if a.startswith("--"):
                if a.split("=", 1)[0] in VIM_REFUSED_LONG:
                    return f"{name} {a.split('=', 1)[0]} runs commands"
            elif a.startswith("-") and VIM_REFUSED_SHORT & set(a[1:]):
                return f"{name} {a} runs commands, scripts or batch mode"
            elif a.startswith("+") and not re.fullmatch(r"\+(?:\d*|/.*)", a):
                return f"{name} {a[:40]} runs an Ex command"
    if name == "emacs" and any(a.split("=", 1)[0] in EMACS_REFUSED for a in opts):
        return "emacs batch/eval/load runs Lisp"
    if name == "emacsclient" and any(a.split("=", 1)[0] in EMACSCLIENT_REFUSED for a in opts):
        return "emacsclient --eval runs Lisp"
    if name == "kak" and any(a in KAK_REFUSED for a in opts):
        return "kak -f/-e/-p runs keys or commands"
    return None


def classify_cmdline(cmd: list) -> tuple:
    """(kind, blocking, detail) for a command line of a process outside our chain, from the
    explicit sets above. Anything not listed is unknown and blocks (fail closed). A passive name
    whose arguments make it a writer or command runner (_passive_refusal, _git_readonly_refusal)
    blocks too. This is the command line only; classify_process also checks /proc/<pid>/exe."""
    if not cmd:
        return "unknown", True, "empty command line"
    sub = _git_subcommand(cmd)
    if Path(cmd[0]).name == "git":
        if sub in MUTATING_GIT:
            return "git-mutation", True, f"git {sub} in progress"
        why = _git_safe_read(cmd)
        if why:
            return "unknown", True, f"{why}; treated as a possible writer"
        return "git-readonly", False, f"read-only git {sub}"
    if _is_agent(cmd):
        return "agent-writer", True, f"autonomous agent outside this chain: {Path(cmd[0]).name}"
    name = program_name(cmd)
    if _interpreter_family(Path(cmd[0]).name.lstrip("-")):
        return "unknown", True, (f"{Path(cmd[0]).name} running {name or '?'}: a script is whatever the file says, "
                                 "not the tool it is named after; treated as a possible writer")
    if name in PASSIVE_LANGUAGE_SERVERS:
        return "unknown", True, (f"language server ({name}) outside a verified VS Code extension host: by name "
                                 "or image name it is unverifiable; treated as a possible writer")
    if name in PASSIVE_DEV_SERVERS:
        return "unknown", True, f"dev server / build tool ({name}) writes build output or runs commands"
    refusal = _passive_refusal(name, cmd) if name in PASSIVE_EDITORS else None
    if refusal:
        return "unknown", True, f"{refusal}; treated as a possible writer"
    if name in PASSIVE_EDITORS:
        return "editor", False, f"editor ({name}); a writer only on operator action, not by CWD alone"
    if name in INTERACTIVE_SHELLS:
        args = cmd[1:]
        if all(a in IDLE_SHELL_OPTIONS for a in args):
            return "unknown", True, f"interactive shell ({name}): {SHELL_REMEDY}"
        return "unknown", True, f"shell with arguments ({name} {' '.join(args)[:80]}): {SHELL_REMEDY}"
    return "unknown", True, f"{name or '?'}: not a known passive tool; treated as a possible writer"


def _long_abbrev(token: str, *names: str) -> bool:
    """Whether *token* (`--x` or `--x=v`) is a long option git would read as one of *names*
    (any prefix: git accepts unique abbreviations; an ambiguous one is refused by git anyway)."""
    if not token.startswith("--") or token == "--":
        return False
    opt = token[2:].split("=", 1)[0]
    return bool(opt) and any(n.startswith(opt) for n in names)


def git_config_refusal(key: str, value: Optional[str]) -> Optional[str]:
    """None when command-line configuration *key*=*value* only changes display; otherwise why not."""
    k, v = key.lower(), (value or "").lower()
    if k in UNGRANTED_GIT_CONFIG_KEYS or k.startswith(UNGRANTED_GIT_CONFIG_PREFIXES):
        return None
    if k == "core.fsmonitor" and value is not None and v in ("false", "no", "off", "0", ""):
        return None
    if re.fullmatch(r"filter\..+\.(?:clean|smudge|process)", k) and value == "":
        return None
    return f"git command-line configuration {key[:60]} can run a program or change what git writes"


def _ungranted_git_refusal(cmd: list) -> Optional[str]:
    """Why a READONLY_GIT command line can write or run an invoker-chosen program; None when plain."""
    globals_, sub, rest = _git_split(cmd)
    i = 0
    while i < len(globals_):
        g = globals_[i]
        if g == "-c" and i + 1 < len(globals_):
            key, eq, value = globals_[i + 1].partition("=")
            why = git_config_refusal(key, value if eq else None)
            if why:
                return why
            i += 2
            continue
        if g.startswith("--config-env") or g.startswith("--exec-path="):
            return f"git {g.split('=', 1)[0]} runs a command-line chosen program"
        i += 1 if g not in _GIT_VALUE_OPTIONS else 2
    opts = _options_until_dashdash(rest)
    positionals = [a for a in opts if not a.startswith("-")]
    if any(_long_abbrev(a, "output") for a in opts):
        return f"git {sub} --output writes a file"
    if sub == "grep":
        for a in opts:
            if _long_abbrev(a, "open-files-in-pager"):
                return "git grep --open-files-in-pager runs a program"
            if a.startswith("-") and not a.startswith("--"):
                for ch in a[1:]:
                    if ch == "O":
                        return "git grep -O runs a program"
                    if ch in GREP_SHORT_VALUE:
                        break
    if sub == "fsck" and any(_long_abbrev(a, "lost-found") for a in opts):
        return "git fsck --lost-found writes into the git directory"
    if sub == "reflog" and positionals[:1] and positionals[0] in REFLOG_WRITES:
        return f"git reflog {positionals[0]} rewrites reflogs"
    if sub == "symbolic-ref" and (len(positionals) != 1 or any(a in ("-d", "-m") or _long_abbrev(a, "delete")
                                                               for a in opts)):
        return "git symbolic-ref with other than one name (or -d/-m) updates a ref"
    if sub == "ls-remote" and any(_long_abbrev(a, "upload-pack") for a in opts):
        return "git ls-remote --upload-pack runs a program"
    return None


def _script_index(cmd: list) -> Optional[int]:
    """Index of the script an interpreter command line runs (as program_name reads it)."""
    for i, part in enumerate(cmd[1:], 1):
        if part.startswith("-"):
            if part in ("-c", "-e", "-m", "--eval", "-p", "--print"):
                return None
            continue
        return i
    return None


def _tool_args(cmd: list) -> list:
    """The tool's own arguments: after the script for an interpreter command line."""
    name = Path(cmd[0]).name.lstrip("-")
    if name in SCRIPT_INTERPRETERS or re.match(r"^python3\.\d+$", name):
        i = _script_index(cmd)
        return cmd[i + 1:] if i is not None else []
    return cmd[1:]


def _dev_server_refusal(name: str, args: list) -> Optional[str]:
    """Why a dev server / watcher invocation is a one-shot build (writes output); None when serve/watch."""
    opts = _options_until_dashdash(args)
    positionals = [a for a in opts if not a.startswith("-")]
    writes = DEV_SERVER_WRITE_COMMANDS.get(name, set()) & set(positionals)
    if writes:
        return f"{name} {sorted(writes)[0]} is a build that writes output"
    if name in DEV_SERVER_WATCH_FLAGS:
        flags = DEV_SERVER_WATCH_FLAGS[name]
        watching = any(a.lower().split("=", 1)[0] in flags for a in opts)
        serving = bool(DEV_SERVER_SERVE_POSITIONALS.get(name, set()) & set(positionals[:1]))
        if not (watching or serving):
            return f"{name} without watch/serve mode is a build that writes output"
    if name == "turbo":
        tasks = [a for a in positionals if a not in ("run", "watch")]
        if not tasks or not set(tasks) <= TURBO_WATCH_TASKS:
            return "turbo runs package tasks other than dev/start/serve"
    return None


def classify_cmdline_ungranted(cmd: list) -> tuple:
    """The pre-PR28 (d468701) classification, used for a checkout no valid observer policy grants:
    agents and git mutations block, the listed read-only git, editors, language servers, dev
    servers/watchers and idle shells are observations, anything else is unknown and blocks. A
    read-only git form that writes or runs an invoker-chosen program (_ungranted_git_refusal) and
    a dev server in build mode (_dev_server_refusal) block; classify_process also checks that
    /proc/<pid>/exe backs the name (ungranted_image_mismatch, git_environ_refusal)."""
    if not cmd:
        return "unknown", True, "empty command line"
    sub = _git_subcommand(cmd)
    if Path(cmd[0]).name == "git":
        if sub in MUTATING_GIT:
            return "git-mutation", True, f"git {sub} in progress"
        if sub in READONLY_GIT:
            why = _ungranted_git_refusal(cmd)
            if why:
                return "unknown", True, f"{why}; treated as a possible writer"
            return "git-readonly", False, f"read-only git {sub}"
        return "unknown", True, f"git {sub or '?'}: not a known read-only subcommand"
    if _is_agent(cmd):
        return "agent-writer", True, f"autonomous agent outside this chain: {Path(cmd[0]).name}"
    name = program_name(cmd)
    if name in PASSIVE_EDITORS:
        return "editor", False, f"editor ({name}); a writer only on operator action, not by CWD alone"
    if name in PASSIVE_LANGUAGE_SERVERS:
        return "language-server", False, f"language server ({name})"
    if name in PASSIVE_DEV_SERVERS:
        why = _dev_server_refusal(name, _tool_args(cmd))
        if why:
            return "unknown", True, f"{why}; treated as a possible writer"
        return "dev-server", False, f"dev server / watcher ({name})"
    if name in INTERACTIVE_SHELLS:
        args = cmd[1:]
        if all(a in IDLE_SHELL_OPTIONS for a in args):
            return "shell", False, f"idle interactive shell ({name})"
        return "unknown", True, f"shell with arguments ({name} {' '.join(args)[:80]}): not provably idle"
    return "unknown", True, f"{name or '?'}: not a known passive tool; treated as a possible writer"


# An interactive shell is never passive. Its prompt is not observable from outside: a shell at
# its prompt can still run code on its own (trap handlers on a signal, PROMPT_COMMAND, TMOUT) and
# `read -t N; echo >> file` sleeps in the same tty wait, with no child and no CPU or
# context-switch change for N seconds, as a prompt does. /proc samples cannot tell them apart, so
# no sampled state is taken as proof. A shell whose CWD is the checkout blocks with this remedy.
SHELL_REMEDY = ("an interactive shell can run code at any time (traps, PROMPT_COMMAND, `read -t` then a "
                "write) and /proc cannot prove it will not; close that shell or cd out of the checkout "
                "(editors and verified language servers may stay open)")


def _vscode_root(path: str) -> Optional[str]:
    if not path.startswith("/") or os.path.normpath(path) != path:
        return None
    m = VSCODE_SERVER_RE.match(path)
    return m.group(1) if m else None


def _vscode_host(pid: Optional[int], root: str, kind: str) -> Optional[str]:
    """None when *pid* is this server's `node [-opts] out/bootstrap-fork --type=<kind>` running the
    server's own node binary; otherwise why not."""
    if not pid or pid <= 1:
        return "no parent"
    cmd = proc_cmdline(pid)
    if not cmd:
        return "parent is gone"
    rest = cmd[1:]
    while rest and rest[0].startswith("-"):
        rest = rest[1:]
    if cmd[0] != f"{root}/node" or rest[:2] != [f"{root}/out/bootstrap-fork", f"--type={kind}"]:
        return f"parent {pid} is not this server's {kind}"
    if proc_exe(pid) != os.path.realpath(f"{root}/node"):
        return f"parent {pid} does not run the server's node binary"
    return None


def classify_vscode(pid: int, cmd: list, grant: Optional[dict] = None) -> Optional[tuple]:
    """(kind, blocking, detail) when *cmd* claims to be a VS Code Remote language server or
    integrated-terminal bash; None when it makes no such claim. A claim is accepted only when
    /proc backs it: exact bundled entry script run by the server's own node binary, parented by
    that server's extension host which is also its --clientProcessId; or bash with the server's
    exact shell-integration init file parented by that server's pty host is identified as the
    integrated terminal but still blocks (SHELL_REMEDY), unless a granted observer policy
    (observer_grant) applies and trusted_terminal_refusal proves the full chain. Anything else: unknown."""
    if len(cmd) >= 2 and Path(cmd[0]).name == "node" and _vscode_root(cmd[0]) and cmd[0] == f"{_vscode_root(cmd[0])}/node":
        root = _vscode_root(cmd[0])
        rel = cmd[1][len(root) + 1:] if cmd[1].startswith(root + "/") else ""
        if not rel:
            return None
        claim = f"VS Code language server {rel.split('/')[1] if '/' in rel else rel}"
        if os.path.normpath(cmd[1]) != cmd[1] or re.sub(r"\.js$", "", rel) not in VSCODE_LANGUAGE_SERVERS:
            return "unknown", True, f"{claim}: not a bundled language server entry point"
        args = cmd[2:]
        client = [a for a in args if a.startswith("--clientProcessId=")]
        if sorted(a for a in args if a not in client) != ["--node-ipc"] or len(client) != 1:
            return "unknown", True, f"{claim}: unexpected arguments"
        if proc_exe(pid) != os.path.realpath(f"{root}/node"):
            return "unknown", True, f"{claim}: not running the server's node binary"
        parent = proc_parent(pid)
        if client[0] != f"--clientProcessId={parent}":
            return "unknown", True, f"{claim}: client process is not its parent {parent}"
        why = _vscode_host(parent, root, "extensionHost")
        if why:
            return "unknown", True, f"{claim}: {why}"
        return "language-server", False, f"{claim} (extension host {parent})"
    if len(cmd) == 3 and Path(cmd[0]).name == "bash" and cmd[1] == "--init-file" \
            and _vscode_root(cmd[2]) and cmd[2] == f"{_vscode_root(cmd[2])}/{VSCODE_BASH_INTEGRATION}":
        root = _vscode_root(cmd[2])
        claim = "VS Code integrated terminal (bash)"
        exe = proc_exe(pid)
        if not exe or Path(exe).name != "bash" or exe != os.path.realpath(cmd[0]):
            return "unknown", True, f"{claim}: not running {cmd[0]}"
        parent = proc_parent(pid)
        why = _vscode_host(parent, root, "ptyHost")
        if why:
            return "unknown", True, f"{claim}: {why}; {SHELL_REMEDY}"
        if grant and grant.get("status") == "granted":
            why = trusted_terminal_refusal(pid, root, parent, grant)
            if why is None:
                return "trusted-terminal", False, (
                    f"{claim} under pty host {parent}: operator-accepted interactive terminal on this host and "
                    f"repository (observer policy {grant['policy_sha256'][:12]}); its commands are classified on "
                    "their own, content drift is still checked")
            return "unknown", True, f"{claim} under pty host {parent}: observer policy not applied ({why}); {SHELL_REMEDY}"
        return "unknown", True, f"{claim} under pty host {parent}: {SHELL_REMEDY}"
    return None


# ------------------------------------------------ trusted observer policy ---
#
# The operator may accept the risk of their OWN VS Code integrated terminal (bash) sitting in one
# designated checkout on one host: a native, private policy file next to the run state (never a
# scanner bypass; the privileged scan still runs and every other match is classified as before).
# It names this host (machine-id + hostname), the uid, the VS Code server directory and the exact
# repositories (root, gitdir, remote). Absent, malformed, foreign-host or foreign-repository
# policies change nothing: the terminal blocks as before. Even under a valid policy the terminal
# passes only when /proc proves the whole VS Code chain (trusted_terminal_refusal); its child
# processes (git commit, an agent, a script) are separate matches and still block, and dirty-state
# provenance, lease and owner checks are unchanged. This module never writes the policy.
OBSERVER_POLICY_VERSION = 1
OBSERVER_POLICY_KIND = "trusted-vscode-terminal"
OBSERVER_POLICY_FIELDS = ("version", "kind", "host", "uid", "vscode_server_dir", "repositories", "accepted_risk")
OBSERVER_POLICY_OPTIONAL = ("note", "created_at")
OBSERVER_HOST_FIELDS = ("machine_id", "hostname")
OBSERVER_REPO_FIELDS = ("root", "gitdir", "remote")
MACHINE_ID_PATH = "/etc/machine-id"
PTS_MAJORS = range(136, 144)  # UNIX98 pty slaves


def observer_policy_path() -> Path:
    return state_root() / "observer-policy.json"


def host_identity() -> dict:
    try:
        machine_id = Path(MACHINE_ID_PATH).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        machine_id = ""
    return {"machine_id": machine_id, "hostname": socket.gethostname()}


def _owner_protected(st: os.stat_result, uid: int, allow_root: bool = False) -> bool:
    """Owned by *uid* (or root when allowed), not world-writable, group-writable only for the
    owner's own primary group (a user-private group, as with umask 002)."""
    if st.st_uid != uid and not (allow_root and st.st_uid == 0):
        return False
    if st.st_mode & stat.S_IWOTH:
        return False
    return not (st.st_mode & stat.S_IWGRP) or st.st_gid == os.getgid()


def load_observer_policy(path: Optional[Path] = None) -> tuple:
    """(policy, problem). (None, None) when there is no policy; (None, why) when one exists but is
    not a private, regular, strictly well-formed file owned by this uid."""
    path = Path(path or observer_policy_path())
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{path} is not readable as a regular file ({exc.strerror})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return None, f"{path} must be a regular file owned by uid {os.getuid()} with mode 0600"
        raw = os.read(fd, 1 << 16)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, f"{path} is not JSON"
    if not isinstance(data, dict):
        return None, f"{path} is not a JSON object"
    missing = [k for k in OBSERVER_POLICY_FIELDS if k not in data]
    extra = sorted(set(data) - set(OBSERVER_POLICY_FIELDS) - set(OBSERVER_POLICY_OPTIONAL))
    if missing or extra:
        return None, f"observer policy fields: missing {missing or 'none'}, unknown {extra or 'none'}"
    host, repos, sdir = data["host"], data["repositories"], data["vscode_server_dir"]
    if data["version"] != OBSERVER_POLICY_VERSION or data["kind"] != OBSERVER_POLICY_KIND:
        return None, f"observer policy must be version {OBSERVER_POLICY_VERSION}, kind {OBSERVER_POLICY_KIND}"
    if not isinstance(host, dict) or sorted(host) != sorted(OBSERVER_HOST_FIELDS) \
            or not all(isinstance(host[k], str) and host[k] for k in OBSERVER_HOST_FIELDS):
        return None, "observer policy host must be exactly {machine_id, hostname} (non-empty strings)"
    if not _strict_int(data["uid"], 0, 2 ** 32 - 1):
        return None, "observer policy uid must be an integer"
    if not isinstance(sdir, str) or not sdir.startswith("/") or os.path.normpath(sdir) != sdir or sdir == "/":
        return None, "observer policy vscode_server_dir must be a normalized absolute directory"
    if not isinstance(repos, list) or not repos or not all(
            isinstance(r, dict) and sorted(r) == sorted(OBSERVER_REPO_FIELDS)
            and all(isinstance(r[k], str) and r[k] for k in OBSERVER_REPO_FIELDS) for r in repos):
        return None, "observer policy repositories must be a non-empty list of exactly {root, gitdir, remote}"
    if not isinstance(data["accepted_risk"], str) or len(data["accepted_risk"].strip()) < 20 \
            or data["accepted_risk"].lstrip().startswith("<"):
        return None, "observer policy accepted_risk must state the accepted risk"
    return data | {"_sha256": hashlib.sha256(raw).hexdigest()}, None


def observer_grant(identity: dict, path: Optional[Path] = None) -> dict:
    """What the observer policy allows for this checkout on this host: status "none" (no policy),
    "refused" (a policy that does not apply here, with why) or "granted"."""
    policy, problem = load_observer_policy(path)
    if problem:
        return {"status": "refused", "detail": problem}
    if policy is None:
        return {"status": "none", "detail": "no observer policy"}
    out = {"policy_sha256": policy["_sha256"]}
    here = host_identity()
    if not here["machine_id"] or policy["host"] != here:
        return out | {"status": "refused", "detail": f"policy is for host {policy['host']}, this is {here}"}
    if policy["uid"] != os.getuid():
        return out | {"status": "refused", "detail": f"policy is for uid {policy['uid']}, this is {os.getuid()}"}
    for repo in policy["repositories"]:
        if repo["root"] == identity["root"] and repo["gitdir"] == identity["gitdir"] \
                and same_remote(repo["remote"], identity.get("remote") or ""):
            return out | {"status": "granted", "uid": policy["uid"], "vscode_server_dir": policy["vscode_server_dir"],
                          "repository": repo, "detail": "trusted VS Code terminal for this host and repository"}
    return out | {"status": "refused", "detail": f"checkout {identity['root']} ({identity['gitdir']}, "
                                                 f"{identity.get('remote')}) is not a policy repository"}


def proc_uids(pid: int) -> Optional[tuple]:
    raw = _proc_read(pid, "status")
    if raw is None:
        return None
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("Uid:"):
            try:
                return tuple(int(v) for v in line.split()[1:5])
            except ValueError:
                return None
    return None


def ptmx_indexes(pid: int) -> set:
    """pty slave indexes whose master (/dev/ptmx) *pid* holds open (fdinfo tty-index)."""
    out = set()
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except FileNotFoundError:
        return out
    except PermissionError as exc:
        raise ProcVisibilityError(errno.EACCES, f"/proc/{pid}/fd: {exc.strerror}")
    for fd in fds:
        try:
            if os.readlink(f"/proc/{pid}/fd/{fd}") not in ("/dev/ptmx", "/dev/pts/ptmx"):
                continue
            info = Path(f"/proc/{pid}/fdinfo/{fd}").read_text()
        except OSError:
            continue
        m = re.search(r"^tty-index:\s*(\d+)$", info, re.M)
        if m:
            out.add(int(m.group(1)))
    return out


def _protected_chain(top: str, path: str, uid: int, last_root_ok: bool = False) -> Optional[str]:
    """None when every component from *top* down to *path* is a real (non-symlink) entry owned by
    *uid* and protected (_owner_protected)."""
    if path != top and not path.startswith(top + "/"):
        return f"{path} is outside {top}"
    parts = [top] + [p for p in path[len(top):].split("/") if p]
    current = ""
    for i, part in enumerate(parts):
        current = part if i == 0 else f"{current}/{part}"
        try:
            st = os.lstat(current)
        except OSError as exc:
            return f"{current} is not inspectable ({exc.strerror})"
        if stat.S_ISLNK(st.st_mode) or not _owner_protected(st, uid, allow_root=last_root_ok and current == path):
            return f"{current} is a symlink or not owned and protected by uid {uid}"
    return None


def _node_role(pid: int, root: str, script: str) -> Optional[str]:
    """None when *pid* is `<root>/node [-opts] <root>/<script> ...` running the server's node binary."""
    cmd = proc_cmdline(pid) if pid and pid > 1 else None
    if not cmd:
        return f"process {pid} is gone or unreadable"
    rest = cmd[1:]
    while rest and rest[0].startswith("-"):
        rest = rest[1:]
    if cmd[0] != f"{root}/node" or rest[:1] != [f"{root}/{script}"]:
        return f"process {pid} is not this server's {script}"
    if proc_exe(pid) != os.path.realpath(f"{root}/node"):
        return f"process {pid} does not run the server's node binary"
    return None


def trusted_terminal_refusal(pid: int, root: str, pty_host: int, grant: dict) -> Optional[str]:
    """None when /proc proves *pid* is this uid's own VS Code integrated terminal under a granted
    policy: the server root lies in the policy's VS Code directory (protected, no symlinks), the
    init script is that server's own, bash is a root-owned system image, the shell is the session
    leader of a pty whose master the pty host holds, the pty host's parent is that server's
    server-main.js, its parent the server's bin/code-server launcher (root-owned shell image), every
    process of the chain runs as the policy uid, and no ancestor up to init is an autonomous agent
    or a managed worker. Otherwise why not. Limit: a same-uid process that deliberately launches a
    real VS Code server detached from any agent is indistinguishable; the policy accepts the
    operator's own editor, not containment."""
    uid = grant["uid"]
    sdir = grant["vscode_server_dir"]
    why = _protected_chain(sdir, root, uid)
    if why:
        return f"server root: {why}"
    init = f"{root}/{VSCODE_BASH_INTEGRATION}"
    why = _protected_chain(root, init, uid)
    if why:
        return f"shell integration script: {why}"
    if not stat.S_ISREG(os.lstat(init).st_mode):
        return "shell integration script is not a regular file"
    exe = proc_exe(pid)
    why = _root_owned_image(exe) if exe else "executable image not readable"
    if why:
        return f"bash image: {why}"
    fields = proc_stat_fields(pid)
    if not fields or len(fields) < 7:
        return "process stat not readable"
    if fields[5] != str(pid):
        return "the shell is not the session leader of its terminal"
    tty = int(fields[6])
    major, minor = (tty >> 8) & 0xFFF, (tty & 0xFF) | ((tty >> 12) & 0xFFF00)
    if major not in PTS_MAJORS:
        return "the shell has no pseudo-terminal"
    if minor not in ptmx_indexes(pty_host):
        return f"pty host {pty_host} does not hold the master of /dev/pts/{minor}"
    server = proc_parent(pty_host)
    why = _node_role(server, root, "out/server-main.js")
    if why:
        return f"pty host parent: {why}"
    launcher = proc_parent(server)
    lcmd = proc_cmdline(launcher) if launcher and launcher > 1 else None
    if not lcmd or len(lcmd) < 2 or Path(lcmd[0]).name not in INTERACTIVE_SHELLS or lcmd[1] != f"{root}/bin/code-server":
        return f"server parent {launcher} is not this server's bin/code-server launcher"
    lexe = proc_exe(launcher)
    why = _root_owned_image(lexe) if lexe else "launcher image not readable"
    if why:
        return f"launcher image: {why}"
    for p in (pid, pty_host, server, launcher):
        uids = proc_uids(p)
        if not uids or set(uids) != {uid}:
            return f"process {p} does not run as uid {uid} ({uids})"
    for p in ancestors(launcher):
        if p <= 1:
            break
        cmd = proc_cmdline(p)
        if cmd is None:
            return f"ancestor {p} vanished during inspection"
        if _is_agent(cmd) or is_managed_worker(cmd):
            return f"started under an autonomous agent or managed worker (ancestor {p}: {Path(cmd[0]).name if cmd else '?'})"
    return None


# Image names that legitimately run under another passive name (Debian alternatives: vi -> vim.tiny).
PASSIVE_IMAGE_ALIASES = {"vi": {"vi", "vim", "nvim", "nvi"}, "view": {"view", "vim", "nvim"}, "hx": {"hx", "helix"},
                         "helix": {"hx", "helix"}, "zed": {"zed", "zed-editor"}, "code": {"code", "code-oss"},
                         "sh": {"sh", "dash", "bash", "busybox"}}
# Packaging suffixes of an image name: vim.basic, vim.tiny, emacs-gtk, emacs-29.1, clangd-17, python3.12.
IMAGE_VARIANT_RE = re.compile(r"(?:\.(?:basic|tiny|gtk3?|nox|athena|lucid|motif)|-(?:gtk3?|nox|lucid|pgtk|x11|wayland)"
                              r"|-?\d+(?:\.\d+)*)$")
PASSIVE_IDENTITY_KINDS = {"editor", "git-readonly"}


def _image_name(exe: str) -> str:
    return IMAGE_VARIANT_RE.sub("", Path(exe.removesuffix(" (deleted)")).name)


def _interpreter_family(name: str) -> Optional[str]:
    name = IMAGE_VARIANT_RE.sub("", name)
    if name in ("node", "nodejs"):
        return "node"
    if name in ("python", "python3"):
        return "python"
    return name if name in SCRIPT_INTERPRETERS else None


def _root_owned_image(exe: str) -> Optional[str]:
    """None when *exe* and every directory above it are owned by root and not group/world writable,
    so its name was chosen by root; otherwise why not. A user copy of python named gopls or
    vim.basic, a symlink farm or a hard link in a user directory all fail here."""
    if not exe.startswith("/") or exe.endswith(" (deleted)") or os.path.normpath(exe) != exe:
        return f"image {exe} is not a normalized live path"
    path = exe
    while True:
        try:
            st = os.lstat(path)
        except OSError as exc:
            return f"image path {path} is not inspectable: {exc.strerror}"
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or stat.S_ISLNK(st.st_mode):
            return f"{path} is not root-owned and protected"
        if path == "/":
            return None
        path = os.path.dirname(path)


def passive_image_mismatch(pid: int, cmd: list) -> Optional[str]:
    """None when /proc/<pid>/exe is the program the command line names (by image name, packaging
    suffixes and listed aliases allowed) AND a root-owned image in root-owned directories. argv[0]
    alone is a claim any process can make (exec -a, a symlink named like the tool); an image name
    alone is one too (a user copy of an interpreter named like the tool)."""
    exe = proc_exe(pid)
    if not exe:
        return "executable image not readable"
    claimed = Path(cmd[0]).name.lstrip("-")
    image = _image_name(exe)
    if image not in PASSIVE_IMAGE_ALIASES.get(claimed, {claimed}):
        return f"command line says {claimed} but the process runs {exe}"
    return _root_owned_image(exe)


UNGRANTED_IDENTITY_KINDS = {"editor", "language-server", "dev-server", "shell", "git-readonly"}
NODE_FAMILY = {"node", "deno", "bun"}


def ungranted_image_mismatch(pid: int, cmd: list, kind: str) -> Optional[str]:
    """Without a grant, the passive name must still be the process's real program: /proc/<pid>/exe
    has the claimed image name (packaging suffixes and listed aliases allowed; ownership is not
    required, user installs stay accepted), an interpreter command line runs on that interpreter,
    and a node-hosted tool's script is an installed package (resolves inside node_modules).
    `exec -a vite <writer>`, a symlink named like the tool and `node /tmp/vite.js` fail here."""
    exe = proc_exe(pid)
    if not exe:
        return "executable image not readable"
    claimed = Path(cmd[0]).name.lstrip("-")
    image = _image_name(exe)
    family = _interpreter_family(claimed)
    if family:
        if _interpreter_family(image) != family:
            return f"command line says {claimed} but the process runs {exe}"
        i = _script_index(cmd)
        if family in NODE_FAMILY and kind in ("dev-server", "language-server") and i is not None:
            script = cmd[i]
            if not script.startswith("/"):
                try:
                    script = os.path.join(os.readlink(f"/proc/{pid}/cwd"), script)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EPERM):
                        raise ProcVisibilityError(exc.errno, f"/proc/{pid}/cwd: {exc.strerror}")
                    return f"cannot resolve the script {cmd[i][:80]}"
            real = os.path.realpath(script)
            if "node_modules" not in Path(real).parts[:-1]:
                return f"script {real[:120]} is not an installed package (outside node_modules)"
        return None
    if image not in PASSIVE_IMAGE_ALIASES.get(claimed, {claimed}) | {_image_name(claimed)}:
        return f"command line says {claimed} but the process runs {exe}"
    return None


def git_environ_refusal(pid: int) -> Optional[str]:
    """Why the environment of a git read carries command-line configuration
    (GIT_CONFIG_PARAMETERS, GIT_CONFIG_COUNT/KEY_n/VALUE_n) that is not display-only."""
    raw = _proc_read(pid, "environ")
    if raw is None:
        return None
    env = dict(e.split("=", 1) for e in raw.decode("utf-8", "surrogateescape").split("\0") if "=" in e)
    params = env.get("GIT_CONFIG_PARAMETERS")
    if params:
        try:
            pairs = shlex.split(params)
        except ValueError:
            return "unparseable GIT_CONFIG_PARAMETERS"
        for pair in pairs:
            key, eq, value = pair.partition("=")
            why = git_config_refusal(key, value if eq else None)
            if why:
                return f"{why} (GIT_CONFIG_PARAMETERS)"
    count = env.get("GIT_CONFIG_COUNT")
    if count:
        if not count.isdigit():
            return "unparseable GIT_CONFIG_COUNT"
        for n in range(int(count)):
            why = git_config_refusal(env.get(f"GIT_CONFIG_KEY_{n}", ""), env.get(f"GIT_CONFIG_VALUE_{n}"))
            if why:
                return f"{why} (GIT_CONFIG_COUNT)"
    return None


def proc_stdout_is_terminal(pid: int) -> Optional[bool]:
    """Whether fd 1 of *pid* is a terminal (None: not readable or gone)."""
    try:
        target = os.readlink(f"/proc/{pid}/fd/1")
    except FileNotFoundError:
        return False if proc_exited(pid) is False else None
    except OSError:
        return None
    return target.startswith(("/dev/pts/", "/dev/tty", "/dev/console"))


def classify_process(pid: int, self_pid: int, anchors: Iterable[int], self_chain: set,
                     grant: Optional[dict] = None) -> dict:
    """One scanner match -> {"pid", "kind", "blocking", "detail"}. *self_chain* is the
    ancestry up to the verified anchor (see :func:`self_chain`), never beyond it."""
    anchors = set(anchors)
    if pid == self_pid or pid in self_chain or pid in anchors:
        return {"pid": pid, "kind": "self-chain", "blocking": False, "detail": "this coordinator's own process chain"}
    try:
        cmd = proc_cmdline(pid)
        if cmd is None or (not cmd and proc_exited(pid)):
            return {"pid": pid, "kind": "gone", "blocking": False, "detail": "exited before inspection"}
        if not cmd:  # alive with nothing to read: an exited leader with live threads, a rewritten argv
            return {"pid": pid, "kind": "unknown", "blocking": True,
                    "detail": "empty command line on a process that can still run (live threads); not identifiable"}
        lineage = ancestors(pid)
        if anchors & set(lineage) or self_pid in lineage:
            return {"pid": pid, "kind": "own-descendant", "blocking": False,
                    "detail": f"descends from this coordinator ({Path(cmd[0]).name if cmd else '?'})"}
        if (grant or {}).get("status") != "granted":
            # No valid policy for this host, uid and repository: the pre-PR28 classification.
            kind, blocking, detail = classify_cmdline_ungranted(cmd)
            if not blocking and kind in UNGRANTED_IDENTITY_KINDS:
                why = ungranted_image_mismatch(pid, cmd, kind) or (git_environ_refusal(pid)
                                                                     if kind == "git-readonly" else None)
                if why:
                    if proc_exited(pid):
                        return {"pid": pid, "kind": "gone", "blocking": False, "detail": "exited before inspection"}
                    return {"pid": pid, "kind": "unknown", "blocking": True,
                            "detail": f"claims {kind} ({detail}) but {why}; treated as a possible writer"}
            return {"pid": pid, "kind": kind, "blocking": blocking, "detail": detail}
        vscode = classify_vscode(pid, cmd, grant)
        kind, blocking, detail = vscode or classify_cmdline(cmd)
        if not vscode and not blocking and kind in PASSIVE_IDENTITY_KINDS:
            mismatch = passive_image_mismatch(pid, cmd)
            if mismatch:
                if proc_exited(pid):
                    return {"pid": pid, "kind": "gone", "blocking": False, "detail": "exited before inspection"}
                return {"pid": pid, "kind": "unknown", "blocking": True,
                        "detail": f"claims {kind} ({detail}) but {mismatch}; treated as a possible writer"}
            if kind == "git-readonly" and proc_stdout_is_terminal(pid) is not False:
                # git starts pager.<cmd> (repository config, any program) only when stdout is a tty.
                return {"pid": pid, "kind": "unknown", "blocking": True,
                        "detail": f"{detail} with stdout on a terminal (or unreadable): it can start a "
                                  "repository-configured pager; treated as a possible writer"}
        return {"pid": pid, "kind": kind, "blocking": blocking, "detail": detail}
    except ProcVisibilityError as exc:
        return {"pid": pid, "kind": "unknown", "blocking": True, "detail": f"not inspectable: {exc}"}


def classify_scan(scan: dict, self_pid: Optional[int] = None, anchors: Iterable[int] = (),
                  grant: Optional[dict] = None) -> dict:
    """*grant* is observer_grant(identity) of the scanned checkout (None: no policy consulted)."""
    self_pid = os.getpid() if self_pid is None else self_pid
    chain = self_chain(self_pid, anchors)
    rows = [classify_process(m["pid"], self_pid, anchors, chain, grant) | {"cwd": m["cwd"]}
            for m in scan.get("matches", [])]
    blocking = [r for r in rows if r["blocking"]]
    if scan.get("status") != "complete":
        blocking.append({"pid": None, "kind": "unknown", "blocking": True, "cwd": "",
                         "detail": f"scan incomplete ({scan.get('scanner', '?')}): {scan.get('tail', '')}"})
    return {"scanner": scan.get("scanner"), "status": scan.get("status"), "processes": rows,
            "blocking": blocking, "ok": not blocking,
            "observer_policy": {k: v for k, v in (grant or {"status": "none"}).items()
                                if k in ("status", "detail", "policy_sha256")}}


# --------------------------------------------------------- manifests ---

def manifests_dir() -> Path:
    return _private_dir(state_root() / "manifests")


def manifest_path(task_id: str) -> Path:
    return manifests_dir() / (re.sub(r"[^A-Za-z0-9_-]", "_", task_id) + ".json")


def baseline_path(task_id: str) -> Path:
    return manifests_dir() / (re.sub(r"[^A-Za-z0-9_-]", "_", task_id) + ".baseline.json")


def dirty_paths(git: GitRunner, root: str) -> list:
    """[(status, path, origin)] of staged, unstaged and untracked (not ignored) entries; paths
    are taken literally from `git status -z` (no quoting, no stripping; a dotfile stays a dotfile)."""
    r = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    if r.returncode != 0:
        raise LeaseError(f"git status failed: {(r.stderr or r.stdout).strip()[:300]}")
    out, parts = [], (r.stdout or "").split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        origin = None
        if code[0] in "RC" or code[1] in "RC":  # rename/copy: origin path follows as its own record
            if i < len(parts):
                origin = parts[i]
                i += 1
        out.append((code, path, origin))
    return out


_FILE_TYPES = ((stat.S_ISREG, "file"), (stat.S_ISLNK, "symlink"), (stat.S_ISDIR, "dir"), (stat.S_ISFIFO, "fifo"),
               (stat.S_ISSOCK, "socket"), (stat.S_ISCHR, "chardev"), (stat.S_ISBLK, "blockdev"))
_GIT_MODES = {"file": None, "symlink": "120000", "dir": "040000"}


def _file_type(mode: int) -> str:
    return next((name for test, name in _FILE_TYPES if test(mode)), "other")


def _open_parent(root: str, rel: str) -> tuple:
    """(dir fd, last component) of *rel* under *root*, walking every directory component with
    O_NOFOLLOW so a symlinked parent never redirects the snapshot outside the checkout."""
    parts = [p for p in rel.rstrip("/").split("/")]
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise LeaseError(f"unexpected path from git status: {rel!r}")
    flags = os.O_RDONLY | os.O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
    fd = os.open(root, flags)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def worktree_state(root: str, rel: str) -> dict:
    """Type, permission bits, git-style mode and content hash of *rel*, all from one inode: lstat
    via a no-follow directory walk, regular files hashed through an O_NOFOLLOW|O_NONBLOCK fd whose
    fstat must be that same inode, unchanged after reading. A symlink hashes its target string; a
    directory is "dir"; a FIFO, socket or device is never opened ("special:<type>", no content
    hash, so it can never match an inventory). Absent: all None."""
    absent = {"worktree_type": None, "worktree_perm": None, "worktree_mode": None, "worktree_sha256": None}
    try:
        dfd, name = _open_parent(root, rel)
    except (FileNotFoundError, NotADirectoryError):
        return absent
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # a parent component is a symlink: git never lists below one
            raise LeaseError(f"{rel}: a parent directory is a symlink; not snapshotted")
        raise
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return absent
        kind, perm = _file_type(st.st_mode), stat.S_IMODE(st.st_mode)
        if kind == "file":
            mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
            fd = os.open(name, os.O_RDONLY | O_NOFOLLOW | O_CLOEXEC | getattr(os, "O_NONBLOCK", 0), dir_fd=dfd)
            try:
                before = os.fstat(fd)
                if (before.st_dev, before.st_ino, before.st_mode) != (st.st_dev, st.st_ino, st.st_mode):
                    raise LeaseError(f"{rel} was replaced while it was snapshotted; retry")
                h = hashlib.sha256()
                for chunk in iter(lambda: os.read(fd, 1 << 20), b""):
                    h.update(chunk)
                after = os.fstat(fd)
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != \
                        (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                    raise LeaseError(f"{rel} changed while it was snapshotted; retry")
            finally:
                os.close(fd)
            digest = h.hexdigest()
        elif kind == "symlink":
            mode = "120000"
            digest = "symlink:" + hashlib.sha256(os.readlink(name, dir_fd=dfd).encode("utf-8", "surrogateescape")).hexdigest()
        elif kind == "dir":
            mode, digest = "040000", "dir"
        else:
            mode, digest = f"special:{kind}", f"special:{kind}"
        return {"worktree_type": kind, "worktree_perm": perm, "worktree_mode": mode, "worktree_sha256": digest}
    finally:
        os.close(dfd)


def worktree_mode(root: str, rel: str) -> Optional[str]:
    """Git-style mode of the path in the working tree (see worktree_state); None when absent."""
    return worktree_state(root, rel)["worktree_mode"]


def hash_worktree_path(root: str, rel: str) -> Optional[str]:
    return worktree_state(root, rel)["worktree_sha256"]


INDEX_MODES = {"100644", "100755", "120000", "160000"}
OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
UNMERGED_RE = re.compile(r"^[123]:(?:100644|100755|120000|160000):(?:[0-9a-f]{40}|[0-9a-f]{64})"
                         r"(?:,[123]:(?:100644|100755|120000|160000):(?:[0-9a-f]{40}|[0-9a-f]{64}))*$")


def index_entry(git: GitRunner, root: str, rel: str) -> tuple:
    """(mode, object id) of *rel* in the index, or (None, None) when it is not staged/tracked.
    The path is matched literally (--literal-pathspecs, exact record path). An unmerged path binds
    every stage: ("unmerged", "<stage>:<mode>:<oid>,..."). Malformed records fail closed."""
    r = git(["--literal-pathspecs", "ls-files", "-s", "-z", "--", rel], root)
    if r.returncode != 0:
        raise LeaseError(f"git ls-files failed for {rel}: {(r.stderr or r.stdout or '').strip()[:200]}")
    stages = []
    for record in (r.stdout or "").split("\0"):
        if not record:
            continue
        meta, sep, path = record.partition("\t")
        if not sep or path != rel:
            continue  # a directory pathspec lists the files below it; only the exact path counts
        fields = meta.split(" ")
        if len(fields) != 3 or fields[0] not in INDEX_MODES or not OID_RE.fullmatch(fields[1]) or fields[2] not in "0123" \
                or len(fields[2]) != 1:
            raise LeaseError(f"unexpected index record for {rel}: {meta[:80]!r}")
        stages.append((fields[2], fields[0], fields[1]))
    if not stages:
        return None, None
    if len(stages) == 1 and stages[0][0] == "0":
        return stages[0][1], stages[0][2]
    if any(stage == "0" for stage, _, _ in stages) or len({stage for stage, _, _ in stages}) != len(stages):
        raise LeaseError(f"inconsistent index stages for {rel}")
    return "unmerged", ",".join(f"{stage}:{mode}:{oid}" for stage, mode, oid in sorted(stages))


def index_oid(git: GitRunner, root: str, rel: str) -> Optional[str]:
    return index_entry(git, root, rel)[1]


ENTRY_IDENTITY = ("status", "worktree_sha256", "worktree_mode", "worktree_type", "worktree_perm", "index_oid",
                  "index_mode", "origin")


def snapshot_dirty(git: GitRunner, identity: dict) -> dict:
    """Full identity of every dirty path: status code, worktree type, permission bits, git mode
    and content hash (one inode, see worktree_state), index object(s) and mode, rename origin.
    Hashes only, never contents."""
    entries = {}
    for code, rel, origin in dirty_paths(git, identity["root"]):
        mode, oid = index_entry(git, identity["root"], rel)
        entries[rel] = {"status": code, **worktree_state(identity["root"], rel), "index_oid": oid, "index_mode": mode,
                        "origin": origin}
    return entries


def same_entry(a: Optional[dict], b: Optional[dict]) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return all(a.get(k) == b.get(k) for k in ENTRY_IDENTITY)


def entry_diff(a: dict, b: dict) -> list:
    return [k for k in ENTRY_IDENTITY if a.get(k) != b.get(k)]


def _bound(identity: dict, extra: Optional[dict] = None) -> dict:
    return {"root": identity["root"], "gitdir": identity["gitdir"], "remote": identity.get("remote", ""),
            "branch": identity.get("branch", ""), "head": identity.get("head", ""), **(extra or {})}


def _binding_ok(record: Optional[dict], identity: dict, head: bool = True) -> bool:
    if not record:
        return False
    return record.get("gitdir") == identity["gitdir"] and record.get("root") == identity["root"] \
        and same_remote(record.get("remote") or "", identity.get("remote") or "") \
        and record.get("branch") == identity.get("branch") \
        and (not head or record.get("head") == identity.get("head"))


def write_manifest(task_id: str, identity: dict, entries: dict, source: str) -> dict:
    payload = {"version": MANIFEST_VERSION, "task": task_id, **_bound(identity),
               "source": source, "created_at": time.time(), "entries": entries}
    _atomic_replace(manifests_dir(), manifest_path(task_id).name, payload)
    return payload


def read_manifest(task_id: str) -> Optional[dict]:
    return _read_json_nofollow(manifest_path(task_id)) or None


def write_baseline(task_id: str, identity: dict, owner: dict, lease: dict, entries: dict) -> dict:
    payload = {"version": BASELINE_VERSION, "task": task_id, **_bound(identity), "owner": owner,
               "lease_acquired_at": lease.get("acquired_at"), "created_at": time.time(), "entries": entries}
    _atomic_replace(manifests_dir(), baseline_path(task_id).name, payload)
    return payload


def read_baseline(task_id: str) -> Optional[dict]:
    return _read_json_nofollow(baseline_path(task_id)) or None


def drop_baseline(task_id: str) -> None:
    try:
        os.unlink(baseline_path(task_id))
    except FileNotFoundError:
        pass


def checkpoint(task_id: str, identity: dict, git: GitRunner, *, owner: Optional[dict] = None,
               scan_runner: ScanRunner = default_scan_runner, registry: Optional[dict] = None,
               self_pid: Optional[int] = None) -> dict:
    """Record this task's own dirty files (hashes only) so the next preflight resumes them.

    Gated: this run must hold the live lease (renewed here, never taken), the writer scan must
    be clean and complete, and a baseline from a passing `preflight` of this same lease tenure
    at this HEAD must exist. What is adopted is exactly: entries already the task's own that are
    unchanged, plus the paths that changed or appeared since the baseline (the edit-round
    evidence). A dirty path that was never the task's cannot pass preflight, so it never gets a
    baseline and is never laundered here. Limit: same-uid cooperative evidence; this proves the
    lease was held and no competing writer was observed between baseline and checkpoint, not
    who wrote the bytes."""
    if not owner:
        raise IdentityError("checkpoint needs the card's bound worker identity (bound_worker)")
    lease = acquire_lease(identity, owner, renew_only=True)
    if lease["status"] != "renewed":
        raise LeaseError(f"checkpoint needs this run's live lease on the checkout ({lease['status']}: {lease['reason']}); "
                         "run preflight first")
    scan = classify_scan(run_scan(identity["root"], scan_runner, registry), self_pid, anchors=[owner["pid"]],
                         grant=observer_grant(identity))
    if not scan["ok"]:
        raise LeaseError("checkpoint refused: " + "; ".join(
            f"pid {b['pid']}: {b['kind']} ({b['detail']})" for b in scan["blocking"]))
    baseline = read_baseline(task_id)
    if not baseline or not same_owner(baseline.get("owner") or {}, owner) \
            or baseline.get("lease_acquired_at") != lease["lease"].get("acquired_at"):
        raise LeaseError("checkpoint refused: no baseline from a passing preflight of this run under this lease; "
                         "run preflight (exit 0) first")
    if not _binding_ok(baseline, identity):
        raise LeaseError(f"checkpoint refused: the baseline was taken on branch {baseline.get('branch')} at "
                         f"{(baseline.get('head') or '')[:12]}, the checkout is on {identity.get('branch')} at "
                         f"{(identity.get('head') or '')[:12]}; run preflight again")
    base_entries = baseline.get("entries") or {}
    manifest = read_manifest(task_id)
    recorded = ((manifest or {}).get("entries") or {}) if _binding_ok(manifest, identity, head=False) else {}
    current = snapshot_dirty(git, identity)
    kept, changed, new = [], [], []
    entries = {}
    for rel, info in sorted(current.items()):
        entries[rel] = info
        if rel in base_entries and same_entry(base_entries[rel], info):
            kept.append(rel)  # unchanged since the baseline; the baseline only holds task-owned paths
        elif rel in base_entries or rel in recorded:
            changed.append(rel)
        else:
            new.append(rel)
    dropped = sorted(set(recorded) - set(current))
    payload = write_manifest(task_id, identity, entries, "checkpoint")
    write_baseline(task_id, identity, owner, lease["lease"], current)
    payload["evidence"] = {"baseline_at": baseline.get("created_at"), "lease_acquired_at": lease["lease"].get("acquired_at"),
                           "scanner": scan.get("scanner"), "kept": kept, "changed_since_baseline": changed,
                           "new_since_baseline": new, "dropped": dropped,
                           "limits": "same-uid cooperative evidence: lease held and no competing writer observed; "
                                     "not proof of who wrote the bytes"}
    return payload


INVENTORY_REQUIRED = ("task", "root", "gitdir", "remote", "branch", "head", "status_sha256", "files")
# index_sha256 stays last: the worker card documents it (test_kanban_workflow.rl_inventory_fields);
# predecessor_sha256 is written only by successor-inventory, never by hand.
INVENTORY_OPTIONAL = ("role", "note", "version", "created_at", "predecessor_sha256", "index_sha256")
ROW_REQUIRED = ("path", "status", "type", "mode", "sha256", "index_oid", "index_mode", "origin")
ROW_OPTIONAL = ("index_sha256",)
FIELD_LABEL = {"status": "status differs", "worktree_sha256": "content differs", "worktree_mode": "mode differs",
               "worktree_type": "type differs", "worktree_perm": "mode differs",
               "index_oid": "index differs", "index_mode": "index differs", "origin": "origin differs"}
SHA256_RE = re.compile(r"^(symlink:)?[0-9a-f]{64}$")
HEX256_RE = re.compile(r"^[0-9a-f]{64}$")
STATUS_RE = re.compile(r"^[ MTADRCU?!]{2}$")


def _row_malformed(row: dict) -> Optional[str]:
    """Why an inventory row's fields are not well-formed (checked before anything reaches git)."""
    if row["type"] not in ("file", "symlink", None) or not (row["mode"] is None or _strict_int(row["mode"], 0, 0o7777)) \
            or not (row["sha256"] is None or (isinstance(row["sha256"], str) and SHA256_RE.fullmatch(row["sha256"]))) \
            or (row["type"] is None) != (row["sha256"] is None) or (row["type"] is None) != (row["mode"] is None):
        return "type/mode/sha256"
    if not isinstance(row["status"], str) or not STATUS_RE.fullmatch(row["status"]):
        return "status"
    if row["origin"] is not None and (not isinstance(row["origin"], str) or not row["origin"]):
        return "origin"
    oid, mode = row["index_oid"], row["index_mode"]
    if (oid is None) != (mode is None):
        return "index_oid/index_mode"
    if oid is not None and not (isinstance(oid, str) and isinstance(mode, str)
                                and ((mode in INDEX_MODES and OID_RE.fullmatch(oid))
                                     or (mode == "unmerged" and UNMERGED_RE.fullmatch(oid)))):
        return "index_oid/index_mode"
    if "index_sha256" in row and not (isinstance(row["index_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["index_sha256"])):
        return "index_sha256"
    return None


def status_sha256(git: GitRunner, root: str) -> str:
    """sha256 of `git status --porcelain=v1 -z --untracked-files=all` run through *git*."""
    r = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    if r.returncode != 0:
        raise LeaseError(f"git status failed: {(r.stderr or r.stdout or '').strip()[:300]}")
    return hashlib.sha256(git_bytes(r.stdout)).hexdigest()


def index_file_sha256(identity: dict) -> Optional[str]:
    try:
        return hashlib.sha256(Path(identity["gitdir"], "index").read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _refuse(problems: list, what: str = "inventory") -> None:
    if problems:
        raise LeaseError(f"{what} does not match the checkout; not adopted: " + "; ".join(problems[:12]))


def _inventory_entries(data: dict, task_id: str, identity: dict, git: GitRunner, current: dict) -> dict:
    """Adopted entries from a strict operator inventory: {task, root, gitdir, remote, branch, head,
    status_sha256, [index_sha256], files: [{path, status, type, mode, sha256, index_oid, index_mode,
    origin, [index_sha256]}]}. Every field is required and compared: the binding to this task and
    checkout, the exact `git status` digest, the raw index file digest when supplied, and per path the
    status, rename origin, staged object and mode, worktree content (sha256 of a symlink is of its
    target string; null for a path absent from the worktree), lstat type and permission bits, and the
    staged blob's sha256 when supplied. Unknown fields are refused, never ignored. An inventory without
    these bindings (the earlier 5-field shape) is refused as a whole; produce a richer successor."""
    missing = [k for k in INVENTORY_REQUIRED if k not in data]
    unknown = sorted(set(data) - set(INVENTORY_REQUIRED) - set(INVENTORY_OPTIONAL))
    if missing or unknown:
        raise LeaseError("inventory is not the strict bound format; not adopted: "
                         + (f"missing field(s) {', '.join(missing)}" if missing else "")
                         + ("; " if missing and unknown else "") + (f"unknown field(s) {', '.join(unknown)}" if unknown else "")
                         + ". A richer successor inventory (root, gitdir, remote, status_sha256 and per-file status, type, "
                           "mode, sha256, index_oid, index_mode, origin) is needed; the source is not modified")
    problems = []
    if data["task"] != task_id:
        problems.append(f"inventory is for task {data['task'] or '(none)'}, not {task_id}")
    if data["root"] != identity["root"]:
        problems.append(f"inventory root {data['root']} is not the checkout {identity['root']}")
    if data["gitdir"] != identity["gitdir"]:
        problems.append(f"inventory gitdir {data['gitdir']} is not the checkout's {identity['gitdir']}")
    if not isinstance(data["remote"], str) or not same_remote(data["remote"], identity.get("remote") or ""):
        problems.append(f"inventory remote {data['remote']} is not the checkout's {identity.get('remote')}")
    if data["branch"] != identity.get("branch"):
        problems.append(f"inventory branch {data['branch'] or '(none)'} is not the checkout's {identity.get('branch')}")
    if data["head"] != identity.get("head"):
        problems.append(f"inventory HEAD {str(data['head'] or '(none)')[:12]} is not the checkout's {identity.get('head', '')[:12]}")
    if data["status_sha256"] != status_sha256(git, identity["root"]):
        problems.append("git status no longer matches the inventory (dirty set or status codes changed)")
    if "index_sha256" in data and data["index_sha256"] != index_file_sha256(identity):
        problems.append("index_sha256: the index file is not the one inventoried (staged content changed)")
    if not isinstance(data["files"], list) or not data["files"]:
        problems.append("files must be a non-empty list")
    _refuse(problems)
    entries, seen = {}, set()
    for row in data["files"]:
        if not isinstance(row, dict):
            raise LeaseError("inventory rows must be objects; not adopted")
        lacking = [k for k in ROW_REQUIRED if k not in row]
        extra = sorted(set(row) - set(ROW_REQUIRED) - set(ROW_OPTIONAL))
        if lacking or extra or not isinstance(row.get("path"), str):
            raise LeaseError(f"inventory row {row.get('path')!r} is not strict; not adopted: "
                             + (f"missing {', '.join(lacking)} " if lacking else "")
                             + (f"unknown {', '.join(extra)}" if extra else ""))
        rel = row["path"]
        if rel in seen:
            raise LeaseError(f"inventory lists {rel} twice (duplicate); not adopted")
        seen.add(rel)
        bad = _row_malformed(row)
        if bad:
            problems.append(f"malformed {bad}: {rel}")
            continue
        have = current.get(rel)
        if have is None:
            problems.append(f"not dirty here: {rel}")
            continue
        want = {"status": row["status"], "origin": row["origin"], "index_oid": row["index_oid"],
                "index_mode": row["index_mode"],
                "worktree_sha256": None if row["sha256"] is None else
                ("symlink:" + row["sha256"] if row["type"] == "symlink" else row["sha256"])}
        diffs = sorted({FIELD_LABEL[k] for k, v in want.items() if have.get(k) != v})
        # Type and permission bits from the same inode the content hash came from (snapshot_dirty).
        if have.get("worktree_type") != row["type"]:
            diffs.append("type differs")
        elif have.get("worktree_perm") != row["mode"]:
            diffs.append("mode differs")
        if "index_sha256" in row:
            # Only the index's own, already validated object id ever reaches git, and only when the
            # row names exactly that object as a single staged blob.
            staged = have.get("index_oid")
            blob = git(["cat-file", "blob", staged], identity["root"]) \
                if staged and staged == row["index_oid"] and have.get("index_mode") in ("100644", "100755", "120000") \
                and OID_RE.fullmatch(staged) else None
            if blob is None or blob.returncode != 0 or \
                    hashlib.sha256(git_bytes(blob.stdout)).hexdigest() != row["index_sha256"]:
                diffs.append("index_sha256 differs")
        if diffs:
            problems += [f"{d}: {rel}" for d in diffs]
            continue
        entries[rel] = have
    _refuse(problems)
    return entries


def _module_entries(data: dict, task_id: str, current: dict) -> dict:
    """This module's own manifest format ({entries: {path: entry}}): the whole entry identity must match."""
    if data.get("task") not in (None, task_id):
        raise LeaseError(f"manifest is for task {data.get('task')}, not {task_id}; not adopted")
    problems, entries = [], {}
    for rel, value in data["entries"].items():
        if not isinstance(value, dict):
            raise LeaseError("manifest entries must be objects with the full entry identity; not adopted")
        have = current.get(rel)
        if have is None:
            problems.append(f"not dirty here: {rel}")
            continue
        diffs = sorted({FIELD_LABEL[k] for k in ENTRY_IDENTITY if value.get(k) != have.get(k)})
        problems += [f"{d}: {rel}" for d in diffs]
        if not diffs:
            entries[rel] = have
    _refuse(problems, "manifest")
    return entries


def _map_entries(data: dict, current: dict) -> dict:
    """Compatibility {path: sha256} map. It binds worktree content only, so it is accepted only as
    exactly that: string sha256 values, no inventory/manifest field names, and no path with a staged
    (index) change, which only the strict inventory can bind."""
    reserved = sorted(set(data) & (set(INVENTORY_REQUIRED) | set(INVENTORY_OPTIONAL) | {"entries"}))
    if reserved:
        raise LeaseError(f"document has inventory/manifest field(s) {', '.join(reserved)} but is not that format; "
                         "it is never read as a {path: sha256} map; not adopted")
    bad = [rel for rel, v in data.items() if not isinstance(v, str) or not SHA256_RE.fullmatch(v)]
    if bad:
        raise LeaseError(f"{{path: sha256}} map values must be sha256 strings ({', '.join(map(str, bad[:5]))}); not adopted")
    problems, entries = [], {}
    for rel, want in data.items():
        have = current.get(rel)
        if have is None:
            problems.append(f"not dirty here: {rel}")
        elif want != have["worktree_sha256"]:
            problems.append(f"content differs: {rel}")
        elif have["status"][0] not in " ?" or have.get("origin"):
            problems.append(f"staged change on {rel}: a {{path: sha256}} map cannot bind the index; use the strict inventory")
        else:
            entries[rel] = have
    _refuse(problems, "manifest")
    return entries


def adopt_manifest(task_id: str, identity: dict, git: GitRunner, source_path: str, *, owner: Optional[dict] = None,
                   scan_runner: ScanRunner = default_scan_runner, registry: Optional[dict] = None,
                   self_pid: Optional[int] = None) -> dict:
    """Bootstrap from an explicit, operator-authorized source: the strict inventory of pre-existing
    work (see _inventory_entries), this module's own manifest format, or the compatibility
    {path: sha256} map (unstaged content only). Every listed path must match now; dirty paths not
    listed stay unknown (preflight reports them). Paths are matched literally.

    Gated like checkpoint, minus the lease: *owner* (this run's bound worker) is required; under the
    per-repository lock, held from the lease check to the manifest write, any live (or undecidable)
    lease that is not exactly this run's refuses adoption, another run of the same task included,
    and adopt never takes a lease itself; the writer scan must be clean and complete before
    provenance changes. The source file is only read; the manifest records its sha256. Cooperative
    same-uid evidence, not containment."""
    if not owner:
        raise IdentityError("adopt needs the card's bound worker identity (bound_worker)")
    if owner.get("task") != task_id:
        raise IdentityError(f"owner is bound to task {owner.get('task')}, not {task_id}")
    raw = Path(source_path).read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseError(f"manifest is not JSON ({exc}); not adopted") from exc
    if not isinstance(data, dict) or not data:
        raise LeaseError("manifest must be a non-empty JSON object (strict inventory, this module's format or "
                         "{path: sha256}); not adopted")
    with repo_lock(identity):
        holder = (read_lease(identity) or {}).get("owner") or {}
        if holder and not same_owner(holder, owner) and owner_alive(holder) is not False:
            raise LeaseError(f"checkout lease is held by task {holder.get('task')} run {holder.get('run_id')} "
                             f"(pid {holder.get('pid')}), not this run; adopt never overrides or takes a live lease; "
                             "not adopted")
        scan = classify_scan(run_scan(identity["root"], scan_runner, registry), self_pid, anchors=[owner["pid"]],
                             grant=observer_grant(identity))
        if not scan["ok"]:
            raise LeaseError("adopt refused (writer scan): " + "; ".join(
                f"pid {b['pid']}: {b['kind']} ({b['detail']})" for b in scan["blocking"]) + "; not adopted")
        fresh = repo_identity(identity["root"], git)
        if fresh["gitdir"] != identity["gitdir"]:
            raise LeaseError("checkout git dir changed; not adopted")
        identity = fresh
        current = snapshot_dirty(git, identity)
        predecessor = None
        if "files" in data:
            if not isinstance(data["files"], list):
                raise LeaseError("inventory 'files' must be a list; not adopted")
            predecessor = data.get("predecessor_sha256")
            if predecessor is not None:
                if not isinstance(predecessor, str) or not HEX256_RE.fullmatch(predecessor):
                    raise LeaseError("inventory predecessor_sha256 must be a sha256; not adopted")
                if predecessor in adopted_predecessors(task_id):
                    raise LeaseError(f"a successor of baseline {predecessor[:12]} was already adopted for {task_id}; "
                                     "the successor adoption is one-time; not adopted")
            entries, fmt = _inventory_entries(data, task_id, identity, git, current), "inventory"
        elif "entries" in data:
            if not isinstance(data["entries"], dict) or not data["entries"]:
                raise LeaseError("manifest 'entries' must be a non-empty object; not adopted")
            entries, fmt = _module_entries(data, task_id, current), "manifest"
        else:
            entries, fmt = _map_entries(data, current), "sha256-map"
        payload = {"version": MANIFEST_VERSION, "task": task_id, **_bound(identity),
                   "source": f"adopt:{Path(source_path).name}", "created_at": time.time(), "entries": entries,
                   "provenance": {"format": fmt, "source_sha256": hashlib.sha256(raw).hexdigest(),
                                  "owner": owner, "scanner": scan.get("scanner"),
                                  **({k: data.get(k) for k in INVENTORY_REQUIRED[:-1] + ("index_sha256",)}
                                     if fmt == "inventory" else {}),
                                  **({"predecessor_sha256": predecessor} if predecessor else {})}}
        _atomic_replace(manifests_dir(), manifest_path(task_id).name, payload)
        if predecessor:
            _atomic_replace(manifests_dir(), predecessors_path(task_id).name,
                            {"task": task_id, "adopted": sorted(adopted_predecessors(task_id) | {predecessor})})
        drop_baseline(task_id)  # provenance changed: the next checkpoint needs a fresh passing preflight
    payload["unlisted_dirty"] = sorted(set(current) - set(entries))
    return payload


def predecessors_path(task_id: str) -> Path:
    if not TASK_RE.match(task_id):
        raise LeaseError(f"invalid task id {task_id!r}")
    return manifests_dir() / f"{task_id}.predecessors.json"


def adopted_predecessors(task_id: str) -> set:
    data = _read_json_nofollow(predecessors_path(task_id)) or {}
    return {v for v in data.get("adopted") or [] if isinstance(v, str)}


# The pre-existing operator baseline shape (task, role, head, branch, status_sha256,
# index_sha256, files[{path, status, sha256, mode, type}]): never adopted as such (it binds no
# root, gitdir, remote, staged object or rename origin), but a strict successor can be derived from
# it once, read-only, when the checkout still matches every field it does bind.
LEGACY_BASELINE_FIELDS = ("task", "role", "head", "branch", "status_sha256", "index_sha256", "files")
LEGACY_ROW_FIELDS = ("path", "status", "sha256", "mode", "type")


def successor_inventory(task_id: str, identity: dict, git: GitRunner, original_path: str, original_sha256: str,
                        out_path: str) -> dict:
    """Derive the strict inventory (see _inventory_entries) from the original baseline at
    *original_path*, pinned by *original_sha256*, and write it to *out_path* (new file, 0600,
    outside the checkout). Refuses unless the original is exactly the legacy shape for *task_id* and
    the checkout still matches everything it binds: branch, HEAD, `git status` digest, index file
    digest, and per path status, type, permission bits and content; its rows must be exactly the
    dirty set. The added bindings (root, gitdir, remote, index object/mode, rename origin) come from
    the same snapshot; predecessor_sha256 makes `adopt` of the successor one-time. Reads the
    checkout only: never adopts, never writes the original or the checkout."""
    if not isinstance(original_sha256, str) or not HEX256_RE.fullmatch(original_sha256):
        raise LeaseError("--original-sha256 must be the sha256 of the original baseline")
    original = os.path.realpath(original_path)
    out = os.path.abspath(out_path)
    root = identity["root"]
    for what, path in (("original", original), ("output", os.path.realpath(os.path.dirname(out)) + "/x")):
        if path == root or path.startswith(root + "/") or path.startswith(identity["gitdir"] + "/"):
            raise LeaseError(f"{what} must be outside the checkout and its git dir")
    if os.path.lexists(out) or os.path.realpath(out) == original:
        raise LeaseError(f"{out} exists; the successor is always a new file and the original is never replaced")
    raw = Path(original).read_bytes()
    if hashlib.sha256(raw).hexdigest() != original_sha256:
        raise LeaseError("the original baseline's sha256 is not the pinned one; no successor")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseError(f"original baseline is not JSON ({exc}); no successor") from exc
    if not isinstance(data, dict) or sorted(data) != sorted(LEGACY_BASELINE_FIELDS):
        raise LeaseError("original is not the legacy baseline shape "
                         f"({', '.join(LEGACY_BASELINE_FIELDS)}); no successor")
    problems = []
    if data["task"] != task_id:
        problems.append(f"original is for task {data['task']}, not {task_id}")
    if data["branch"] != identity.get("branch"):
        problems.append(f"branch {data['branch']} is not the checkout's {identity.get('branch')}")
    if data["head"] != identity.get("head"):
        problems.append(f"HEAD {str(data['head'])[:12]} is not the checkout's {(identity.get('head') or '')[:12]}")
    status_digest = status_sha256(git, root)
    if data["status_sha256"] != status_digest:
        problems.append("git status no longer matches the original (dirty set or status codes changed)")
    if data["index_sha256"] != index_file_sha256(identity):
        problems.append("the index file is not the one in the original (staged content changed)")
    rows = data["files"]
    if not isinstance(rows, list) or not rows:
        problems.append("files must be a non-empty list")
    _refuse(problems, "original baseline")
    current = snapshot_dirty(git, identity)
    files, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or sorted(row) != sorted(LEGACY_ROW_FIELDS) or not isinstance(row["path"], str):
            raise LeaseError(f"original row {row!r:.120} is not exactly {{{', '.join(LEGACY_ROW_FIELDS)}}}; no successor")
        rel = row["path"]
        if rel in seen:
            raise LeaseError(f"original lists {rel} twice; no successor")
        seen.add(rel)
        have = current.get(rel)
        if have is None:
            problems.append(f"not dirty here: {rel}")
            continue
        want_sha = None if row["sha256"] is None else \
            ("symlink:" + row["sha256"] if row["type"] == "symlink" else row["sha256"])
        for label, ok in (("status differs", have["status"] == row["status"]),
                          ("type differs", have["worktree_type"] == row["type"]),
                          ("mode differs", have["worktree_perm"] == row["mode"]),
                          ("content differs", have["worktree_sha256"] == want_sha)):
            if not ok:
                problems.append(f"{label}: {rel}")
        files.append({"path": rel, "status": have["status"], "type": have["worktree_type"], "mode": have["worktree_perm"],
                      "sha256": row["sha256"], "index_oid": have["index_oid"], "index_mode": have["index_mode"],
                      "origin": have["origin"]})
    problems += [f"dirty but not in the original: {rel}" for rel in sorted(set(current) - seen)]
    _refuse(problems, "original baseline")
    successor = {"version": 1, "task": task_id, "role": "strict successor of the operator baseline inventory",
                 "note": f"derived read-only from {original}", "created_at": time.time(),
                 "root": root, "gitdir": identity["gitdir"], "remote": identity.get("remote") or "",
                 "branch": identity.get("branch"), "head": identity.get("head"), "status_sha256": status_digest,
                 "index_sha256": data["index_sha256"], "predecessor_sha256": original_sha256, "files": files}
    body = (json.dumps(successor, indent=1, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    return {"successor": out, "successor_sha256": hashlib.sha256(body).hexdigest(), "predecessor_sha256": original_sha256,
            "files": len(files), "head": identity.get("head"), "branch": identity.get("branch"),
            "next": "review it, then `repo_lease.py adopt --task ... --checkout ... --manifest <successor>` "
                    "(needs the card's bound worker, no other live lease and a clean writer scan)"}


def verify_dirty(task_id: str, identity: dict, git: GitRunner) -> dict:
    """Current dirty paths vs the task manifest: known (identical change), changed (any part of
    the change differs: content, mode, staged object, status, rename origin), unknown."""
    manifest = read_manifest(task_id)
    recorded = (manifest or {}).get("entries") or {}
    current = snapshot_dirty(git, identity)
    known, changed, unknown, why = [], [], [], {}
    for rel, info in sorted(current.items()):
        rec = recorded.get(rel)
        if rec is None:
            unknown.append(rel)
        elif not same_entry(rec, info):
            changed.append(rel)
            why[rel] = entry_diff(rec, info)
        else:
            known.append(rel)
    # The manifest binds to root, git dir, remote and branch, not HEAD: a commit of some of the
    # recorded paths leaves the others the same change (their index object is part of the
    # identity, so a commit that did touch one shows as "changed"). Only the checkpoint baseline
    # is bound to HEAD.
    manifest_ok = _binding_ok(manifest, identity, head=False)
    if current and manifest and not manifest_ok:
        unknown = sorted(set(unknown) | set(known) | set(changed))
        known, changed, why = [], [], {}
    return {"clean": not current, "known": known, "changed": changed, "changed_fields": why, "unknown": unknown,
            "manifest": None if not manifest else {k: manifest.get(k) for k in ("branch", "head", "source", "created_at")},
            "ok": not changed and not unknown}


# ---------------------------------------------------------- preflight ---

def branch_checks(identity: dict, branch: str, base: str, git: GitRunner) -> list:
    root, checks = identity["root"], []
    index_lock = Path(identity["gitdir"], "index.lock")
    checks.append({"check": "index.lock", "ok": not index_lock.exists(),
                   "detail": "absent" if not index_lock.exists() else f"{index_lock} exists: another git process is writing"})
    if branch in PROTECTED_BRANCHES:
        checks.append({"check": "branch", "ok": False, "detail": f"'{branch}' is protected; never write there"})
    elif identity["branch"] != branch:
        checks.append({"check": "branch", "ok": False,
                       "detail": f"checkout is on '{identity['branch'] or 'detached HEAD'}', the card's branch is '{branch}'"})
    else:
        checks.append({"check": "branch", "ok": True, "detail": branch})
    r = git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], root)
    if r.returncode == 0 and identity["branch"] == branch:
        counts = git_text(git, ["rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"], root).split()
        ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
        checks.append({"check": "diverged", "ok": not (ahead and behind),
                       "detail": f"ahead {ahead}, behind {behind} of origin/{branch}" + (
                           "; diverged: the operator decides" if ahead and behind else "")})
    r = git(["worktree", "list", "--porcelain"], root)
    elsewhere = []
    if r.returncode == 0:
        current_wt, current_branch = None, None
        for line in (r.stdout or "").splitlines() + [""]:
            if line.startswith("worktree "):
                current_wt = line[len("worktree "):]
            elif line.startswith("branch "):
                current_branch = line[len("branch "):].removeprefix("refs/heads/")
            elif not line:
                if current_wt and current_branch == branch and str(Path(current_wt).resolve()) != root:
                    elsewhere.append(current_wt)
                current_wt, current_branch = None, None
    checks.append({"check": "worktrees", "ok": not elsewhere,
                   "detail": "branch is checked out in no other worktree" if not elsewhere
                   else f"'{branch}' is checked out in {', '.join(elsewhere)}"})
    return checks


def preflight(task_id: str, checkout: str, repo: str, branch: str, base: str = "main", *, git: GitRunner = default_git,
              scan_runner: ScanRunner = default_scan_runner, owner: Optional[dict] = None, ttl: int = DEFAULT_TTL,
              self_pid: Optional[int] = None, registry: Optional[dict] = None, require_clean: bool = False) -> dict:
    """Every check a code card must pass before touching the designated checkout.
    Returns {"ok", "exit", "checks", "lease", "scan", "dirty", "identity"}; exit 0 ok,
    2 conflict / action required, 3 unknown visibility (fail closed)."""
    identity = repo_identity(checkout, git)
    checks = [{"check": "remote", "ok": same_remote(identity["remote"], repo),
               "detail": "origin matches the card's repo" if same_remote(identity["remote"], repo)
               else f"origin is '{identity['remote'] or 'none'}', the card's repo is '{repo}'"}]
    checks += branch_checks(identity, branch, base, git)
    if not owner:
        raise IdentityError("preflight needs the card's bound worker identity (bound_worker)")
    lease = acquire_lease(identity, owner, ttl)
    checks.append({"check": "lease", "ok": lease["status"] != "conflict", "detail": f"{lease['status']}: {lease['reason']}"})
    scan = classify_scan(run_scan(identity["root"], scan_runner, registry), self_pid, anchors=[owner["pid"]],
                         grant=observer_grant(identity))
    checks.append({"check": "writers", "ok": scan["ok"],
                   "detail": "no competing writer" if scan["ok"] else "; ".join(
                       f"pid {b['pid']}: {b['kind']} ({b['detail']})" for b in scan["blocking"])})
    dirty = verify_dirty(task_id, identity, git)
    if require_clean:
        dirty_ok = dirty["clean"]
        detail = "clean" if dirty_ok else f"uncommitted paths: {', '.join((dirty['known'] + dirty['changed'] + dirty['unknown'])[:8])}"
    else:
        dirty_ok = dirty["ok"]
        detail = ("clean" if dirty["clean"] else
                  f"{len(dirty['known'])} task-owned path(s) resume" if dirty_ok else
                  (f"unknown dirty paths (not in this task's manifest): {', '.join(dirty['unknown'][:8])}" if dirty["unknown"] else "")
                  + (" changed since checkpoint: " + ", ".join(
                      f"{p} ({'/'.join(dirty['changed_fields'].get(p) or [])})" for p in dirty["changed"][:8])
                     if dirty["changed"] else ""))
    checks.append({"check": "dirty", "ok": dirty_ok, "detail": detail})
    ok = all(c["ok"] for c in checks)
    unknown = any(b["kind"] == "unknown" for b in scan["blocking"]) or lease["status"] == "conflict" and "not decidable" in lease["reason"]
    if ok and lease["status"] in ("acquired", "renewed", "recovered"):
        # Baseline for the edit round: every dirty path here is the task's own (or the tree is
        # clean); checkpoint adopts only what changes from this under the same lease tenure. A
        # failing preflight writes nothing: a tree with foreign dirty paths never gets a baseline.
        write_baseline(task_id, identity, owner, lease["lease"], snapshot_dirty(git, identity))
    return {"ok": ok, "exit": 0 if ok else (3 if unknown else 2), "task": task_id, "identity": identity,
            "checks": checks, "lease": {"status": lease["status"], "owner": lease["lease"].get("owner"), "reason": lease["reason"]},
            "scan": scan, "dirty": dirty}


def lease_status(checkout: str, git: GitRunner = default_git) -> dict:
    identity = repo_identity(checkout, git)
    lease = read_lease(identity)
    out = {"identity": identity, "lease": lease, "scanner": scanner_registry().get(identity["root"])}
    if lease:
        out["owner_alive"] = owner_alive(lease.get("owner") or {})
        out["age_seconds"] = int(time.time() - float(lease.get("renewed_at") or 0))
    return out


# ---------------------------------------------------------------- CLI ---

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, task=True):
        if task:
            sp.add_argument("--task", required=True, help="the code card id this run works for")
        sp.add_argument("--checkout", required=True, help="canonical path of the designated checkout")
        sp.add_argument("--json", action="store_true")

    pf = sub.add_parser("preflight", help="all checks before writing in the checkout (lease, writers, dirty provenance)")
    common(pf)
    pf.add_argument("--repo", required=True)
    pf.add_argument("--branch", required=True)
    pf.add_argument("--base", default="main")
    pf.add_argument("--require-clean", action="store_true", help="also fail on any uncommitted path (record-sha)")
    pf.add_argument("--owner-pid", type=int, default=None,
                    help="must equal the card's board-recorded bound worker pid (never an arbitrary anchor)")
    cp = sub.add_parser("checkpoint", help="record this task's own dirty files for resume (needs this run's lease, "
                                           "a clean writer scan and a baseline from a passing preflight)")
    common(cp)
    cp.add_argument("--owner-pid", type=int, default=None)
    ad = sub.add_parser("adopt", help="bootstrap the manifest from an explicit, authorized {path: sha256} file")
    common(ad)
    ad.add_argument("--manifest", required=True)
    rl = sub.add_parser("release", help="release this run's lease")
    common(rl)
    rl.add_argument("--owner-pid", type=int, default=None)
    sc = sub.add_parser("successor-inventory", help="derive the strict inventory from the original operator "
                                                     "baseline (read-only; writes only --out, never adopts)")
    common(sc)
    sc.add_argument("--original", required=True)
    sc.add_argument("--original-sha256", required=True)
    sc.add_argument("--out", required=True)
    op = sub.add_parser("observer-policy", help="print a trusted VS Code terminal policy template for this host "
                                                "and checkout (stdout only; the operator reviews and saves it)")
    common(op, task=False)
    op.add_argument("--vscode-server-dir", default=str(Path.home() / ".vscode-server"))
    rs = sub.add_parser("register-scanner", help="register the privileged CWD scanner for a checkout")
    rs.add_argument("checkout")
    rs.add_argument("argv", nargs=argparse.REMAINDER, help="-- sudo -n /absolute/helper")
    st = sub.add_parser("status", help="lease and scanner of a checkout")
    common(st, task=False)
    return p


def emit(result, as_json: bool = True) -> None:
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


HermesRunner = Callable[[list], subprocess.CompletedProcess]


def default_hermes(argv: list) -> subprocess.CompletedProcess:
    return subprocess.run([os.environ.get("HERMES_BIN", "hermes"), *argv], capture_output=True, text=True, timeout=120)


def read_show(task_id: str, hermes: HermesRunner = default_hermes) -> dict:
    """`hermes kanban show <id> --json`, the only board call this module makes (a read)."""
    r = hermes(["kanban", "show", task_id, "--json"])
    if r.returncode != 0:
        raise IdentityError(f"hermes kanban show {task_id} failed: {(r.stderr or r.stdout).strip()[:300]}")
    try:
        show = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise IdentityError(f"hermes kanban show {task_id} returned non-JSON output") from exc
    if not isinstance(show, dict):
        raise IdentityError(f"hermes kanban show {task_id} returned no task")
    return show


def cli_owner(task_id: str, checkout_identity: Optional[dict] = None, explicit_pid: Optional[int] = None,
              hermes: HermesRunner = default_hermes) -> dict:
    """Lease owner for the CLI: the card's bound worker (bound_worker), checked against the
    card that holds the checkout's lease when that is another card (a sibling bound to the
    same chain is ambiguous). Never this short-lived CLI process, never an env value."""
    show = read_show(task_id, hermes)
    others = []
    holder = ((read_lease(checkout_identity) or {}).get("owner") or {}) if checkout_identity else {}
    if holder.get("task") and holder["task"] != task_id and TASK_RE.match(str(holder["task"])):
        others.append(read_show(holder["task"], hermes))
    return bound_worker(task_id, show, others, explicit_pid=explicit_pid)


def main(argv=None, git: GitRunner = default_git, scan_runner: Optional[ScanRunner] = None,
         hermes: HermesRunner = default_hermes) -> int:
    args = parser().parse_args(argv)
    scan_runner = scan_runner or default_scan_runner  # resolved per call, not at import
    try:
        if args.cmd == "preflight":
            owner = cli_owner(args.task, repo_identity(args.checkout, git), args.owner_pid, hermes)
            result = preflight(args.task, args.checkout, args.repo, args.branch, args.base, git=git,
                               scan_runner=scan_runner, owner=owner, require_clean=args.require_clean)
            emit(result)
            return result["exit"]
        identity = None
        if args.cmd in ("checkpoint", "adopt", "release"):
            identity = repo_identity(args.checkout, git)
            owner = cli_owner(args.task, identity, getattr(args, "owner_pid", None), hermes)
        if args.cmd == "checkpoint":
            emit(checkpoint(args.task, identity, git, owner=owner, scan_runner=scan_runner))
            return 0
        if args.cmd == "adopt":
            emit(adopt_manifest(args.task, identity, git, args.manifest, owner=owner, scan_runner=scan_runner))
            return 0
        if args.cmd == "release":
            result = release_lease(identity, owner)
            emit(result)
            return 0 if result["status"] in ("released", "none") else 2
        if args.cmd == "successor-inventory":
            emit(successor_inventory(args.task, repo_identity(args.checkout, git), git, args.original,
                                     args.original_sha256, args.out))
            return 0
        if args.cmd == "observer-policy":
            identity = repo_identity(args.checkout, git)
            emit({"path": str(observer_policy_path()), "mode": "0600", "current": observer_grant(identity),
                  "template": {"version": OBSERVER_POLICY_VERSION, "kind": OBSERVER_POLICY_KIND, "host": host_identity(),
                               "uid": os.getuid(), "vscode_server_dir": os.path.normpath(args.vscode_server_dir),
                               "repositories": [{k: identity.get(k) or "" for k in OBSERVER_REPO_FIELDS}],
                               "accepted_risk": "<operator: state that you accept your own VS Code integrated "
                                                "terminal running in this checkout on this host>"}})
            return 0
        if args.cmd == "register-scanner":
            scanner = [a for a in args.argv if a != "--"]
            emit(register_scanner(args.checkout, scanner, git))
            return 0
        emit(lease_status(args.checkout, git))
        return 0
    except IdentityError as exc:
        print(f"repo-lease: capability: task-scoped worker identity: {exc}", file=sys.stderr)
        return 3
    except LeaseError as exc:
        print(f"repo-lease: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
