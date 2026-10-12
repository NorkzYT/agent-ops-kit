#!/usr/bin/env python3
"""Standard Hermes workflow: Discord request -> Kanban cards -> isolated workers
-> review gate -> final Discord report.

    workflow.py intake   --title T --body B --lane mac --lane windows  # build the card graph
    workflow.py dispatch <lane_id>                                    # peer lane: start the remote run
    workflow.py wait     <lane_id> [--timeout S]                      # peer lane: poll the remote run
    workflow.py gate     <request_id>                                 # exit 0 iff every lane passed review
    workflow.py status   <request_id>                                 # one line per card

Card graph (Hermes `--parent` means "waits for"):

    [mac] T  ─┐  lane cards: end with kanban_request_review to an independent reviewer
    [win] T  ─┴─> Report: T  request card: waits for the lanes, runs `gate`, writes the
                             report; its completion wakes the orchestrator in the
                             originating Discord thread (notify+wake subscription)

Mac and Windows lanes are peer lanes: the host's `mac-worker`/`windows-worker`
profile is a thin relay. It runs `dispatch`, which hands the card's remote task
to the worker machine's own Hermes over `hermes peer run` (authenticated API,
idempotent per card and attempt) and records peer + run id on the card, then
`wait`, which polls `hermes peer status` and turns the remote JSON report into
the review hand-off. The peer key stays in the lane profile's .env; this script
never reads or prints it.

Everything goes through the `hermes kanban` / `hermes peer` CLIs (no direct
SQLite access), so the dispatcher, review dispatch and gateway notifier stay the
source of truth. Idempotent: every card carries an idempotency key derived from
the Discord message, so a retried intake returns the same cards. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

SCRIPT = Path(__file__).resolve()
LANES = {"mac": "mac-worker", "windows": "windows-worker"}
LANE_LABELS = {"mac-worker": "mac", "windows-worker": "win"}
LANE_WHERE = {"mac-worker": "the Mac worker", "windows-worker": "the Windows worker"}
# Lanes that run on another machine's own Hermes, and their default peer target
# (`hermes -p <lane> peer add <target> ...`; `--peer lane=target` overrides).
PEER_LANES = {"mac-worker": "mac", "windows-worker": "windows"}
PEER_TRANSPORT = "hermes-peer"
RELAY_BACKEND = "local"  # the relay runs this script, thus on this host
PEER_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}(/[A-Za-z0-9][A-Za-z0-9_-]{0,63})?$")
RECORD_PREFIX = "peer-run "
REMOTE_MARKER = "===== Remote task (sent to the peer as is) ====="
TASK_TOKEN = "$HERMES_KANBAN_TASK"
RUNNING = {"queued", "started", "running", "stopping"}
FAILED = {"failed", "cancelled", "interrupted"}
SECRET_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD))(\s*[=:]\s*)(?!<)[^\s'\"]+"), r"\1\2[REDACTED]"),
]

Runner = Callable[[list], subprocess.CompletedProcess]


class WorkflowError(RuntimeError):
    pass


def default_runner(argv: list) -> subprocess.CompletedProcess:
    hermes = os.environ.get("HERMES_BIN", "hermes")
    return subprocess.run([hermes, *argv], capture_output=True, text=True, timeout=120)


def hermes_json(run: Runner, argv: list):
    r = run(argv)
    if r.returncode != 0:
        raise WorkflowError(f"hermes {' '.join(argv[:3])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"hermes {' '.join(argv[:3])} returned non-JSON output") from exc


# ------------------------------------------------------------------ intake ---

def discord_origin(args) -> Optional[dict]:
    """Explicit --discord-* flags win; else the gateway's HERMES_SESSION_* env,
    which the local terminal backend injects into every command it runs."""
    env = os.environ
    from_env = env.get("HERMES_SESSION_PLATFORM", "").lower() == "discord"
    pick = lambda flag, var: flag or (env.get(var, "") if from_env else "")  # noqa: E731
    chat_id = pick(args.discord_chat_id, "HERMES_SESSION_CHAT_ID")
    if not chat_id:
        return None
    return {
        "chat_id": chat_id,
        "thread_id": pick(args.discord_thread_id, "HERMES_SESSION_THREAD_ID"),
        "message_id": pick(args.discord_message_id, "HERMES_SESSION_MESSAGE_ID"),
        "user_id": pick(args.discord_user_id, "HERMES_SESSION_USER_ID"),
        "chat_type": pick(args.discord_chat_type, "HERMES_SESSION_CHAT_TYPE"),
    }


def idempotency_base(key: str, origin: Optional[dict]) -> str:
    if key:
        return key
    if origin and origin.get("message_id"):
        return f"discord:{origin['chat_id']}:{origin['message_id']}"
    raise WorkflowError("no idempotency key: pass --key, or run from a Discord turn "
                        "(or pass --discord-chat-id and --discord-message-id)")


def lane_profile(lane: str) -> str:
    return LANES.get(lane, lane)


def peer_targets(lanes: list, overrides: list) -> dict:
    """{lane profile: peer target} for every peer lane. Mac/Windows lanes are always
    peer lanes; `--peer lane=target` renames their target or adds another lane."""
    peers = {lane: PEER_LANES[lane] for lane in lanes if lane in PEER_LANES}
    for item in overrides:
        lane, sep, target = item.partition("=")
        lane, target = lane_profile(lane.strip()), target.strip()
        if not sep or lane not in lanes:
            raise WorkflowError(f"--peer '{item}': expected <lane>=<peer>[/<profile>] for a --lane of this request")
        peers[lane] = target
    for lane, target in peers.items():
        if not PEER_TARGET_RE.match(target):
            raise WorkflowError(f"lane '{lane}': invalid peer target '{target}' (<peer>[/<profile>])")
    return peers


def peer_setup_hint(lane: str, target: str) -> str:
    return (f"set it up on this host: PROFILE_PEER_NAME={target.partition('/')[0]} "
            f"PROFILE_PEER_URL=http://<worker>:8642 PROFILE_PEER_KEY=<worker API_SERVER_KEY> "
            f"make hermes-profile NAME={lane} (docs/discord-kanban-workflow.md, 'Peers')")


def peer_problem(run: Runner, lane: str, target: str) -> Optional[str]:
    """None when the lane profile has the peer registered with a key, else why not.
    Reads `hermes peer list`, which reports only whether a key is set."""
    peer = target.partition("/")[0]
    r = run(["-p", lane, "peer", "list"])
    if r.returncode != 0:
        return f"lane '{lane}': `hermes -p {lane} peer list` failed; {peer_setup_hint(lane, target)}"
    for line in (r.stdout or "").splitlines():
        cols = line.split("\t")
        if cols[0].strip() == peer:
            if len(cols) > 2 and cols[2].startswith("[key set]"):
                return None
            return f"lane '{lane}': peer '{peer}' has no API key; {peer_setup_hint(lane, target)}"
    return f"lane '{lane}': no Hermes peer '{peer}' is configured; {peer_setup_hint(lane, target)}"


def check_lanes(run: Runner, lanes: list, peers: dict, reviewer: str) -> None:
    """Refuse to build a graph the dispatcher cannot finish: every assignee must
    exist, every peer lane needs its peer registered with a key and a relay that
    runs here, and the reviewer must not be one of the implementers."""
    problems = []
    if reviewer in lanes:
        problems.append(f"reviewer '{reviewer}' is also a lane; review must be independent")
    for name in [*lanes, reviewer]:
        r = run(["-p", name, "config", "get", "terminal.backend"])
        if r.returncode != 0:
            problems.append(f"profile '{name}' does not exist (see docs/discord-kanban-workflow.md)")
            continue
        if name not in peers:
            continue
        backend = ((r.stdout or "").strip().splitlines()[-1:] or [""])[0]
        if backend != RELAY_BACKEND:
            problems.append(f"lane '{name}' terminal.backend is '{backend}', not '{RELAY_BACKEND}'; the relay "
                            f"runs on this host: hermes -p {name} config set terminal.backend local")
            continue
        problem = peer_problem(run, name, peers[name])
        if problem:
            problems.append(problem)
    if problems:
        raise WorkflowError("; ".join(problems))


HUMAN_GATES = """\
Human gates (mandatory):
- Never merge, deploy, release, publish, or change production. Those need the operator's explicit approval.
- Never type or request passwords, tokens, MFA codes or keys. At any login, MFA,
  payment, admin/UAC or credential prompt, stop and call
  kanban_block(reason="human-approval: <what you hit>"). The operator finishes it and unblocks.
