#!/usr/bin/env python3
"""Repository lease, competing-writer classification and task-owned change manifests for
the kanban-workflow coordinator working in an operator-designated checkout (`--checkout`).

    repo_lease.py preflight  --task T --checkout DIR --repo URL --branch B [--base main] [--json]
    repo_lease.py checkpoint --task T --checkout DIR            # record the task's own dirty files
    repo_lease.py adopt      --task T --checkout DIR --manifest FILE   # explicit provenance bootstrap
    repo_lease.py release    --task T --checkout DIR            # give the lease back
    repo_lease.py register-scanner DIR -- sudo -n /usr/local/libexec/<helper>
    repo_lease.py status     --checkout DIR [--json]

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
                               the supervising Hermes worker) and its descendants -> fine
  gone                         exited between scan and inspection (ENOENT/ESRCH)  -> fine
  editor / language-server /   known passive tools: editors, LSP servers, idle
  dev-server / shell           shells with no args or only -i/-l/--login, dev servers -> reported
  git-readonly                 git status/log/diff/show/...                       -> reported
  git-mutation                 git commit/merge/rebase/checkout/reset/stash/...  -> blocked
  agent-writer                 another autonomous coding agent (hermes worker, claude,
                               codex, aider, ...) not in our chain                -> blocked
  unknown                      anything else: arbitrary python/node/scripts, a shell
                               with any other argument (-c, -s, a script), a tty-attached tool that is not a
                               known editor, /proc unreadable, incomplete scan     -> blocked

Uncommitted changes are resumed only with provenance. The protocol is preflight ->
edit -> checkpoint: `preflight` (exit 0) records a baseline of the dirty tree under this
run's lease; `checkpoint` requires that live lease, a clean writer scan and that
baseline, and records as this task's own only what changed since the baseline (plus
what was already the task's). Dirty paths that were never this task's cannot pass
preflight, so they are never laundered by checkpoint; the operator's explicit `adopt`
with a {path: sha256} file is the only other way in. Entries carry the full identity of
a change (status, worktree hash and mode, index object and mode, rename origin), so a
foreign `git add`, a chmod or a symlink swap on a recorded path is detected. Paths are
kept literally (a dotfile is a dotfile). Limits: everything here is cooperative
same-uid evidence; a checkpoint proves the lease was held and no competing writer was
observed, not who typed the bytes. Nothing is ever reset, stashed, cleaned or deleted.
Stdlib only.
"""

from __future__ import annotations

import argparse
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
READONLY_GIT = {"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "ls-remote", "cat-file", "blame",
                "describe", "rev-list", "grep", "shortlog", "for-each-ref", "name-rev", "merge-base", "show-ref",
                "diff-tree", "diff-index", "diff-files", "count-objects", "var", "version", "help", "check-ignore",
                "check-attr", "symbolic-ref", "fsck", "reflog"}
# Autonomous coding agents by executable name. Blocking only when the process is NOT in our own
# chain. The passive sets below are the ONLY names that make a foreign process non-blocking;
# they are observations (reported), never writers by CWD alone. Nothing else is accepted by name.
AGENT_EXECUTABLES = {"hermes", "claude", "codex", "aider", "gemini", "cursor-agent", "copilot", "goose", "opencode"}
AGENT_CMDLINE_HINTS = ("work kanban task", "hermes_cli.main", "delegate_task")
PASSIVE_EDITORS = {"vim", "nvim", "vi", "view", "nano", "pico", "emacs", "emacsclient", "hx", "helix", "micro", "kak",
                   "code", "code-server", "code-insiders", "zed", "subl", "sublime_text"}
PASSIVE_LANGUAGE_SERVERS = {"gopls", "pyright-langserver", "pylsp", "pyls", "jedi-language-server", "typescript-language-server",
                            "tsserver", "rust-analyzer", "clangd", "sourcekit-lsp", "lua-language-server",
                            "bash-language-server", "yaml-language-server", "vscode-json-language-server",
                            "vscode-css-language-server", "vscode-html-language-server", "kotlin-language-server", "jdtls",
                            "eslint_d", "prettierd", "copilot-language-server", "tailwindcss-language-server", "ruff", "ruff-lsp"}
