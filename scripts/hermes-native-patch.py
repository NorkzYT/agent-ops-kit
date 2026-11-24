#!/usr/bin/env python3
"""hermes-native-patch.py -- offline, read-only verification of a pinned native Hermes patch artifact.

Each artifact under hermes/native-patches/<name>/ is ONE format-patch file plus a manifest
(schema hermes-native-patch/v1) that pins the upstream base commit and tree, the patch SHA-256
and the tree base + patch must produce. This helper only reads that artifact directory:

  verify [--dir DIR]                manifest gates + single regular patch file + exact SHA-256
                                    + static patch shape (header names the reviewed commit,
                                    no CR, no signature, no binary hunks)
  verify --emit-patch [--dir DIR]   the same checks; on success write the exact bytes that were
                                    hashed to stdout (report goes to stderr), so a caller can pipe
                                    them without re-reading the file

It takes no checkout argument, runs no git (or any other) subprocess, writes no files and no
git objects, and never touches a Hermes home, the installed runtime, a live config or the
gateway. It cannot apply, install or enable anything. The base + patch = result_tree fact is
NOT re-derived offline; it is the recorded verification in the manifest.

The artifact directory is opened once with O_NOFOLLOW, which refuses a symlink only as the
final path component (symlinked parent directories of --dir are still followed). manifest.json and the
patch are opened relative to that descriptor with O_NOFOLLOW, must be regular files and are read
once; every check runs on those in-memory bytes. The manifest name must be a plain slug equal to
the artifact directory's name.
Exit: 0 verified, 1 verification failed, 2 usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = KIT / "hermes" / "native-patches" / "aux-model-floor"
SCHEMA = "hermes-native-patch/v1"
FULL_ID = re.compile(r"[0-9a-f]{40}")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.patch")
MAX_BYTES = 8 * 1024 * 1024
ALLOWED_ENTRIES = {"manifest.json", "README.md"}


class Fail(Exception):
    pass


def _reject_constant(name: str):
    raise ValueError(f"non-finite number {name}")


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def parse_manifest(data: bytes) -> dict:
    try:
        m = json.loads(data.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Fail(f"manifest unreadable: {exc}")
    if not isinstance(m, dict):
        raise Fail("manifest is not a JSON object")
    errs = []
    if not isinstance(m.get("name"), str) or not SLUG.fullmatch(m["name"]):
        errs.append("name is not a plain lowercase slug (a-z, 0-9, -; at most 64)")
    if m.get("schema") != SCHEMA:
        errs.append(f"schema != {SCHEMA}")
    if m.get("enabled") is not False:
        errs.append("enabled must be false (the kit never activates a native patch)")
    if m.get("auto_apply") is not False:
        errs.append("auto_apply must be false")
    review = m.get("review") if isinstance(m.get("review"), dict) else {}
    if review.get("deployment_approved") is not False:
        errs.append("review.deployment_approved must be false")
    up, pt, rs = (m.get(k) if isinstance(m.get(k), dict) else {} for k in ("upstream", "patch", "reviewed_source"))
    for label, val in (("upstream.base", up.get("base")), ("upstream.base_tree", up.get("base_tree")),
                       ("patch.result_tree", pt.get("result_tree")), ("reviewed_source.commit", rs.get("commit")),
                       ("reviewed_source.tree", rs.get("tree"))):
        if not isinstance(val, str) or not FULL_ID.fullmatch(val):
            errs.append(f"{label} is not a full lowercase object id")
    if not isinstance(pt.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", pt["sha256"]):
        errs.append("patch.sha256 is not a lowercase SHA-256")
    if rs.get("tree") != pt.get("result_tree"):
        errs.append("reviewed_source.tree != patch.result_tree")
    if rs.get("parent") != up.get("base"):
        errs.append("reviewed_source.parent != upstream.base")
    if not isinstance(pt.get("file"), str) or not SAFE_NAME.fullmatch(pt["file"]):
        errs.append("patch.file is not a plain *.patch name")
    if errs:
        raise Fail("manifest invalid: " + "; ".join(errs))
    return m


def check_dir_name(m: dict, pdir: str) -> None:
    base = os.path.basename(os.path.normpath(pdir))
    if m["name"] != base:
        raise Fail(f"manifest name {m['name']!r} != artifact directory name {base!r}")


def check_patch(m: dict, entries: list[str], patch: bytes) -> list[str]:
    """All checks on in-memory bytes. Returns the paths the patch touches."""
    name = m["patch"]["file"]
    others = sorted(e for e in entries if e.endswith(".patch") and e != name)
    if others:
        raise Fail(f"unexpected extra patch files (single-patch artifact): {', '.join(others)}")
    unknown = sorted(e for e in entries if e != name and e not in ALLOWED_ENTRIES and not e.endswith(".patch"))
    if unknown:
        raise Fail(f"unexpected files in artifact directory: {', '.join(unknown)}")
    digest = hashlib.sha256(patch).hexdigest()
    if digest != m["patch"]["sha256"]:
        raise Fail(f"patch sha256 {digest} != manifest {m['patch']['sha256']}")
    if b"\r" in patch:
        raise Fail("patch contains CR bytes")
    header = f"From {m['reviewed_source']['commit']} Mon Sep 17 00:00:00 2001\n".encode()
    if not patch.startswith(header):
        raise Fail("patch header does not name reviewed_source.commit")
    if re.search(rb"(?m)^From [0-9a-f]{40} Mon Sep 17 00:00:00 2001$", patch[len(header):]):
        raise Fail("more than one commit in the patch (single-patch artifact)")
    if re.search(rb"(?m)^-- \n[0-9]+\.[0-9]+", patch):
        raise Fail("patch carries a git version signature (expected --no-signature)")
    if re.search(rb"(?m)^GIT binary patch$", patch):
        raise Fail("binary hunks are not accepted")
    files = [f.decode("utf-8", "replace") for f in re.findall(rb"(?m)^diff --git a/(\S+) b/\S+$", patch)]
    if not files:
        raise Fail("patch has no file diffs")
    return files


def read_artifact(pdir: str) -> tuple[dict, list[str], bytes]:
    """Read manifest and patch once each through one O_NOFOLLOW directory descriptor."""
    try:
        dfd = os.open(pdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Fail(f"artifact directory unreadable: {exc}")
    try:
        entries = sorted(os.listdir(dfd))

        def slurp(name: str) -> bytes:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            except OSError as exc:
                raise Fail(f"{name}: cannot open as a regular non-symlink file ({exc.strerror})")
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise Fail(f"{name}: not a regular file")
                if st.st_size > MAX_BYTES:
                    raise Fail(f"{name}: larger than {MAX_BYTES} bytes")
                chunks, total = [], 0
                while chunk := os.read(fd, 1 << 16):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise Fail(f"{name}: larger than {MAX_BYTES} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(fd)

        m = parse_manifest(slurp("manifest.json"))
        return m, entries, slurp(m["patch"]["file"])
    finally:
        os.close(dfd)


def verify(pdir: str) -> tuple[dict, list[str], bytes]:
    m, entries, patch = read_artifact(pdir)
    check_dir_name(m, pdir)
    return m, check_patch(m, entries, patch), patch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["verify"])
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="native patch artifact directory (read only)")
    ap.add_argument("--emit-patch", action="store_true",
                    help="on success write the verified patch bytes to stdout (report to stderr)")
    args = ap.parse_args(argv)
    report = sys.stderr if args.emit_patch else sys.stdout
    try:
        m, files, patch = verify(args.dir)
    except Fail as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(f"verified (offline, read-only): {m['name']} {m['patch']['file']} sha256 {m['patch']['sha256']}\n"
          f"  reviewed commit {m['reviewed_source']['commit']} on base {m['upstream']['base']}; "
          f"{len(files)} files; enabled=false auto_apply=false deployment_approved=false\n"
          f"  not re-derived here: base + patch = tree {m['patch']['result_tree']} (recorded in manifest.verification)",
          file=report, flush=True)
    if args.emit_patch:
        sys.stdout.buffer.write(patch)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