- Never send Discord messages yourself; the board notifies the requester."""

REPORT_NOTE = """\
The "Report:" card that waits on this one is the final report, not a review phase;
it does not replace review."""


def lane_body(request: str, lane: str, reviewer: str, repo: str, key: str) -> str:
    """Local profile lane (coder, ...): the assignee does the work on this host."""
    where = LANE_WHERE.get(lane, f"the '{lane}' lane")
    repo_line = f"Repository: {repo}\n" if repo else ""
    return f"""\
Request (from Discord, verbatim):
{request}

{repo_line}Lane: {lane} — your terminal runs on {where}.
Workflow key: {key}

Contract:
1. Work in a fresh per-task directory: ~/hermes-work/{TASK_TOKEN}.
   Clone or fetch the repository there. Never reuse another task's checkout.
2. Use a feature branch (feat/... or fix/...). Never commit to main.
3. Build and test; keep the exact commands and their output.
4. Push the feature branch so the reviewer can read it. Opening a draft PR is fine; merging is not.
5. Finish with kanban_request_review(reviewer="{reviewer}", summary=<what changed and how
   it was verified>, metadata={{"branch": ..., "commit": ..., "tests": ...}}).
   Do not call kanban_complete yourself: the reviewer approves. {REPORT_NOTE}