PASSIVE_DEV_SERVERS = {"vite", "webpack-dev-server", "webpack", "next", "nuxt", "astro", "nodemon", "air", "esbuild",
                       "browser-sync", "live-server", "http-server", "serve", "parcel", "rollup", "tsc", "turbo", "swc"}
INTERACTIVE_SHELLS = {"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh"}
# The only shell arguments that still mean "idle prompt": no arguments at all, or these exact
# interactive/login options. Anything else (-c, -s/--stdin, combined -lc/-sc, a script, ...)
# may be running a command and is unknown.
IDLE_SHELL_OPTIONS = {"-i", "-l", "--login", "--interactive"}
SCRIPT_INTERPRETERS = {"node", "nodejs", "deno", "bun", "python", "python3", "ruby", "perl"}
# Observations that are not writers; everything not listed here is "unknown" and blocks.
NONBLOCKING_KINDS = {"self-chain", "own-descendant", "gone", "editor", "language-server", "dev-server", "shell",
                     "git-readonly"}
MATCH_RE = re.compile(r"^MATCH:\s*PID=(\d+)\s+CWD=(\S.*)$")
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


def default_git(argv: list, cwd: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, timeout=120, env=env)


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
    raw = _proc_read(pid, "cmdline")
    if raw is None:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def proc_env_value(pid: int, key: str) -> Optional[str]:
    """One variable of /proc/<pid>/environ (same uid only); None when absent, gone or unreadable."""
    try:
        raw = _proc_read(pid, "environ")
    except ProcVisibilityError:
        return None
    if raw is None:
        return None
    prefix = key.encode("utf-8") + b"="
    for part in raw.split(b"\0"):
        if part.startswith(prefix):
            return part[len(prefix):].decode("utf-8", "replace")
    return None


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


def _is_hermes_cmd(cmd: list) -> bool:
    return any(Path(part).name == "hermes" or "hermes_cli" in part for part in cmd[:3])


def anchor_pid(explicit: Optional[int] = None, task_id: Optional[str] = None) -> int:
    """Long-lived process that stands for this coordinator run: an explicit pid, else a
    VERIFIED supervising Hermes worker, else this process itself.

    A candidate counts as the worker only when it is an ancestor of this process AND, when
    *task_id* is known, its own environment carries HERMES_KANBAN_TASK=<task_id>. The nearest
    `hermes` ancestor without that identity (typically the gateway every worker descends
    from) is never the anchor: with the gateway as anchor every foreign worker would have
    read as "own descendant". Falling back to our own pid keeps the chain narrow."""
    if explicit:
        return int(explicit)
    lineage = ancestors(os.getpid())

    def verified(pid: int) -> bool:
        if pid not in lineage:
            return False
        if task_id is None:
            return True
        return proc_env_value(pid, "HERMES_KANBAN_TASK") == task_id

    env_pid = (os.environ.get("HERMES_KANBAN_SUPERVISED_WORKER_PID") or "").strip()
    if env_pid.isdigit() and verified(int(env_pid)):
        return int(env_pid)
    for pid in lineage:
        try:
            cmd = proc_cmdline(pid) or []
        except ProcVisibilityError:
            continue
        if _is_hermes_cmd(cmd) and "gateway" not in cmd[:4] and verified(pid):
            return pid
    return os.getpid()


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

