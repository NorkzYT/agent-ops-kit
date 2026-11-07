#!/usr/bin/env python3
"""Completion policy for the kanban-workflow coordinator (library; the CLI is
`workflow.py completion ...`).

The coordinator (profile `default`) stays responsible for a request until every stage
below is met, not until a coding delegate returns:

    understand -> plan -> implement -> test (debug, fix, retest) -> review -> commit -> push
      = implementation handoff: the code card may complete; its validation children start
    -> validate (every lane passed the exact pushed SHA) -> complete (independent review
      approved that SHA and the report gate passed) = final completion

Everything here is computed from durable records (type `completion`, written through
workflow.py's ledger-first `put_record` onto the request's own cards) plus the current
source identity; nothing is kept only in a prompt.

Source identity: HEAD, plus one content id over every tracked and untracked (not
ignored) path: git mode (exec bit, symlink 120000, gitlink 160000) and SHA-256 of the
bytes (a symlink's target string, never followed; a submodule's index object). Only
digests are stored, never file contents or paths. Evidence counts only while its
content id equals the current one: a later edit, rebase or checkout makes it stale and
the stage unmet again. A commit of the same bytes keeps it valid. Ignored files (build
output, local env files) are outside the identity. An index entry flagged
assume-unchanged or skip-worktree hides its changes from `git status`, thus the tree is
never clean while one exists.

Recovery: a routine failure (compile, lint, test, toolchain, dependency, disposable DB,
Docker, timed-out delegate, stale session) is recorded with a failure signature, a
diagnosis and the next action. An identical (signature, diagnosis, action) repeat is
refused: change the diagnosis or the approach. Routine attempts per stage are bounded
(MAX_RECOVERY_PER_STAGE since the stage was last met: test = every required check passing
on one source content, review = a passing full-diff review, the other stages = a
handoff or final completion; an unrelated pass never resets them); exhaustion yields the
only routine block reason (`recovery-exhausted:`). Approval, device, capability, conflict
and dependency stops are genuine and map to the existing block prefixes. A proposed block
reason with any other prefix is refused as routine. A stop stays open until its own stage
is met after it, a handoff, or an explicit `resolve` record; an approval stop only by
`resolve`.

Ownership: these records never take, renew past or release another run's lease; a
delegate's exit or a timeout is a delegate state, not an owner change. A lease is
cooperative same-uid evidence, not OS containment, and is reported as such.
Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Callable, Optional

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import repo_lease as rl  # noqa: E402

RTYPE = "completion"
STAGES = ("understand", "plan", "implement", "test", "review", "commit", "push", "validate", "complete")
HANDOFF_STAGES = STAGES[:7]  # met: the code card may complete (implementation handoff)
FINAL_STAGES = STAGES[7:]    # met: the request is complete (cross-platform validation + review + gate)
EVENTS = ("criteria", "plan", "delegate", "evidence", "recovery", "block", "block-refused", "resolve",
          "owner-conflict", "handoff", "resume", "complete")
EVIDENCE_KINDS = ("test", "review")
DELEGATE_STATES = ("started", "complete", "partial", "failed", "interrupted")
DEFAULT_CHECKS = ("tests",)
MAX_RECOVERY_PER_STAGE = 8
# Genuine stop classes -> the block prefix the existing workflow and operator runbook use.
STOP_PREFIX = {"approval": "human-approval", "device": "device", "capability": "capability",
               "conflict": "checkout", "dependency": "dependency"}
RECOVERY_CLASSES = ("routine", *STOP_PREFIX)
BLOCK_CLASSES = {prefix: cls for cls, prefix in STOP_PREFIX.items()}
BLOCK_CLASSES["recovery-exhausted"] = "recovery-exhausted"
CONTAINMENT = "cooperative same-uid lease and process scan; not OS isolation"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")

GitRunner = Callable[[list, str], object]


class CompletionError(ValueError):
    pass


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def short(digest: str) -> str:
    return (digest or "")[:12] or "none"


# --------------------------------------------------------- source identity ---

def _git_z(git: GitRunner, argv: list, root: str) -> list:
    r = git(argv, root)
    if r.returncode != 0:
        raise CompletionError(f"git {' '.join(argv[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return [p for p in (r.stdout or "").split("\0") if p]


def _path_digest(root: str, rel: str) -> tuple:
    """(git-style mode, digest) of a worktree path, never following a symlink and never
    opening anything but a regular file."""
    full = os.path.join(root, rel)
    parent = os.path.realpath(os.path.dirname(full))
    if parent != root and not parent.startswith(root.rstrip("/") + "/"):
        return "outside", "outside:a parent directory leaves the checkout"  # never read through it
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return "000000", "deleted"
    if stat.S_ISLNK(st.st_mode):
        return "120000", "symlink:" + hashlib.sha256(os.readlink(full).encode("utf-8", "surrogateescape")).hexdigest()
    if stat.S_ISDIR(st.st_mode):
        return "040000", "dir"
    if not stat.S_ISREG(st.st_mode):
        return "special", f"special:{stat.S_IFMT(st.st_mode):o}"
    h = hashlib.sha256()
    fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return ("100755" if st.st_mode & stat.S_IXUSR else "100644"), h.hexdigest()


def source_identity(root: str, git: GitRunner) -> dict:
    """HEAD + content id of the working tree at *root* (its top level). Digests only."""
    root = os.path.realpath(root)
    r = git(["rev-parse", "HEAD"], root)
    head = (r.stdout or "").strip() if r.returncode == 0 else ""
    r = git(["branch", "--show-current"], root)
    branch = (r.stdout or "").strip() if r.returncode == 0 else ""
    entries = {}
    for record in _git_z(git, ["ls-files", "-z", "-s"], root):
        meta, _, rel = record.partition("\t")
        fields = meta.split()
        if len(fields) >= 2 and fields[0] == "160000":
            entries[rel] = ("160000", "gitlink:" + fields[1])
        elif rel not in entries:
            entries[rel] = _path_digest(root, rel)
    for rel in _git_z(git, ["ls-files", "-z", "--others", "--exclude-standard"], root):
        entries.setdefault(rel, _path_digest(root, rel))
    h = hashlib.sha256()
    for rel in sorted(entries):
        mode, digest = entries[rel]
        h.update(f"{mode} {digest} ".encode() + rel.encode("utf-8", "surrogateescape") + b"\0")
    dirty = rl.dirty_paths(git, root)
    flagged = flagged_paths(git, root)
    return {"head": head, "branch": branch, "content_id": h.hexdigest(), "clean": not dirty and not flagged,
            "dirty": len(dirty), "flagged": len(flagged), "files": len(entries)}


def flagged_paths(git: GitRunner, root: str) -> list:
    """Index entries flagged assume-unchanged (lowercase tag) or skip-worktree (S/s) in
    `git ls-files -v`: git status does not compare them, so their worktree bytes may differ
    from HEAD while the tree looks clean. Fail closed: such a tree is never clean."""
    out = []
    for record in _git_z(git, ["ls-files", "-z", "-v"], root):
        tag, _, rel = record.partition(" ")
        if tag.islower() or tag == "S":
            out.append(rel)
    return out


# ---------------------------------------------------------------- records ---

def of(recs: list, event: str, **match) -> list:
    return [r for r in recs if r.get("event") == event and all(r.get(k) == v for k, v in match.items())]


def last(recs: list, event: str, **match) -> Optional[dict]:
    found = of(recs, event, **match)
    return found[-1] if found else None


def criteria_record(items: list, checks: list, sources: list) -> dict:
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        raise CompletionError("criteria needs at least one acceptance criterion (--item)")
    checks = list(dict.fromkeys(c.strip() for c in (checks or DEFAULT_CHECKS) if c.strip()))
    for c in checks:
        if not NAME_RE.match(c):
            raise CompletionError(f"check name '{c}' must be a short token (letters, digits, ._:/-)")
    return {"event": "criteria", "items": items, "checks": checks, "sources": [s for s in sources if s]}


def plan_record(steps: list) -> dict:
    steps = [s.strip() for s in steps if s and s.strip()]
    if not steps:
        raise CompletionError("plan needs at least one step (--step)")
    return {"event": "plan", "steps": steps}


def delegate_record(delegate_id: str, state: str, summary: str, remaining: str) -> dict:
    if not NAME_RE.match(delegate_id or ""):
        raise CompletionError("delegate --id must be a short token (the delegate_task id, or 'self')")
    if state not in DELEGATE_STATES:
        raise CompletionError(f"delegate --state must be one of {', '.join(DELEGATE_STATES)}")
    if state == "partial" and not (remaining or "").strip():
        raise CompletionError("a partial delegate needs --remaining: what is still not done")
    return {"event": "delegate", "delegate": delegate_id, "state": state, "summary": summary or "",
            "remaining": remaining or ""}


def evidence_record(kind: str, check: str, result: str, command: str, exit_code: Optional[int], counts: dict,
                    source: dict, log_digest: Optional[dict], summary: str) -> dict:
    if kind not in EVIDENCE_KINDS:
        raise CompletionError(f"evidence --kind must be one of {', '.join(EVIDENCE_KINDS)}")
    if not NAME_RE.match(check or ""):
        raise CompletionError("evidence --check must be a short token naming the check (e.g. unit, lint, diff)")
    if result not in ("pass", "fail"):
        raise CompletionError("evidence --result must be pass or fail")
    if kind == "test" and not (command or "").strip():
        raise CompletionError("test evidence needs --command: the exact command that ran")
    if result == "pass" and ((exit_code not in (0, None)) or int(counts.get("failed") or 0) > 0):
        raise CompletionError("a pass needs exit 0 and no failed tests; record it as --result fail")
    if kind == "test" and exit_code is None:
        raise CompletionError("test evidence needs --exit: the command's exit code")
    return {"event": "evidence", "kind": kind, "check": check, "result": result, "command": command or "",
            "exit_code": exit_code, "counts": {k: int(v) for k, v in counts.items() if v is not None},
            "source": {k: source[k] for k in ("head", "content_id", "clean", "branch")},
            "log": log_digest, "summary": summary or ""}


def log_digest(path: str) -> Optional[dict]:
    """SHA-256 and size of a command log; its content is never stored."""
    if not path:
        return None
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise CompletionError(f"--log: {exc}") from exc
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


# --------------------------------------------------------------- recovery ---

def _ts(rec: dict) -> float:
    return float(rec.get("ts") or 0)


def _required_checks(recs: list, ts: float) -> list:
    crit = [r for r in of(recs, "criteria") if _ts(r) <= ts]
    return (crit[-1].get("checks") if crit else None) or list(DEFAULT_CHECKS)


def stage_met_times(recs: list, stage: str) -> list:
    """When *stage* was proven met by records, oldest first. test: every required check's
    latest result on one source content is a pass; review: a passing full-diff review;
    the other implementation stages: a handoff; validate/complete: a final completion. A
    pass of one check while another required check fails is not progress."""
    if stage == "test":
        times, latest = [], {}
        for e in of(recs, "evidence", kind="test"):
            cid = (e.get("source") or {}).get("content_id")
            latest[(e.get("check"), cid)] = e.get("result")
            if all(latest.get((c, cid)) == "pass" for c in _required_checks(recs, _ts(e))):
                times.append(_ts(e))
        return times
    if stage == "review":
        return [_ts(e) for e in of(recs, "evidence", kind="review", check="diff", result="pass")]
    return [_ts(r) for r in of(recs, "handoff" if stage in HANDOFF_STAGES else "complete")]


def _stage_passed_ts(recs: list, stage: str) -> float:
    return max(stage_met_times(recs, stage) or [0.0])


def plan_recovery(recs: list, stage: str, cls: str, signature: str, diagnosis: str, action: str) -> dict:
    """Decision for one recovery attempt: proceed | repeat-refused | exhausted | stop."""
    if stage not in STAGES:
        raise CompletionError(f"--stage must be one of {', '.join(STAGES)}")
    if cls not in RECOVERY_CLASSES:
        raise CompletionError(f"--class must be one of {', '.join(RECOVERY_CLASSES)}")
    for name, value in (("--signature", signature), ("--diagnosis", diagnosis), ("--action", action)):
        if not (value or "").strip():
            raise CompletionError(f"recover needs {name}")
    rec = {"event": "recovery", "stage": stage, "class": cls, "signature": signature.strip(),
           "diagnosis": diagnosis.strip(), "action": action.strip()}
    if cls != "routine":
        rec["decision"] = "stop"
        rec["block_reason"] = f"{STOP_PREFIX[cls]}: {signature.strip()} ({diagnosis.strip()})"
        return rec
    since = _stage_passed_ts(recs, stage)
    prior = [r for r in of(recs, "recovery", stage=stage, **{"class": "routine"})
             if _ts(r) >= since and r.get("decision") == "proceed"]
    key = (norm(signature), norm(diagnosis), norm(action))
    if any((norm(r.get("signature")), norm(r.get("diagnosis")), norm(r.get("action"))) == key for r in prior):
        rec["decision"] = "repeat-refused"
        rec["detail"] = ("identical signature, diagnosis and action already tried for this stage: change the "
                         "diagnosis or the approach (read the new output, narrow the cause, try another authorized path)")
    elif len(prior) >= MAX_RECOVERY_PER_STAGE:
        rec["decision"] = "exhausted"
        rec["block_reason"] = (f"recovery-exhausted: {stage} after {len(prior)} distinct attempts; last failure "
                               f"{signature.strip()}; work is preserved; smallest intervention: {action.strip()}")
    else:
        rec["decision"] = "proceed"
        rec["attempt"] = len(prior) + 1
        rec["limit"] = MAX_RECOVERY_PER_STAGE
    return rec


def check_block(recs: list, reason: str) -> dict:
    """allowed (a genuine stop with its class) or refused (routine: recover instead)."""
    reason = (reason or "").strip()
    prefix, sep, rest = reason.partition(":")
    cls = BLOCK_CLASSES.get(prefix.strip()) if sep and rest.strip() else None
    if cls == "recovery-exhausted" and not of(recs, "recovery", decision="exhausted"):
        return {"event": "block-refused", "reason": reason,
                "detail": "no recorded exhausted recovery: record each distinct attempt with `completion recover` first"}
    if cls is None:
        allowed = ", ".join(f"{p}:" for p in BLOCK_CLASSES)
        return {"event": "block-refused", "reason": reason,
                "detail": f"routine failure: diagnose and recover (`completion recover`), do not block; a block "
                          f"reason must start with one of {allowed}"}
    return {"event": "block", "class": cls, "reason": reason}


def _is_stop(r: dict) -> bool:
    return r.get("event") == "block" or (r.get("event") == "recovery" and r.get("decision") in ("stop", "exhausted"))


def resolve_record(recs: list, stop_rid: str, reason: str) -> dict:
    """An explicit resolution of one recorded stop (the only way an approval stop closes):
    *reason* names the operator's decision; it grants nothing the card's gates require."""
    if not (reason or "").strip():
        raise CompletionError("resolve needs --reason: the operator decision or the evidence that resolves the stop")
    stop = next((r for r in recs if _is_stop(r) and r.get("rid") == stop_rid), None)
    if not stop:
        raise CompletionError(f"no recorded stop '{stop_rid}': use the rid that `completion status` names")
    return {"event": "resolve", "stop": stop_rid, "class": stop.get("class"), "reason": reason.strip()}


# ------------------------------------------------------------- evaluation ---

def _stage(name: str, met: Optional[bool], unmet: list, **extra) -> dict:
    return {"stage": name, "met": met, "unmet": [] if met else unmet, **extra}


def _fresh_evidence(recs: list, kind: str, check: str, current: Optional[dict], accept=None) -> tuple:
    """(met, unmet line, evidence) for one check against the current content. *accept*: an
    extra predicate a record must pass to count (the dev-test mode's backend rule)."""
    evs = [e for e in of(recs, "evidence", kind=kind, check=check) if accept is None or accept(e)]
    what = f"{kind} '{check}'" if kind == "test" else "full-diff review"
    if current is None:
        return None, f"{what}: source state unknown (pass --repo-dir)", evs[-1] if evs else None
    same = [e for e in evs if (e.get("source") or {}).get("content_id") == current["content_id"]]
    if same:
        ev = same[-1]
        if ev.get("result") == "pass":
            return True, "", ev
        return False, (f"{what} fails on the current source ({short(current['content_id'])}): "
                       f"diagnose, fix, retest"), ev
    if evs:
        return False, (f"{what} evidence is stale (content {short((evs[-1].get('source') or {}).get('content_id'))}, "
                       f"current {short(current['content_id'])}): rerun it"), evs[-1]
    return False, f"{what}: no evidence yet: run it and record it", None


def evaluate(recs: list, code_card: str, current: Optional[dict], pushed: Optional[dict],
             remote_sha: Optional[str] = None, final: Optional[dict] = None,
             accept: Optional[dict] = None) -> list:
    """One row per stage. *recs*: every completion record of the request, oldest first.
    *current*: source identity of the checkout (None: unknown). *pushed*: the code card's
    latest code-sha record. *remote_sha*: a live `git ls-remote` answer (None: not checked
    now; record-sha verified it when it wrote *pushed*). *final*: {"validate": (ok, reasons),
    "complete": (ok, reasons)} from the report gate, None when not evaluated. *accept*: {check:
    predicate} a record of that check must pass to count (the dev-test mode's db-tests rule)."""
    rows = []
    crit = last(recs, "criteria")
    rows.append(_stage("understand", bool(crit), ["record the acceptance criteria recovered from the request, "
                                                   "card and repository (completion criteria)"],
                       criteria=(crit or {}).get("items", [])))
    plan = last(recs, "plan")
    plan_ok = bool(plan) and (not crit or float(plan.get("ts") or 0) >= float(crit.get("ts") or 0))
    rows.append(_stage("plan", plan_ok, ["record a plan for the current criteria (completion plan)"]))

    dels = [r for r in of(recs, "delegate") if r.get("card") == code_card]
    latest_state = {}
    for r in dels:
        latest_state[r["delegate"]] = r
    unmet = []
    for d, r in latest_state.items():
        if r["state"] == "started":
            unmet.append(f"delegate {d} is still running: collect its result; never start a second writer")
    if dels and dels[-1]["state"] == "partial":
        unmet.append(f"delegate {dels[-1]['delegate']} returned partial: continue the remaining work: "
                     f"{dels[-1].get('remaining')}")
    elif dels and dels[-1]["state"] in ("failed", "interrupted"):
        unmet.append(f"delegate {dels[-1]['delegate']} {dels[-1]['state']}: diagnose (completion recover), resume "
                     "from the checkpoint; its exit does not release the lease or end this run")
    if not dels:
        unmet.append(f"implement on {code_card}: delegate the bounded coding task and record its result "
                     "(completion delegate)")
    impl_ok = not unmet and dels[-1]["state"] == "complete"
    rows.append(_stage("implement", impl_ok, unmet or ["record the delegate's complete result"]))

    checks = (crit or {}).get("checks") or list(DEFAULT_CHECKS)
    test_unmet, oks, evid = [], [], {}
    for check in checks:
        ok, line, ev = _fresh_evidence(recs, "test", check, current, (accept or {}).get(check))
        oks.append(ok)
        evid[check] = (ev or {}).get("rid")
        if ok is not True:
            test_unmet.append(line)
    test_ok = False if False in oks else (None if None in oks else True)
    rows.append(_stage("test", test_ok, test_unmet, checks=checks, evidence=evid))

    rev_ok, line, ev = _fresh_evidence(recs, "review", "diff", current)
    rows.append(_stage("review", rev_ok, [line], evidence=(ev or {}).get("rid")))

    if current is None:
        rows.append(_stage("commit", None, ["source state unknown (pass --repo-dir)"]))
    else:
        commit_unmet = [] if current["head"] else ["no commit yet"]
        if current.get("dirty"):
            commit_unmet.append(f"commit the work: {current['dirty']} uncommitted or untracked path(s)")
        if current.get("flagged"):
            commit_unmet.append(f"{current['flagged']} index entr{'y' if current['flagged'] == 1 else 'ies'} flagged "
                                "assume-unchanged or skip-worktree: git status cannot show their changes; clear the "
                                "flags (git update-index --no-assume-unchanged / --no-skip-worktree <path>), then "
                                "commit what they hid")
        rows.append(_stage("commit", bool(current["head"]) and current["clean"],
                           commit_unmet or ["the tree is not clean"]))

    push_unmet = []
    sha = (pushed or {}).get("sha", "")
    if not sha:
        push_unmet.append("push the branch and run record-sha (exact remote SHA)")
    elif current is not None and current["head"] != sha:
        push_unmet.append(f"HEAD {short(current['head'])} is not the recorded pushed SHA {short(sha)}: push, "
                          "then record-sha again")
    if sha and remote_sha is not None and remote_sha != sha:
        push_unmet.append(f"the remote branch is at {short(remote_sha) if remote_sha else 'nothing'}, not "
                          f"{short(sha)}: push again (never force)")
    push_ok = None if (current is None and sha and not push_unmet and remote_sha is None) else not push_unmet
    rows.append(_stage("push", push_ok, push_unmet or ["remote SHA not verified now (handoff checks it)"],
                       commit=sha or None))

    for name in FINAL_STAGES:
        if not final or name not in final:
            rows.append(_stage(name, None, [f"{name}: not evaluated (no report card or gate yet)"]))
        else:
            ok, reasons = final[name]
            rows.append(_stage(name, bool(ok), reasons or [f"{name} not met"]))
    return rows


def next_unmet(rows: list) -> list:
    """The first stage that is not met, in order, with its unmet criteria."""
    for row in rows:
        if row["met"] is not True:
            return [{"stage": row["stage"], "met": row["met"], "unmet": row["unmet"]}]
    return []


def handoff_ready(rows: list) -> bool:
    return all(r["met"] is True for r in rows if r["stage"] in HANDOFF_STAGES)


def open_stop(recs: list) -> Optional[dict]:
    """The latest genuine stop not yet resolved. An approval stop is resolved only by an
    explicit `resolve` record; any other stop also by its own stage being met after it or
    by a later handoff. An unrelated pass (one check, another stage) never hides it."""
    resolved = {r.get("stop") for r in of(recs, "resolve")}
    handoffs = [_ts(r) for r in of(recs, "handoff")]
    for r in reversed([r for r in recs if _is_stop(r)]):
        if r.get("rid") in resolved:
            continue
        if r.get("class") != "approval":
            met = stage_met_times(recs, r["stage"]) if r.get("stage") in STAGES else []
            if any(t > _ts(r) for t in met + handoffs):
                continue
        return r
    return None


# ---------------------------------------------------------------- metrics ---

def _ratio(num: int, den: int, unknown: int = 0, note: str = "") -> dict:
    out = {"count": num, "of": den, "unknown": unknown}
    out["rate"] = round(num / den, 3) if den else None
    if note:
        out["note"] = note
    return out


def _recovery_outcome(recs: list, r: dict) -> Optional[bool]:
    """True: the stage was met after the attempt, or the failed check it followed passed
    later; False: that check only failed again; None (unknown): neither is recorded."""
    if any(t > _ts(r) for t in stage_met_times(recs, r.get("stage"))):
        return True
    if r.get("stage") not in ("test", "review"):
        return None
    kind = r["stage"]
    failed = [e for e in of(recs, "evidence", kind=kind, result="fail") if _ts(e) <= _ts(r)]
    if not failed:
        return None
    later = [e for e in of(recs, "evidence", kind=kind, check=failed[-1].get("check")) if _ts(e) > _ts(r)]
    if any(e.get("result") == "pass" for e in later):
        return True
    return False if later else None


def metrics(ledgers: dict) -> dict:
    """Delivery metrics from ledger records only ({task_id: [records]}). No baseline is
    assumed; a value that the ledger cannot show is reported as unknown."""
    by_key, pushes = {}, 0
    for recs in ledgers.values():
        for r in recs:
            if r.get("type") == "code-sha":
                pushes += 1
            if r.get("type") == RTYPE and r.get("key"):
                by_key.setdefault(r["key"], []).append(r)
    for recs in by_key.values():
        recs.sort(key=lambda r: float(r.get("ts") or 0))
    keys = sorted(by_key)
    completed = [k for k in keys if of(by_key[k], "complete")]
    handed = [k for k in keys if of(by_key[k], "handoff")]
    no_block = [k for k in completed if not of(by_key[k], "block")]
    blocks = {}
    for k in keys:
        for r in of(by_key[k], "block"):
            blocks[r.get("class")] = blocks.get(r.get("class"), 0) + 1
    attempts = succeeded = pending = 0
    areas = {}
    for k in keys:
        recs = by_key[k]
        for r in of(recs, "recovery", decision="proceed"):
            attempts += 1
            areas[r.get("area") or "unrecorded"] = areas.get(r.get("area") or "unrecorded", 0) + 1
            outcome = _recovery_outcome(recs, r)
            succeeded += outcome is True
            pending += outcome is None
    delegated_done = [k for k in keys if of(by_key[k], "delegate", state="complete")]
    stalled = [k for k in delegated_done if not of(by_key[k], "handoff")]
    exact = sum(1 for k in completed
                if {r.get("commit") for r in of(by_key[k], "complete")} & {r.get("commit") for r in of(by_key[k], "handoff")})
    return {
        "source": "kanban-workflow ledger records only; no baseline",
        "requests_tracked": len(keys),
        "completed": _ratio(len(completed), len(keys)),
        "completed_without_recorded_block": _ratio(
            len(no_block), len(completed),
            note="operator unblocks and blocks not checked through `completion block-check` are not in the ledger"),
        "blocks_by_class": blocks,
        "avoidable_blocks_refused": sum(len(of(by_key[k], "block-refused")) for k in keys),
        "recovery_after_failure": _ratio(
            succeeded, attempts, pending,
            note="success = the stage met after the attempt, or the failed check it followed passed later; unknown "
                 "= neither recorded yet"),
        "recovery_attempts_by_area": areas,
        "time_implementation_vs_infrastructure": "unknown (durations are not recorded)",
        "verified_and_reviewed": _ratio(len(completed), len(keys),
                                        note="complete requires the gate: lane evidence and independent review"),
        "pushes_recorded": pushes,
        "implementation_handoffs_remote_verified": _ratio(len(handed), len(keys)),
        "exact_sha_validation_handoffs": _ratio(exact, len(completed)),
        "duplicate_writer_refusals": sum(len(of(by_key[k], "owner-conflict")) for k in keys),
        "lost_work_incidents": "unknown (not recorded)",
        "incomplete_after_delegate_finished": _ratio(len(stalled), len(delegated_done)),
    }


# ------------------------------------------------------ capability detection ---

# Allowlisted manifests and toolchain hints: (glob, stack, hint). Files are only matched
# by name (a few JSON/TOML fields are read); nothing is executed. Hints are what a
# human or an approved path would run, never run by this module.
MANIFESTS = (
    ("go.mod", "go", "go test ./..."),
    ("package.json", "node", "npm test (see scripts)"),
    ("pnpm-lock.yaml", "node:pnpm", "pnpm test"),
    ("yarn.lock", "node:yarn", "yarn test"),
    ("pyproject.toml", "python", "python -m pytest"),
    ("requirements.txt", "python", "python -m pytest"),
    ("Cargo.toml", "rust", "cargo test"),
    ("Gemfile", "ruby", "bundle exec rake test"),
    ("pom.xml", "java:maven", "mvn test"),
    ("build.gradle", "jvm:gradle", "./gradlew test"),
    ("build.gradle.kts", "jvm:gradle", "./gradlew test"),
    ("settings.gradle", "jvm:gradle", "./gradlew test"),
    ("settings.gradle.kts", "jvm:gradle", "./gradlew test"),
    ("Package.swift", "swift", "swift test"),
    ("Makefile", "make", "make help / make test"),
    ("Dockerfile", "docker", "docker build"),
    ("docker-compose.yml", "docker-compose", "docker compose config"),
    ("docker-compose.yaml", "docker-compose", "docker compose config"),
    ("compose.yml", "docker-compose", "docker compose config"),
    ("compose.yaml", "docker-compose", "docker compose config"),
    (".tool-versions", "toolchain:asdf", ""),
    (".nvmrc", "toolchain:node", ""),
    (".python-version", "toolchain:python", ""),
    ("go.work", "go", ""),
)
DIR_HINTS = (
    ("migrations", "database:migrations"), ("db/migrations", "database:migrations"),
    (".github/workflows", "ci:github-actions"),
)
SUFFIX_HINTS = ((".xcodeproj", "apple:xcode"), (".xcworkspace", "apple:xcode"), (".csproj", "dotnet"),
                (".sln", "dotnet"))


DB_NEED_BY_MODE = {
    "compose": ("database tests (Dev-test-mode: compose), when the change touches database code: the project's "
                "existing Docker Compose development services under the card's Compose rules (compose exec/run, "
                "or host go test -p 1 / race / vet, or the repository's make target or script) against an "
                "isolated test database, never the main development database; never dev_test_run; select "
                "--check db-tests and record it with --backend compose; never a credential from the project's "
                "env files on a command line"),
    "dev_test_run": ("database tests (Dev-test-mode: dev_test_run), when the change touches database code: only the "
                     "dev_test_run tool; no Compose or terminal path; without it a capability block; select "
                     "--check db-tests and record it with --backend dev_test_run"),
}


def detect_capabilities(root: str, registered_scanner: bool = False, dev_test_mode: str = "") -> dict:
    """Stacks, hint commands and the approved capabilities a task in *root* would need.
    Reads names in the top two directory levels and package.json scripts only."""
    base = Path(root)
    stacks, hints, found = [], [], []
    for name, stack, hint in MANIFESTS:
        if (base / name).is_file() and not (base / name).is_symlink():
            found.append(name)
            stacks.append(stack)
            if hint:
                hints.append(hint)
    for rel, stack in DIR_HINTS:
        if (base / rel).is_dir() and not (base / rel).is_symlink():
            found.append(rel + "/")
            stacks.append(stack)
    level = []
    for directory in [base]:
        try:
            top = list(directory.iterdir())
        except OSError:
            break
        level += top
        for p in top:
            if p.is_dir() and not p.is_symlink() and not p.name.startswith("."):
                try:
                    level += list(p.iterdir())
                except OSError:
                    continue
    for p in level:
        for suffix, stack in SUFFIX_HINTS:
            if p.name.endswith(suffix):
                found.append(str(p.relative_to(base)))
                stacks.append(stack)
    scripts = []
    pkg = base / "package.json"
    if pkg.is_file() and not pkg.is_symlink():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            scripts = sorted(k for k in (data.get("scripts") or {}) if isinstance(k, str))[:20]
        except (OSError, ValueError, AttributeError):
            scripts = []
    stacks = list(dict.fromkeys(stacks))
    needs = []
    if any(s.startswith("database") for s in stacks) and dev_test_mode in DB_NEED_BY_MODE:
        needs.append(DB_NEED_BY_MODE[dev_test_mode])
    elif any(s.startswith("database") for s in stacks):
        needs.append("database tests: a registered dev_test_run profile (approved isolation); never a database "
                     "URL or credential file on a command line")
    if any(s.startswith("docker") for s in stacks):
        needs.append("docker: an approved isolated runner; unrestricted Docker access is not granted")
    if "apple:xcode" in stacks or "swift" in stacks:
        needs.append("macOS/iOS validation: the Mac lane (kanban-workflow --lane mac [--mobile ios])")
    if "jvm:gradle" in stacks or "dotnet" in stacks:
        needs.append("Windows/Android validation: the Windows lane (deferred until the Windows worker is repaired)")
    if "ci:github-actions" in stacks:
        needs.append("GitHub Actions: read the run for the exact pushed SHA (gh run list --commit <sha>)")
    return {"root": str(base), "manifests": found, "stacks": stacks, "package_scripts": scripts,
            "hints": list(dict.fromkeys(hints)), "hints_executed": False,
            "approved_capabilities_needed": needs,
            "writer_scanner_registered": registered_scanner,
            **({"dev_test_mode": dev_test_mode} if dev_test_mode else {})}