6. If you cannot finish, call kanban_block with the exact blocker.

{HUMAN_GATES}
"""


def remote_task(request: str, lane: str, repo: str, key: str) -> str:
    """What the worker machine's own Hermes receives (TASK_TOKEN is filled in by dispatch)."""
    where = LANE_WHERE.get(lane, f"the '{lane}' worker")
    repo_line = f"Repository: {repo}\n" if repo else ""
    return f"""\
You are {where}'s own Hermes agent. Kanban card {TASK_TOKEN} on the coordinator sent you
this task over the authenticated peer API. You cannot reach the coordinator's board;
an independent reviewer there reads your branch before anything counts as done.

Request (from Discord, verbatim):
{request}

{repo_line}Workflow key: {key}

Contract:
1. Work in a fresh directory: ~/hermes-work/{TASK_TOKEN}. Clone or fetch the repository there.
   Never reuse another task's checkout and never touch files outside it.
2. Use a feature branch (feat/... or fix/...). Never commit to main. Conventional Commits.
3. Build and test here; keep the exact commands and their output.
4. Push the feature branch so the reviewer can read it. A draft PR is fine; merging is not.
5. Never merge, deploy, release or publish. Never type or ask for passwords, tokens,
   MFA codes or keys: at a login, MFA, payment, admin/UAC or credential prompt, stop and
   report "status": "blocked" with "blocker": "human-approval: <what you hit>".
6. Never send Discord messages. Never put secrets in your reply.
7. End your final reply with one fenced json block and nothing after it:
```json
{{"task": "{TASK_TOKEN}", "status": "ready_for_review", "branch": "...", "commit": "...", "tests": "<commands and result>", "summary": "<what changed>", "blocker": ""}}
```
   When you cannot finish, use "status": "blocked" and a one-line "blocker".
"""


