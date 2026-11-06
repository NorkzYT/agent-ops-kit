#!/usr/bin/env python3
"""Narrow, no-restart rollout of the Discord->Kanban dev workflow files.

Copies exactly these tracked files from this checkout into an installed Hermes home:

  hermes/skills/kanban-workflow/{SKILL.md, scripts/browserstack.py, scripts/completion.py,
    scripts/workflow.py, scripts/repo_lease.py}
                                     -> <home>/skills/agent-ops-kit/kanban-workflow/...
  hermes/profiles/coder/SOUL.md      -> <coder-home>/SOUL.md

The skill files are a fixed allowlist, not a directory walk: an untracked or
stray file under the skill directory is never copied.

<coder-home> is --coder-home, else <home>/profiles/coder when that directory
already exists; otherwise the SOUL is skipped. Nothing else is read or written:
no config.yaml, .env, honcho.json, plugins, memories or the default SOUL.md.
Files already in the destination that are not in the source list are left
alone (no removal).

--plan (default) only inspects. --apply validates every destination first and
writes nothing if any check fails; then each changed file gets a unique backup
of the old content and an atomic same-directory replace. Every directory from
the Hermes root down (and every replaced file) must be a real directory/regular
file owned by the current uid and not world-writable; symlinks and special
files are refused. Output is paths, sha256 and status only.

Config keys (approvals, security) are not this script's job: set them with
`hermes config set` (docs/hermes-autonomous-development.md, Rollout).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL_SRC = Path("hermes/skills/kanban-workflow")
SKILL_DEST = ("skills", "agent-ops-kit", "kanban-workflow")
CODER_SOUL_SRC = Path("hermes/profiles/coder/SOUL.md")
# The tracked skill files this helper deploys. Anything else under SKILL_SRC is ignored.
SKILL_FILES = ("SKILL.md", "scripts/browserstack.py", "scripts/completion.py", "scripts/workflow.py",
               "scripts/repo_lease.py")


class Refused(Exception):
    pass


@dataclass
class Item:
    src: Path                 # absolute source path
    parts: tuple[str, ...]    # destination path components below the Hermes root
    data: bytes = b""
    mode: int = 0o644
    status: str = ""
    old_sha: str = ""
    backup: str = ""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_items(repo: Path, files: tuple[str, ...] = SKILL_FILES) -> list[Item]:
    """One item per allowlisted skill file; a missing file fails at read time."""
    root = repo / SKILL_SRC
    items = []
    for rel in files:
        parts = Path(rel).parts
        if not parts or Path(rel).is_absolute() or ".." in parts:
            raise Refused(f"source list entry {rel!r} is not a plain relative path")
        for i in range(len(parts)):  # the skill dir and every directory on the way: no symlinks
            d = root.joinpath(*parts[:i])
            if os.path.islink(d):
                raise Refused(f"source {d} is a symlink")
        items.append(Item(root.joinpath(*parts), SKILL_DEST + parts))
    return items


def read_source(item: Item) -> None:
    fd = os.open(item.src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"source {item.src} is not a regular file")
        with os.fdopen(os.dup(fd), "rb") as fh:
            item.data = fh.read()
        item.mode = 0o755 if st.st_mode & 0o100 else 0o644
    finally:
        os.close(fd)


def check_owned(st: os.stat_result, what: str, want) -> None:
    if not want(st.st_mode):
        raise Refused(f"{what} is a symlink or wrong file type")
    if st.st_uid != os.getuid():
        raise Refused(f"{what} is owned by uid {st.st_uid}, not {os.getuid()}")
    if st.st_mode & stat.S_IWOTH:
        raise Refused(f"{what} is world-writable")


def open_dir(name: str, dir_fd: int | None, what: str) -> int:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP / ENOTDIR: symlink or not a directory
        raise Refused(f"{what} is a symlink or not a directory ({exc.strerror})") from None
    try:
        check_owned(os.fstat(fd), what, stat.S_ISDIR)
    except Refused:
        os.close(fd)
        raise
    return fd


class Dest:
    """Walks destination directories by fd from the Hermes root, never following links."""

    def __init__(self, home: Path):
        self.home = home
        self.root_fd = open_dir(str(home), None, str(home))
        self.fds: dict[tuple[str, ...], int] = {(): self.root_fd}

    def close(self) -> None:
        for fd in self.fds.values():
            os.close(fd)

    def dir_fd(self, parts: tuple[str, ...], create: bool) -> int | None:
        """fd of home/parts, or None when absent (and create is False)."""
        if parts in self.fds:
            return self.fds[parts]
        parent = self.dir_fd(parts[:-1], create)
        if parent is None:
            return None
        what = str(self.home.joinpath(*parts))
        try:
            fd = open_dir(parts[-1], parent, what)
        except FileNotFoundError:
            if not create:
                return None
            os.mkdir(parts[-1], 0o755, dir_fd=parent)
            fd = open_dir(parts[-1], parent, what)
        self.fds[parts] = fd
        return fd

    def read_existing(self, dir_fd: int, name: str, what: str) -> bytes | None:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise Refused(f"{what} is a symlink or unreadable ({exc.strerror})") from None
        try:
            check_owned(os.fstat(fd), what, stat.S_ISREG)
            with os.fdopen(os.dup(fd), "rb") as fh:
                return fh.read()
        finally:
            os.close(fd)


def inspect(dest: Dest, item: Item) -> None:
    dirs, name = item.parts[:-1], item.parts[-1]
    fd = dest.dir_fd(dirs, create=False)
    if fd is None:
        item.status = "create"  # some directory on the way is absent; apply creates it
        return
    old = dest.read_existing(fd, name, str(dest.home.joinpath(*item.parts)))
    if old is None:
        item.status = "create"
    elif old == item.data:
        item.status, item.old_sha = "unchanged", sha256(old)
    else:
        item.status, item.old_sha = "update", sha256(old)


def write_new(dir_fd: int, name: str, data: bytes, mode: int) -> str:
    """Create name exclusively in dir_fd; returns the name."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return name