def owner_from_env(task_id: str, pid: Optional[int] = None) -> dict:
    """Lease owner identity of this run; the task id must match the worker's env when present."""
    env_task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if env_task and env_task != task_id:
        raise LeaseError(f"this terminal belongs to card {env_task}, not {task_id}")
    anchor = anchor_pid(pid, task_id)
    return {
        "board": (os.environ.get("HERMES_KANBAN_BOARD") or "default").strip(),
        "task": task_id,
        "run_id": (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip() or None,
        "pid": anchor,
        "pid_start": proc_start_ticks(anchor),
        "host": socket.gethostname(),
    }


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


def owner_alive(owner: dict) -> Optional[bool]:
    pid = owner.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if owner.get("host") and owner["host"] != socket.gethostname():
        return None  # another host: not decidable here
    return proc_alive(pid, owner.get("pid_start"))


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
    if not helper.startswith("/") or helper != os.path.normpath(helper):
        raise LeaseError(f"scanner helper '{helper}' must be a normalized absolute path")
    try:
        st = os.stat(helper)
    except OSError as exc:
        raise LeaseError(f"scanner helper '{helper}' is not accessible: {exc.strerror}")
    if not stat.S_ISREG(st.st_mode) or not (st.st_mode & stat.S_IXUSR):
        raise LeaseError(f"scanner helper '{helper}' is not an executable regular file")
    if st.st_uid != 0 or (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise LeaseError(f"scanner helper '{helper}' must be owned by root and not group/world writable")
    return list(argv)


def register_scanner(checkout: str, argv: list, git: GitRunner = default_git) -> dict:
    identity = repo_identity(checkout, git)
    argv = validate_scanner_argv(argv)
    registry = scanner_registry()
    registry[identity["root"]] = argv
    _private_dir(state_root())
    _atomic_replace(state_root(), scanner_registry_path().name, registry)
    return {"checkout": identity["root"], "scanner": argv}


def default_scan_runner(argv: list) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "HOME", "USER")}
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env,
                          cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL)


def parse_scan_output(returncode: int, stdout: str, stderr: str = "") -> dict:
    matches = []
    for line in (stdout or "").splitlines():
        m = MATCH_RE.match(line.strip())
        if m:
            matches.append({"pid": int(m.group(1)), "cwd": m.group(2).strip()})
    if returncode == 0:
        status = "complete" if not matches else "complete"
    elif returncode == 1:
        status = "complete"
    else:
        status = "incomplete"
    tail = "\n".join((stdout or "").splitlines()[-3:] + (stderr or "").splitlines()[-3:])[:600]
    return {"status": status, "matches": matches, "returncode": returncode, "tail": tail}


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
    try:
        r = runner(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "incomplete", "matches": [], "returncode": 2, "tail": str(exc)[:300],
                "scanner": " ".join(shlex.quote(a) for a in argv)}
    out = parse_scan_output(r.returncode, r.stdout or "", r.stderr or "")
    out["scanner"] = " ".join(shlex.quote(a) for a in argv)
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


def classify_cmdline(cmd: list) -> tuple:
    """(kind, blocking, detail) for a command line of a process outside our chain, from the
    explicit sets above. Anything not listed is unknown and blocks (fail closed)."""
    if not cmd:
        return "unknown", True, "empty command line"
    sub = _git_subcommand(cmd)
    if Path(cmd[0]).name == "git":
        if sub in MUTATING_GIT:
            return "git-mutation", True, f"git {sub} in progress"
        if sub in READONLY_GIT:
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
        return "dev-server", False, f"dev server / watcher ({name})"
    if name in INTERACTIVE_SHELLS:
        args = cmd[1:]
        if all(a in IDLE_SHELL_OPTIONS for a in args):
            return "shell", False, f"idle interactive shell ({name})"
        return "unknown", True, f"shell with arguments ({name} {' '.join(args)[:80]}): not provably idle"
    return "unknown", True, f"{name or '?'}: not a known passive tool; treated as a possible writer"


def classify_process(pid: int, self_pid: int, anchors: Iterable[int], self_chain: set) -> dict:
    """One scanner match -> {"pid", "kind", "blocking", "detail"}. *self_chain* is the
    ancestry up to the verified anchor (see :func:`self_chain`), never beyond it."""
    anchors = set(anchors)
    if pid == self_pid or pid in self_chain or pid in anchors:
        return {"pid": pid, "kind": "self-chain", "blocking": False, "detail": "this coordinator's own process chain"}
    try:
        cmd = proc_cmdline(pid)
        if not cmd:  # None: gone; []: zombie / exiting (the kernel drops cmdline first)
            return {"pid": pid, "kind": "gone", "blocking": False, "detail": "exited before inspection"}
        lineage = ancestors(pid)
        if anchors & set(lineage) or self_pid in lineage:
            return {"pid": pid, "kind": "own-descendant", "blocking": False,
                    "detail": f"descends from this coordinator ({Path(cmd[0]).name if cmd else '?'})"}
        kind, blocking, detail = classify_cmdline(cmd)
        return {"pid": pid, "kind": kind, "blocking": blocking, "detail": detail}
    except ProcVisibilityError as exc:
        return {"pid": pid, "kind": "unknown", "blocking": True, "detail": f"not inspectable: {exc}"}