def peer_lane_body(request: str, lane: str, target: str, reviewer: str, repo: str, key: str) -> str:
    """Peer lane (mac/windows): the assignee relays to the worker's own Hermes.
    The header lines come first; dispatch reads them and nothing below."""
    where = LANE_WHERE.get(lane, f"the '{lane}' worker")
    return f"""\
Transport: {PEER_TRANSPORT}
Peer: {target}
Lane: {lane}

You relay this card to {where}'s own Hermes agent; you do not implement it here.
Workflow key: {key}

Relay steps:
1. Run: python3 {SCRIPT} dispatch {TASK_TOKEN}
   It sends the remote task below with `hermes peer run` (idempotent per card and attempt)
   and records the peer and run id on this card. Exit 2 is a setup error:
   call kanban_block(reason="peer-setup: <its output>") and stop.
2. Run: python3 {SCRIPT} wait {TASK_TOKEN} --timeout 540
   Repeat while it exits 3 (the remote run is still working).
3. Act on its exit code:
   0  ready for review: kanban_request_review(reviewer="{reviewer}", summary=<"summary" it
      printed>, metadata=<the "metadata" object it printed>).
   4  blocked on the worker: kanban_block(reason=<the "blocker" it printed>).
   1  the remote run failed or sent no report: kanban_block(reason="peer-run: <its "error">").
   Copy only what the script printed; it redacts secrets. Never call kanban_complete:
   the reviewer approves. {REPORT_NOTE}
   Never do the work on this host and never switch to another transport (SSH included).

{HUMAN_GATES}

{REMOTE_MARKER}
{remote_task(request, lane, repo, key)}"""


def request_body(request: str, lanes: list, key: str) -> str:
    return f"""\
Request (from Discord, verbatim):
{request}

Workflow key: {key}
Lane cards: {', '.join(lanes)} (this card's parents).

You write the final report. Steps:
1. Run: python3 {SCRIPT} gate $HERMES_KANBAN_TASK
   Exit 0 means every lane card was approved by an independent reviewer.
   Any other exit: call kanban_block(reason="review-gate: <its output>") and stop.
2. Run: python3 {SCRIPT} status $HERMES_KANBAN_TASK --json
   It lists each lane's transport, peer and remote run id. Read each lane card
   (kanban_show) for branch, commit, tests and the review verdict.
3. Call kanban_complete(summary=<one-line outcome>, result=<report>). The report lists, per lane:
   status, transport (peer and run id for Mac/Windows lanes), branch and commit, tests run
   and their result, reviewer verdict; then "Needs your approval:" with every merge,
   deploy, release or login still open.
   The gateway posts the outcome to the Discord thread and wakes the orchestrator there
   to deliver the report.

{HUMAN_GATES}
"""


def create_card(run: Runner, title: str, assignee: str, body: str, key: str,
                parents: list = (), max_runtime: str = "") -> dict:
    argv = ["kanban", "create", title, "--assignee", assignee, "--body", body,
            "--idempotency-key", key, "--created-by", "kanban-workflow", "--json"]
    for p in parents:
        argv += ["--parent", p]
    if max_runtime:
        argv += ["--max-runtime", max_runtime]
    return hermes_json(run, argv)


def subscribe(run: Runner, task_id: str, origin: dict, mode: str) -> None:
    argv = ["kanban", "notify-subscribe", task_id, "--platform", "discord",
            "--chat-id", origin["chat_id"], "--delivery-mode", mode]
    for flag, field in (("--thread-id", "thread_id"), ("--user-id", "user_id"), ("--chat-type", "chat_type")):
        if origin.get(field):
            argv += [flag, origin[field]]
    r = run(argv)
    if r.returncode != 0:
        raise WorkflowError(f"notify-subscribe {task_id} failed: {(r.stderr or r.stdout).strip()[:300]}")


def short_title(title: str, limit: int = 80) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    return title if len(title) <= limit else title[: limit - 1] + "…"


