#!/usr/bin/env python3
"""RTK replay pilot: compress captured synthetic command output with a pinned
rtk binary and check nothing decision-relevant was lost.

Boundaries (enforced here, not just documented):
- the binary must match rtk.lock.json binary_sha256, else nothing runs;
- argv comes only from the fixed MODES table with a fixture path inside
  fixtures/; there is no free-form command, no shell, no hook, no proxy;
- private HOME/XDG dirs, RTK_TELEMETRY_DISABLED=1, minimal environment;
- raw fixture, rtk stdout/stderr, exit code/signal are all kept;
- every case (exact keys, types, a path-safe unique id) and the lock are
  validated before anything is written;
- a crash, timeout, exec failure or empty output is "unmeasured", never 0% or
  100% savings;
- producer-exit parity is NOT tested: a replay has no producer process, so
  `producer_exit` is a recorded label from the capture, never compared with
  RTK's exit. Results say `producer_exit_parity: "not-tested"`.

usage: run_pilot.py --rtk <path> --out <dir> [--cases cases.json] [--timeout 20]
exit: 0 all kept, 1 a marker was lost, 2 refused (nothing ran), 3 some case
unmeasured, 4 internal runner error (unexpected; not a verdict)
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

# mode -> (argv after the binary, stdin from fixture?)
MODES = {
    "log-stdin": (["log"], True),
    "diff-stdin": (["diff", "-"], True),
    "summary-cat": (["summary", "cat", "{fixture}"], False),
}


class Refused(Exception):
    pass


CASE_KEYS = {"id", "fixture", "mode", "producer_exit", "must_keep"}
CASES_DOC_KEYS = {"note", "cases"}
# Used as a directory name under --out: no dots, slashes or reserved names.
CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def validate_lock(lock) -> dict:
    if not isinstance(lock, dict) or not isinstance(lock.get("binary_sha256"), str) \
            or not SHA256.fullmatch(lock["binary_sha256"]) or not isinstance(lock.get("release"), str):
        raise Refused("lock must be an object with release and a 64-hex binary_sha256")
    return lock


def validate_cases(doc) -> list:
    """Whole-file check before anything runs or is written."""
    if not isinstance(doc, dict) or set(doc) - CASES_DOC_KEYS or not isinstance(doc.get("cases"), list) or not doc["cases"]:
        raise Refused(f"cases file must be an object with keys {sorted(CASES_DOC_KEYS)} and a non-empty cases list")
    seen = set()
    for i, c in enumerate(doc["cases"]):
        if not isinstance(c, dict) or set(c) != CASE_KEYS:
            got = sorted(c) if isinstance(c, dict) else type(c).__name__
            raise Refused(f"case #{i}: keys must be exactly {sorted(CASE_KEYS)} (got {got})")
        cid = c["id"]
        if not isinstance(cid, str) or not CASE_ID.fullmatch(cid):
            raise Refused(f"case #{i}: id {cid!r} must match {CASE_ID.pattern}")
        if cid in seen:
            raise Refused(f"case #{i}: duplicate id {cid!r}")
        seen.add(cid)
        if not isinstance(c["fixture"], str):
            raise Refused(f"case {cid}: fixture must be a string")
        pe = c["producer_exit"]
        if not isinstance(pe, int) or isinstance(pe, bool) or not 0 <= pe <= 255:
            raise Refused(f"case {cid}: producer_exit must be an integer 0-255")
        mk = c["must_keep"]
        if not isinstance(mk, list) or not mk or not all(isinstance(m, str) and m for m in mk):
            raise Refused(f"case {cid}: must_keep must be a non-empty list of non-empty strings")
    return doc["cases"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_binary(rtk: Path, lock: dict) -> str:
    if not rtk.is_file():
        raise Refused(f"rtk binary not found: {rtk}")
    digest = sha256(rtk)
    if digest != lock["binary_sha256"]:
        raise Refused(f"rtk sha256 {digest} != pinned {lock['binary_sha256']}")
    return digest


def build_argv(rtk: Path, case: dict):
    mode = case.get("mode")
    if mode not in MODES:
        raise Refused(f"case {case.get('id')}: mode {mode!r} is not allowlisted")
    fixture = (HERE / case["fixture"]).resolve()
    if FIXTURES not in fixture.parents or not fixture.is_file():
        raise Refused(f"case {case.get('id')}: fixture must be a file under fixtures/: {case['fixture']}")
    args, stdin = MODES[mode]
    return [str(rtk)] + [a.replace("{fixture}", str(fixture)) for a in args], fixture, stdin


def pilot_env(home: Path) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "RTK_TELEMETRY_DISABLED": "1",
    }


def evaluate(case: dict, raw: bytes, res: dict) -> dict:
    """Pure: classify one run. Savings only when the run is measurable."""
    out = res["stdout"]
    text = out.decode("utf-8", "replace")
    measurable = res["status"] == "ok" and len(out) > 0
    lost = [m for m in case["must_keep"] if m not in text] if measurable else None
    return {
        "id": case["id"],
        "status": res["status"],
        "rtk_exit": res["exit"],
        "rtk_signal": res["signal"],
        "producer_exit": case["producer_exit"],
        "producer_exit_parity": "not-tested",
        "raw_bytes": len(raw),
        "rtk_bytes": len(out) if measurable else None,
        "reduction": round(1 - len(out) / len(raw), 4) if measurable and raw else None,
        "lost_markers": lost,
        "verdict": ("unmeasured" if not measurable else "lossy" if lost else "kept"),
    }


def run_case(rtk: Path, case: dict, out_dir: Path, home: Path, timeout: float) -> dict:
    argv, fixture, use_stdin = build_argv(rtk, case)
    raw = fixture.read_bytes()
    case_dir = out_dir / case["id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        p = subprocess.run(argv, input=raw if use_stdin else None,
                           stdin=None if use_stdin else subprocess.DEVNULL,
                           capture_output=True, env=pilot_env(home), cwd=str(home), timeout=timeout)
        rc = p.returncode
        res = {"stdout": p.stdout, "stderr": p.stderr, "exit": rc if rc >= 0 else None,
               "signal": signal.Signals(-rc).name if rc < 0 else None,
               "status": "ok" if rc == 0 else ("crashed" if rc < 0 or rc >= 128 else "error")}
    except subprocess.TimeoutExpired as e:
        res = {"stdout": e.stdout or b"", "stderr": e.stderr or b"", "exit": None, "signal": None, "status": "timeout"}
    except OSError as e:  # exec failed (bad format, permissions): RTK never ran
        res = {"stdout": b"", "stderr": f"exec failed: {e}".encode(), "exit": None, "signal": None, "status": "exec-error"}
    (case_dir / "argv.json").write_text(json.dumps(argv) + "\n")
    (case_dir / "raw.input").write_bytes(raw)
    (case_dir / "rtk.stdout").write_bytes(res["stdout"])
    (case_dir / "rtk.stderr").write_bytes(res["stderr"])
    result = evaluate(case, raw, res)
    result["seconds"] = round(time.time() - started, 3)
    (case_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None) -> int:
    try:
        return _main(argv)
    except Exception as e:  # noqa: BLE001 - must not be confused with exit 1 (lossy)
        print(f"internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 4


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rtk", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--cases", type=Path, default=HERE / "cases.json")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--lock", type=Path, default=HERE / "rtk.lock.json", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        try:
            lock = validate_lock(json.loads(a.lock.read_text()))
            doc = json.loads(a.cases.read_text())
        except (OSError, ValueError) as e:
            raise Refused(f"cannot read lock/cases: {e}") from e
        digest = verify_binary(a.rtk.resolve(), lock)
        cases = validate_cases(doc)
        for c in cases:
            build_argv(a.rtk.resolve(), c)  # refuse the whole run before executing anything
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    a.out.mkdir(parents=True, exist_ok=True)
    home = a.out / ".rtk-home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(mode=0o700)
    results = [run_case(a.rtk.resolve(), c, a.out, home, a.timeout) for c in cases]
    summary = {
        "rtk_release": lock["release"], "binary_sha256": digest,
        "producer_exit_parity": "not-tested",
        "cases": results,
        "measured": sum(r["verdict"] != "unmeasured" for r in results),
        "lossy": [r["id"] for r in results if r["verdict"] == "lossy"],
    }
    (a.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for r in results:
        print(f"{r['id']:<14} {r['verdict']:<10} status={r['status']} exit={r['rtk_exit']} signal={r['rtk_signal']} "
              f"raw={r['raw_bytes']} rtk={r['rtk_bytes']} reduction={r['reduction']} lost={r['lost_markers']}")
    # 0: all measured and nothing lost; 1: something lossy; 3: nothing lossy but some unmeasured.
    if summary["lossy"]:
        return 1
    return 0 if summary["measured"] == len(results) else 3


if __name__ == "__main__":
    sys.exit(main())