def classify_scan(scan: dict, self_pid: Optional[int] = None, anchors: Iterable[int] = ()) -> dict:
    self_pid = os.getpid() if self_pid is None else self_pid
    chain = self_chain(self_pid, anchors)
    rows = [classify_process(m["pid"], self_pid, anchors, chain) | {"cwd": m["cwd"]} for m in scan.get("matches", [])]
    blocking = [r for r in rows if r["blocking"]]
    if scan.get("status") != "complete":
        blocking.append({"pid": None, "kind": "unknown", "blocking": True, "cwd": "",
                         "detail": f"scan incomplete ({scan.get('scanner', '?')}): {scan.get('tail', '')}"})
    return {"scanner": scan.get("scanner"), "status": scan.get("status"), "processes": rows,
            "blocking": blocking, "ok": not blocking}


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


def worktree_mode(root: str, rel: str) -> Optional[str]:
    """Git-style mode of the path in the working tree: 120000 symlink, 040000 directory,
    100755 executable, 100644 regular; None when absent."""
    try:
        st = os.lstat(Path(root, rel))
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return "120000"
    if stat.S_ISDIR(st.st_mode):
        return "040000"
    return "100755" if st.st_mode & stat.S_IXUSR else "100644"


def hash_worktree_path(root: str, rel: str) -> Optional[str]:
    full = Path(root, rel)
    try:
        if full.is_symlink():
            return "symlink:" + hashlib.sha256(os.readlink(full).encode("utf-8", "replace")).hexdigest()
        if full.is_dir():
            return "dir"
        with open(full, "rb") as fh:
            h = hashlib.sha256()
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None


def index_entry(git: GitRunner, root: str, rel: str) -> tuple:
    """(mode, object id) of *rel* in the index, or (None, None) when it is not staged/tracked."""
    r = git(["ls-files", "-s", "-z", "--", rel], root)
    if r.returncode != 0 or not (r.stdout or "").strip("\0").strip():
        return None, None
    record = (r.stdout or "").split("\0")[0]
    meta = record.split("\t", 1)[0].split()
    if len(meta) < 2:
        return None, None
    return meta[0], meta[1]


def index_oid(git: GitRunner, root: str, rel: str) -> Optional[str]:
    return index_entry(git, root, rel)[1]


ENTRY_IDENTITY = ("status", "worktree_sha256", "worktree_mode", "index_oid", "index_mode", "origin")