def intake(args, run: Runner = default_runner) -> dict:
    lanes = list(dict.fromkeys(lane_profile(lane) for lane in args.lane))
    if not lanes:
        raise WorkflowError("at least one --lane is required (mac, windows or a profile name)")
    peers = peer_targets(lanes, args.peer)
    origin = discord_origin(args)
    base = idempotency_base(args.key, origin)
    request = args.body or args.title
    title = short_title(args.title)

    plan = {
        "key": base,
        "lanes": [{"assignee": lane, "title": f"[{LANE_LABELS.get(lane, lane)}] {title}",
                   "idempotency_key": f"{base}:{lane}",
                   "transport": PEER_TRANSPORT if lane in peers else "local",
                   **({"peer": peers[lane]} if lane in peers else {})} for lane in lanes],
        "request": {"assignee": args.orchestrator, "title": f"Report: {title}",
                    "idempotency_key": f"{base}:request"},
        "reviewer": args.reviewer,
        "discord": origin,
    }
    if args.dry_run:
        plan["dry_run"] = True
        return plan

    check_lanes(run, lanes, peers, args.reviewer)
    work_ids = []
    for spec in plan["lanes"]:
        lane = spec["assignee"]
        body = (peer_lane_body(request, lane, peers[lane], args.reviewer, args.repo, base) if lane in peers
                else lane_body(request, lane, args.reviewer, args.repo, base))
        card = create_card(run, spec["title"], lane, body, spec["idempotency_key"], max_runtime=args.max_runtime)
        spec["id"] = card["id"]
        work_ids.append(card["id"])
    req = create_card(run, plan["request"]["title"], args.orchestrator, request_body(request, lanes, base),
                      plan["request"]["idempotency_key"], parents=work_ids)
    plan["request"]["id"] = req["id"]

    plan["subscribed"] = False
    if origin:
        # Lane cards: passive status lines (review requested, blocked for a human).
        # Request card: notify + wake the orchestrator so it delivers the report.
        for wid in work_ids:
            subscribe(run, wid, origin, "notify")
        subscribe(run, req["id"], origin, "notify+wake")
        plan["subscribed"] = True
    return plan


# ------------------------------------------------------------ peer lanes ---

