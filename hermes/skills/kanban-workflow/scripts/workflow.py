#!/usr/bin/env python3
"""Standard Hermes workflow: Discord request -> Kanban cards -> isolated workers
-> review gate -> final Discord report.

    workflow.py intake --title T --body B --lane mac --lane windows   # build the card graph
    workflow.py gate   <request_id>                                    # exit 0 iff every lane passed review
    workflow.py status <request_id>                                    # one line per card

Card graph (Hermes `--parent` means "waits for"):

    [mac] T  ─┐  lane cards: run on the worker, end with kanban_request_review
    [win] T  ─┴─> Report: T  request card: waits for the lanes, runs `gate`, writes the
                             report; its completion wakes the orchestrator in the
                             originating Discord thread (notify+wake subscription)

Everything goes through the `hermes kanban` CLI (no direct SQLite access), so the
dispatcher, review dispatch and gateway notifier stay the source of truth.
Idempotent: every card carries an idempotency key derived from the Discord
message, so a retried intake returns the same cards. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

SCRIPT = Path(__file__).resolve()
LANES = {"mac": "mac-worker", "windows": "windows-worker"}
LANE_LABELS = {"mac-worker": "mac", "windows-worker": "win"}
ISOLATED_BACKEND = "ssh"

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


def check_profiles(run: Runner, lanes: list, reviewer: str, allow_local: bool) -> None:
    """Refuse to build a graph the dispatcher cannot finish: every assignee must
    exist, the mac/windows lanes must use the SSH terminal backend (isolation),
    and the reviewer must not be one of the implementers."""
    problems = []
    if reviewer in lanes:
        problems.append(f"reviewer '{reviewer}' is also a lane; review must be independent")
    for name in [*lanes, reviewer]:
        r = run(["-p", name, "config", "get", "terminal.backend"])
        if r.returncode != 0:
            problems.append(f"profile '{name}' does not exist (see docs/discord-kanban-workflow.md)")
            continue
        backend = (r.stdout or "").strip().splitlines()[-1:] or [""]
        if name in LANE_LABELS and backend[0] != ISOLATED_BACKEND and not allow_local:
            problems.append(f"lane '{name}' terminal.backend is '{backend[0]}', not '{ISOLATED_BACKEND}'; "
                            "it would run on this host, not the worker")
    if problems:
        raise WorkflowError("; ".join(problems))


HUMAN_GATES = """\
Human gates (mandatory):
- Never merge, deploy, release, publish, or change production. Those need the operator's explicit approval.
- Never type or request passwords, tokens, MFA codes or keys. At any login, MFA,
  payment, admin/UAC or credential prompt, stop and call
  kanban_block(reason="human-approval: <what you hit>"). The operator finishes it and unblocks.
- Never send Discord messages yourself; the board notifies the requester."""


def lane_body(request: str, lane: str, reviewer: str, repo: str, key: str) -> str:
    where = {
        "mac-worker": "the Mac worker",
        "windows-worker": "the Windows worker",
    }.get(lane, f"the '{lane}' lane")
    repo_line = f"Repository: {repo}\n" if repo else ""
    return f"""\
Request (from Discord, verbatim):
{request}

{repo_line}Lane: {lane} — your terminal runs on {where}.
Workflow key: {key}

Contract:
1. Work in a fresh per-task directory on the worker: ~/hermes-work/$HERMES_KANBAN_TASK.
   Clone or fetch the repository there. Never reuse another task's checkout.
2. Use a feature branch (feat/... or fix/...). Never commit to main.
3. Build and test on the worker; keep the exact commands and their output.
4. Push the feature branch so the reviewer can read it. Opening a draft PR is fine; merging is not.
5. Finish with kanban_request_review(reviewer="{reviewer}", summary=<what changed and how
   it was verified>, metadata={{"branch": ..., "commit": ..., "tests": ...}}).
   Do not call kanban_complete yourself: the reviewer approves. The "Report:" card that
   waits on this one is the final report, not a review phase; it does not replace review.
6. If you cannot finish, call kanban_block with the exact blocker.

{HUMAN_GATES}
"""


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
2. Read each lane card (kanban_show) for branch, commit, tests and the review verdict.
3. Call kanban_complete(summary=<one-line outcome>, result=<report>). The report lists, per lane:
   status, branch and commit, tests run and their result, reviewer verdict; then
   "Needs your approval:" with every merge, deploy, release or login still open.
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
    origin = discord_origin(args)
    base = idempotency_base(args.key, origin)
    request = args.body or args.title
    title = short_title(args.title)

    plan = {
        "key": base,
        "lanes": [{"assignee": lane, "title": f"[{LANE_LABELS.get(lane, lane)}] {title}",
                   "idempotency_key": f"{base}:{lane}"} for lane in lanes],
        "request": {"assignee": args.orchestrator, "title": f"Report: {title}",
                    "idempotency_key": f"{base}:request"},
        "reviewer": args.reviewer,
        "discord": origin,
    }
    if args.dry_run:
        plan["dry_run"] = True
        return plan

    check_profiles(run, lanes, args.reviewer, args.allow_local_lane)
    work_ids = []
    for spec in plan["lanes"]:
        card = create_card(run, spec["title"], spec["assignee"],
                           lane_body(request, spec["assignee"], args.reviewer, args.repo, base),
                           spec["idempotency_key"], max_runtime=args.max_runtime)
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


# -------------------------------------------------------------------- gate ---

def review_verdict(show: dict) -> tuple:
    """(ok, reason) for one lane card from `hermes kanban show --json`.

    Passes only when the card is done, its last review request was followed by a
    completion, and that completion came from someone other than the implementer."""
    task = show.get("task") or {}
    status = task.get("status")
    if status != "done":
        return False, f"status is {status}"
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
    request = hermes_json(run, ["kanban", "show", task_id, "--json"])
    rows = []
    for tid in [*(request.get("parents") or []), task_id]:
        t = (request if tid == task_id else hermes_json(run, ["kanban", "show", tid, "--json"]))["task"]
        rows.append({"id": tid, "status": t.get("status"), "assignee": t.get("assignee"), "title": t.get("title")})
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
    i.add_argument("--allow-local-lane", action="store_true",
                   help="accept mac/windows lane profiles that do not use the SSH backend")
    i.add_argument("--dry-run", action="store_true", help="print the plan, create nothing")
    for name in ("chat-id", "thread-id", "message-id", "user-id", "chat-type"):
        i.add_argument(f"--discord-{name}", default="")

    for name, text in (("gate", "exit 0 iff every lane card passed independent review"),
                       ("status", "show the request card and its lane cards")):
        s = sub.add_parser(name, help=text)
        s.add_argument("task_id")
        s.add_argument("--json", action="store_true")
    return p


def main(argv=None, run: Runner = default_runner) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "intake":
            print(json.dumps(intake(args, run), indent=2))
            return 0
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
                print(f"{r['id']}  {r['status']:<8} {r['assignee'] or '-':<16} {r['title']}")
        return 0
    except WorkflowError as exc:
        print(f"kanban-workflow: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