def snapshot_dirty(git: GitRunner, identity: dict) -> dict:
    """Full identity of every dirty path: status code, worktree content hash and mode, index
    object and mode, rename origin. Hashes only, never contents."""
    entries = {}
    for code, rel, origin in dirty_paths(git, identity["root"]):
        mode, oid = index_entry(git, identity["root"], rel)
        entries[rel] = {"status": code, "worktree_sha256": hash_worktree_path(identity["root"], rel),
                        "worktree_mode": worktree_mode(identity["root"], rel), "index_oid": oid, "index_mode": mode,
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
    owner = owner or owner_from_env(task_id)
    lease = acquire_lease(identity, owner, renew_only=True)
    if lease["status"] != "renewed":
        raise LeaseError(f"checkpoint needs this run's live lease on the checkout ({lease['status']}: {lease['reason']}); "
                         "run preflight first")
    scan = classify_scan(run_scan(identity["root"], scan_runner, registry), self_pid, anchors=[owner["pid"]])
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


def adopt_manifest(task_id: str, identity: dict, git: GitRunner, source_path: str) -> dict:
    """Bootstrap from an explicit, operator-authorized manifest: `{path: sha256}` or this
    module's own format. Every listed path must exist with that exact content now; dirty
    paths not listed stay unknown (preflight reports them). Paths are matched literally."""
    data = json.loads(Path(source_path).read_text(encoding="utf-8"))
    listed = data.get("entries") if isinstance(data, dict) and isinstance(data.get("entries"), dict) else data
    if not isinstance(listed, dict) or not listed:
        raise LeaseError("manifest must be a non-empty JSON object of path -> sha256 (or this module's format)")
    current = snapshot_dirty(git, identity)
    entries, mismatched, missing = {}, [], []
    for rel, value in listed.items():
        want = value.get("worktree_sha256") if isinstance(value, dict) else value
        if rel not in current:
            missing.append(rel)
            continue
        have = current[rel]["worktree_sha256"]
        if want != have:
            mismatched.append(rel)
            continue
        entries[rel] = current[rel]
    if mismatched or missing:
        raise LeaseError("manifest does not match the checkout; not adopted. "
                         + (f"content differs: {', '.join(mismatched[:5])}. " if mismatched else "")
                         + (f"not dirty here: {', '.join(missing[:5])}." if missing else ""))
    payload = write_manifest(task_id, identity, entries, f"adopt:{Path(source_path).name}")
    drop_baseline(task_id)  # provenance changed: the next checkpoint needs a fresh passing preflight
    payload["unlisted_dirty"] = sorted(set(current) - set(entries))
    return payload


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
    owner = owner or owner_from_env(task_id)
    lease = acquire_lease(identity, owner, ttl)
    checks.append({"check": "lease", "ok": lease["status"] != "conflict", "detail": f"{lease['status']}: {lease['reason']}"})
    scan = classify_scan(run_scan(identity["root"], scan_runner, registry), self_pid, anchors=[owner["pid"]])
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
    pf.add_argument("--owner-pid", type=int, default=None, help="process that stands for this run (default: the worker)")
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
    rs = sub.add_parser("register-scanner", help="register the privileged CWD scanner for a checkout")
    rs.add_argument("checkout")
    rs.add_argument("argv", nargs=argparse.REMAINDER, help="-- sudo -n /absolute/helper")
    st = sub.add_parser("status", help="lease and scanner of a checkout")
    common(st, task=False)
    return p


def emit(result, as_json: bool = True) -> None:
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


def cli_owner(task_id: str, explicit_pid: Optional[int] = None) -> dict:
    """Lease owner for the lease-taking CLI commands. Without --owner-pid the anchor must be a
    verified supervising worker: falling back to this short-lived CLI process would take a lease
    that dies with it, so the next checkpoint could never renew it. Refuse that up front."""
    owner = owner_from_env(task_id, explicit_pid)
    if explicit_pid is None and owner["pid"] == os.getpid():
        raise LeaseError(f"no verified supervising Hermes worker for card {task_id} in this process's ancestry "
                         "(HERMES_KANBAN_SUPERVISED_WORKER_PID unset/unverified, or the worker's HERMES_KANBAN_TASK "
                         "differs); refusing a lease owned by this short-lived CLI process. Run from the card's "
                         "worker terminal or pass --owner-pid <long-lived pid that stands for this run>")
    return owner


def main(argv=None, git: GitRunner = default_git, scan_runner: ScanRunner = default_scan_runner) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "preflight":
            owner = cli_owner(args.task, args.owner_pid)
            result = preflight(args.task, args.checkout, args.repo, args.branch, args.base, git=git,
                               scan_runner=scan_runner, owner=owner, require_clean=args.require_clean)
            emit(result)
            return result["exit"]
        identity = None
        if args.cmd in ("checkpoint", "adopt", "release"):
            identity = repo_identity(args.checkout, git)
            explicit = getattr(args, "owner_pid", None)
            # refuses another card's terminal; checkpoint also refuses a self-anchored owner
            owner = cli_owner(args.task, explicit) if args.cmd == "checkpoint" else owner_from_env(args.task, explicit)
        if args.cmd == "checkpoint":
            emit(checkpoint(args.task, identity, git, owner=owner, scan_runner=scan_runner))
            return 0
        if args.cmd == "adopt":
            emit(adopt_manifest(args.task, identity, git, args.manifest))
            return 0
        if args.cmd == "release":
            result = release_lease(identity, owner)
            emit(result)
            return 0 if result["status"] in ("released", "none") else 2
        if args.cmd == "register-scanner":
            scanner = [a for a in args.argv if a != "--"]
            emit(register_scanner(args.checkout, scanner, git))
            return 0
        emit(lease_status(args.checkout, git))
        return 0
    except LeaseError as exc:
        print(f"repo-lease: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