def redact(value):
    """Mask credential-looking strings before anything reaches stdout or the board."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    for pattern, repl in SECRET_PATTERNS:
        value = pattern.sub(repl, value)
    return value


def card_header(body: str) -> dict:
    """`Key: value` lines before the first blank line of a body that opens with
    `Transport:`. Only peer lane bodies do; every other body opens with the
    verbatim request, thus a request cannot spoof the header."""
    fields = {}
    if not (body or "").startswith("Transport:"):
        return fields
    for line in body.split("\n\n", 1)[0].splitlines():
        name, sep, value = line.partition(":")
        if sep:
            fields[name.strip().lower()] = value.strip()
    return fields


def peer_records(show: dict) -> list:
    records = []
    for c in show.get("comments") or []:
        body = c.get("body") or ""
        if body.startswith(RECORD_PREFIX):
            try:
                rec = json.loads(body[len(RECORD_PREFIX):])
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("run_id"):
                records.append(rec)
    return records


def peer_lane(show: dict) -> tuple:
    """(lane, target) of a peer lane card, or raise."""
    task = show.get("task") or {}
    header = card_header(task.get("body") or "")
    if header.get("transport") != PEER_TRANSPORT:
        raise WorkflowError(f"{task.get('id')} is not a peer lane card (no 'Transport: {PEER_TRANSPORT}' header)")
    target, lane = header.get("peer", ""), task.get("assignee") or ""
    if not PEER_TARGET_RE.match(target) or not lane:
        raise WorkflowError(f"{task.get('id')}: peer lane card without a valid peer or assignee")
    return lane, target


def peer_status(run: Runner, lane: str, target: str, run_id: str) -> dict:
    return hermes_json(run, ["-p", lane, "peer", "status", target, run_id, "--json"])


def followup(show: dict, rec: dict) -> Optional[tuple]:
    """(kind, note for the remote agent) for board events since `rec` was dispatched
    that call for a new attempt: a reviewer change request, or an operator unblock."""
    events = (show.get("events") or [])[int(rec.get("events_seen") or 0):]
    changes = [e for e in events if e.get("kind") == "changes_requested"]
    if changes:
        reason = (changes[-1].get("payload") or {}).get("reason") or "(no reason given)"
        return "changes_requested", f"The independent reviewer asked for changes:\n{reason}"
    if any(e.get("kind") == "unblocked" for e in events):
        return "unblocked", "The operator cleared the earlier block. Continue from where you stopped."
    return None


def dispatch(task_id: str, run: Runner = default_runner) -> dict:
    """Start (or resume) the remote run for a peer lane card and record it on the card.
    A new attempt starts only for a first dispatch, a reviewer change request, or an
    unblock after the previous run ended; anything else resumes the recorded run."""
    show = hermes_json(run, ["kanban", "show", task_id, "--json"])
    lane, target = peer_lane(show)
    problem = peer_problem(run, lane, target)
    if problem:
        raise WorkflowError(problem)
    records = peer_records(show)
    kind, note = None, None
    if records:
        latest = records[-1]
        kind, note = followup(show, latest) or (None, None)
        if kind is None:
            return {**latest, "resumed": True}
        if kind == "unblocked":  # the operator may have cleared a gate on a live run
            remote = peer_status(run, lane, target, latest["run_id"])
            if remote.get("status") in RUNNING | {"waiting_for_approval"}:
                return {**latest, "resumed": True}

    attempt = len(records) + 1
    remote = (show["task"].get("body") or "").partition(REMOTE_MARKER)[2].strip()
    if not remote:
        raise WorkflowError(f"{task_id}: card has no remote task section")
    message = remote.replace(TASK_TOKEN, task_id)
    if note:
        message = (f"Attempt {attempt} for Kanban card {task_id}. Continue in ~/hermes-work/{task_id} "
                   f"on the same branch.\n{redact(note)}\n\n{message}")
    idem = f"kanban:{task_id}:{attempt}"
    started = hermes_json(run, ["-p", lane, "peer", "run", target, message, "--idempotency-key", idem, "--json"])
    if not started.get("run_id"):
        raise WorkflowError(f"peer '{target}' returned no run id")
    rec = {"transport": PEER_TRANSPORT, "lane": lane, "peer": target, "run_id": str(started["run_id"]),
           "session_id": str(started.get("session_id") or ""), "attempt": attempt,
           "idempotency_key": idem, "events_seen": len(show.get("events") or [])}
    r = run(["kanban", "comment", task_id, RECORD_PREFIX + json.dumps(rec, sort_keys=True),
             "--author", "kanban-workflow"])
    if r.returncode != 0:
        raise WorkflowError(f"run {rec['run_id']} started; recording it on {task_id} failed "
                            f"(rerun dispatch, the peer replays the same run): {(r.stderr or r.stdout).strip()[:200]}")
    return {**rec, "resumed": bool(started.get("replayed"))}


def remote_report(output: str) -> Optional[dict]:
    """The last fenced json block of the remote agent's final reply."""
    blocks = re.findall(r"```json\s*\n(.*?)```", output or "", re.S)
    for block in reversed(blocks):
        try:
            report = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict):
            return report
    return None