def apply_item(dest: Dest, item: Item, stamp: str) -> None:
    dirs, name = item.parts[:-1], item.parts[-1]
    fd = dest.dir_fd(dirs, create=True)
    what = str(dest.home.joinpath(*item.parts))
    old = dest.read_existing(fd, name, what)  # re-checked right before the write
    if old == item.data:
        item.status = "unchanged"
        return
    if old is not None:
        bak = f"{name}.bak-{stamp}-{secrets.token_hex(4)}"
        write_new(fd, bak, old, 0o600)
        item.backup = str(dest.home.joinpath(*dirs, bak))
        item.status = "updated"
    else:
        item.status = "created"
    tmp = write_new(fd, f".{name}.tmp-{secrets.token_hex(8)}", item.data, item.mode)
    try:
        os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
    except BaseException:
        os.unlink(tmp, dir_fd=fd)
        raise
    os.fsync(fd)


def coder_parts(home: Path, coder_home: Path | None) -> tuple[str, ...] | None:
    if coder_home is None:
        default = home / "profiles" / "coder"
        return ("profiles", "coder") if os.path.isdir(default) and not os.path.islink(default) else None
    rel = os.path.relpath(os.path.abspath(coder_home), os.path.abspath(home))
    if rel == "." or rel.startswith(".."):
        raise Refused(f"--coder-home {coder_home} must be a directory below {home}")
    parts = tuple(Path(rel).parts)
    if parts == () or not os.path.lexists(coder_home):
        raise Refused(f"--coder-home {coder_home} does not exist")
    return parts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="inspect only (default)")
    mode.add_argument("--apply", action="store_true", help="write the changed files")
    ap.add_argument("--hermes-home", type=Path,
                    default=Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes"))
    ap.add_argument("--coder-home", type=Path, default=None,
                    help="coder profile dir below the Hermes home (default: <home>/profiles/coder if it exists)")
    args = ap.parse_args(argv)
    home = Path(os.path.abspath(args.hermes_home))

    try:
        items = source_items(REPO)
        parts = coder_parts(home, args.coder_home)
        if parts is not None:
            items.append(Item(REPO / CODER_SOUL_SRC, parts + ("SOUL.md",)))
        else:
            print(f"skip {home / 'profiles' / 'coder' / 'SOUL.md'} (no coder profile)")
        for item in items:
            read_source(item)
        dest = Dest(home)
    except FileNotFoundError as exc:
        print(f"refused: {exc.filename} does not exist", file=sys.stderr)
        return 1
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    try:
        try:
            for item in items:
                inspect(dest, item)
        except Refused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        if args.apply:
            stamp = time.strftime("%Y%m%dT%H%M%S")
            try:
                for item in items:
                    if item.status != "unchanged":
                        apply_item(dest, item, stamp)
            except Refused as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
        for item in items:
            line = f"{item.status} {home.joinpath(*item.parts)} sha256={sha256(item.data)}"
            if item.old_sha and item.status != "unchanged":
                line += f" was={item.old_sha}"
            if item.backup:
                line += f" backup={item.backup}"
            print(line)
        if not args.apply:
            changes = sum(i.status != "unchanged" for i in items)
            print(f"plan: {changes} to write, {len(items) - changes} unchanged; nothing written (--apply to write)")
    finally:
        dest.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