def settle(task_id: str, rec: dict, remote: dict) -> Optional[tuple]:
    """(exit code, result) for a remote run that needs the relay to act, else None."""
    state = remote.get("status")
    base = {"task": task_id, "peer": rec["peer"], "run_id": rec["run_id"], "remote_status": state}
    if state == "waiting_for_approval":
        what = ((remote.get("approval") or {}).get("description") or "a command")[:200]
        return 4, {**base, "blocker": f"human-approval: the {rec['peer']} worker waits for approval of {what} "
                                      f"(run {rec['run_id']}); approve or deny it there, then unblock"}
    if state in FAILED:
        return 1, {**base, "error": f"remote run {state}: {(remote.get('error') or '')[:300]}"}
    if state != "completed":
        return None
    report = remote_report(remote.get("output") or "")
    if report is None:
        return 1, {**base, "error": "remote run completed without the JSON report"}
    if str(report.get("task") or task_id) != task_id:
        return 1, {**base, "error": f"remote report is for task {report.get('task')}, not {task_id}"}
    if report.get("status") == "blocked":
        return 4, {**base, "blocker": str(report.get("blocker") or "remote worker blocked without a reason")[:500]}
    if report.get("status") != "ready_for_review":
        return 1, {**base, "error": f"remote report status is {report.get('status')!r}"}
    meta = {k: report.get(k) for k in ("branch", "commit", "tests") if report.get(k)}
    meta.update({"transport": PEER_TRANSPORT, "peer": rec["peer"], "run_id": rec["run_id"]})
    return 0, {**base, "summary": str(report.get("summary") or "")[:2000], "metadata": meta}


def wait(task_id: str, timeout: float, interval: float, run: Runner = default_runner,
         sleep: Callable[[float], None] = time.sleep) -> tuple:
    """Poll the recorded remote run. Exit codes: 0 ready for review, 4 blocked
    (human gate), 1 failed or no report, 3 still running after `timeout` seconds."""
    show = hermes_json(run, ["kanban", "show", task_id, "--json"])
    lane, target = peer_lane(show)
    records = peer_records(show)
    if not records:
        raise WorkflowError(f"{task_id}: no peer run recorded; run `dispatch {task_id}` first")
    rec = records[-1]
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        remote = peer_status(run, lane, target, rec["run_id"])
        settled = settle(task_id, rec, remote)
        if settled:
            code, result = settled
            return code, redact(result)
        left = deadline - time.monotonic()
        if left <= 0:
            return 3, {"task": task_id, "peer": rec["peer"], "run_id": rec["run_id"],
                       "remote_status": remote.get("status")}
        sleep(min(interval, left))


# -------------------------------------------------------------------- gate ---

def review_verdict(show: dict) -> tuple:
    """(ok, reason) for one lane card from `hermes kanban show --json`.

    Passes only when the card is done, its last review request was followed by a
    completion, and that completion came from someone other than the implementer."""
    task = show.get("task") or {}
    status = task.get("status")
    if status != "done":
        return False, f"status is {status}"
    if card_header(task.get("body") or "").get("transport") == PEER_TRANSPORT and not peer_records(show):
        return False, "peer lane has no recorded peer run (the work did not run on the worker)"
    events = show.get("events") or []
    asked = [i for i, e in enumerate(events) if e.get("kind") == "review_requested"]
    if not asked:
        return False, "completed without a review request"
    last = asked[-1]
    implementer = (events[last].get("payload") or {}).get("implementer") or ""
    after = [e.get("kind") for e in events[last + 1:]]
    if "completed" not in after:
        return False, "no completion after the last review request"
    if "changes_requested" in after[: after.index("completed")]:
        return False, "changes were requested and not re-reviewed"
    approvals = [r for r in show.get("runs") or [] if r.get("outcome") == "completed"]
    if not approvals:
        return False, "no completing run recorded"
    approver = approvals[-1].get("profile") or ""
    if approver == implementer:
        return False, f"self-approved by {implementer}"
    return True, f"approved by {approver} (implementer {implementer})"


def gate(task_id: str, run: Runner = default_runner) -> tuple:
    request = hermes_json(run, ["kanban", "show", task_id, "--json"])
    parents = request.get("parents") or []
    if not parents:
        return False, [{"id": task_id, "ok": False, "reason": "request card has no lane cards"}]
    rows = []
    for pid in parents:
        ok, reason = review_verdict(hermes_json(run, ["kanban", "show", pid, "--json"]))
        rows.append({"id": pid, "ok": ok, "reason": reason})
    return all(r["ok"] for r in rows), rows


def status(task_id: str, run: Runner = default_runner) -> list:
    """One row per card; peer lanes add transport, peer and the latest remote run id
    (read from the card, no network call)."""
    request = hermes_json(run, ["kanban", "show", task_id, "--json"])
    rows = []
    for tid in [*(request.get("parents") or []), task_id]:
        show = request if tid == task_id else hermes_json(run, ["kanban", "show", tid, "--json"])
        t = show["task"]
        row = {"id": tid, "status": t.get("status"), "assignee": t.get("assignee"), "title": t.get("title")}
        header = card_header(t.get("body") or "")
        if header.get("transport") == PEER_TRANSPORT:
            records = peer_records(show)
            row.update({"transport": PEER_TRANSPORT, "peer": header.get("peer"),
                        "run_id": records[-1]["run_id"] if records else None, "attempts": len(records)})
        rows.append(row)
    return rows


# --------------------------------------------------------------------- CLI ---

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("intake", help="create the lane cards, the request card and the Discord subscriptions")
    i.add_argument("--title", required=True)
    i.add_argument("--body", default="", help="the operator's request, verbatim (default: the title)")
    i.add_argument("--lane", action="append", default=[],
                   help="mac, windows, or any profile name (repeatable)")
    i.add_argument("--reviewer", default=os.environ.get("HERMES_WORKFLOW_REVIEWER", "reviewer"))
    i.add_argument("--orchestrator", default=os.environ.get("HERMES_WORKFLOW_ORCHESTRATOR", "default"))
    i.add_argument("--repo", default="", help="repository URL or path the lanes work on")
    i.add_argument("--key", default="", help="idempotency key (default: discord:<chat>:<message>)")
    i.add_argument("--max-runtime", default="2h", help="per-run cap for lane cards ('' for none)")
    i.add_argument("--peer", action="append", default=[], metavar="LANE=PEER[/PROFILE]",
                   help="peer target for a lane (default: mac-worker=mac, windows-worker=windows)")
    i.add_argument("--dry-run", action="store_true", help="print the plan, create nothing")
    for name in ("chat-id", "thread-id", "message-id", "user-id", "chat-type"):
        i.add_argument(f"--discord-{name}", default="")

    for name, text in (("gate", "exit 0 iff every lane card passed independent review"),
                       ("status", "show the request card and its lane cards")):
        s = sub.add_parser(name, help=text)
        s.add_argument("task_id")
        s.add_argument("--json", action="store_true")

    d = sub.add_parser("dispatch", help="peer lane: start or resume the remote run and record it on the card")
    d.add_argument("task_id")
    w = sub.add_parser("wait", help="peer lane: poll the remote run; exit 0 review, 4 blocked, 1 failed, 3 running")
    w.add_argument("task_id")
    w.add_argument("--timeout", type=float, default=540, help="seconds to keep polling (0: poll once)")
    w.add_argument("--interval", type=float, default=30, help="seconds between polls")
    return p


def main(argv=None, run: Runner = default_runner) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "intake":
            print(json.dumps(intake(args, run), indent=2))
            return 0
        if args.cmd == "dispatch":
            print(json.dumps(dispatch(args.task_id, run), indent=2))
            return 0
        if args.cmd == "wait":
            code, result = wait(args.task_id, args.timeout, args.interval, run)
            print(json.dumps(result, indent=2))
            return code
        if args.cmd == "gate":
            ok, rows = gate(args.task_id, run)
            if args.json:
                print(json.dumps({"ok": ok, "lanes": rows}, indent=2))
            else:
                for r in rows:
                    print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['id']}  {r['reason']}")
                print("review gate: PASS" if ok else "review gate: FAIL")
            return 0 if ok else 1
        rows = status(args.task_id, run)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                peer = f"  [peer {r['peer']} run {r['run_id'] or '-'}]" if r.get("transport") else ""
                print(f"{r['id']}  {r['status']:<8} {r['assignee'] or '-':<16} {r['title']}{peer}")
        return 0
    except WorkflowError as exc:
        print(f"kanban-workflow: {redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
