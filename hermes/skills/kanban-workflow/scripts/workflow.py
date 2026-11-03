#!/usr/bin/env python3
"""Standard Hermes engineering workflow: one central code writer, evidence-only
platform peers, automatic fix/retest rounds, independent review, Discord report.

    workflow.py intake     --title T --body B --repo URL --lane mac --lane windows   # round 1 + report card
    workflow.py intake     --title T --body B --repo URL --lane mac --mobile ios     # iOS/iPad-first mobile run
    workflow.py intake     ... --checkout /opt/repos/app                  # code in that existing checkout, not a clone
    workflow.py preflight  <code_id> --repo-dir DIR     # --checkout card: lease + writer scan + provenance (0/2/3)
    workflow.py checkpoint <code_id> --repo-dir DIR     # --checkout card: record this card's own dirty files
    workflow.py release    <code_id> --repo-dir DIR     # --checkout card: give the lease back
    workflow.py record-sha <code_id> --repo-dir DIR [--notes N]   # code card: record the pushed exact SHA
    workflow.py dispatch   <validate_id>                          # validation card: start/resume the peer run
    workflow.py wait       <validate_id> [--timeout S]            # validation card: poll, record evidence
    workflow.py advance    <round_id>                             # round card: review, or the next fix round
    workflow.py verdict    <review_id> approve|changes [--reason R]
    workflow.py gate       <report_id>                            # exit 0 iff the final SHA passed and was approved
    workflow.py status     <any card id> [--json]                 # one row and state per card

Card graph (Hermes `--parent` means "waits for"):

    [code 1] T   coordinator (Claude Opus/Fable): edit, commit, push, record the exact SHA
      ├─> [mac 1] T   relay -> Mac's own Hermes: checkout SHA, build, test, install, launch -> evidence
      └─> [win 1] T   relay -> Windows' own Hermes: same -> evidence
            └─> [round 1] T   coordinator runs `advance`:
                  every lane passed -> Review: T (independent reviewer: exact diff + evidence)
                  any lane failed   -> [code 2] (the failure evidence) -> [mac 2] [win 2] -> [round 2] ...
    Report: T   waits for every round and review card; runs `gate`; its completion wakes the
                orchestrator in the originating Discord thread.

iOS/iPad-first mobile validation (`--mobile ios`) is staged, one exact SHA throughout:

    writer (coordinator pushes the SHA) -> mac: iPhone simulator AND iPad simulator
    (build, test, install, launch, screenshot) -> browserstack: real iPhone AND iPad devices
    ; windows: deferred (same flow once the Windows worker is repaired)

    [round N] all simulators passed + BrowserStack active
      └─> [devices N] relay -> Mac: signed build + XCUITest runner -> BrowserStack build -> build id
            └─> [devices-round N] `advance`: passed -> Review ; failed -> [code N+1] ...

The BrowserStack stage is disabled and fails closed (browserstack.py): it runs only
after the operator writes the 0600 activation file with credentials and devices.
Disabled, it is reported "skipped: disabled" (never run, never passed); a partial
activation blocks the round. A device pass is never the Mac's word: this host
re-reads the build from the BrowserStack API and needs the exact SHA as build tag
and a passed session on a real iPhone and a real iPad.

Only the coordinator writes code. Peers check out the exact commit, never edit,
commit or push, and return a structured evidence block that this script parses,
validates against the SHA and stores on the card (not the model's prose).

Every record (peer run, evidence, pushed SHA, round outcome, review verdict) is
written first to a local append-only ledger under the shared Kanban root and then
mirrored to the card as a comment; a failed comment is re-synced by the next
command, thus a started remote run is never lost. Mutating commands refuse to run
inside a delegate_task child, where Hermes forbids Kanban writes.

Everything goes through the `hermes kanban` / `hermes peer` CLIs and `git` (no
direct SQLite access). Idempotent: every card carries an idempotency key derived
from the Discord message and the round. Stdlib only.
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
sys.dont_write_bytecode = True  # keep __pycache__ out of the installed skill dir
sys.path.insert(0, str(SCRIPT.parent))
import browserstack as bs  # noqa: E402
import repo_lease as rl  # noqa: E402

LANES = {"mac": "mac-worker", "windows": "windows-worker"}
LANE_LABELS = {"mac-worker": "mac", "windows-worker": "win"}
LANE_WHERE = {"mac-worker": "the Mac worker", "windows-worker": "the Windows worker"}
LANE_PLATFORM = {
    "mac-worker": "macOS: Xcode builds and tests, iOS/iPadOS simulators or a connected device, macOS packaging",
    "windows-worker": "Windows: Android Studio/Gradle builds and tests, emulators or a connected device, "
                      "MSBuild/.NET, PowerShell, Windows packaging",
}
# Lanes that run on another machine's own Hermes, and their default peer target
# (`hermes -p <lane> peer add <target> ...`; `--peer lane=target` overrides).
PEER_LANES = {"mac-worker": "mac", "windows-worker": "windows"}
# iOS/iPad-first mobile validation (`--mobile ios`, docs/mobile-validation.md).
MOBILE_TARGETS = ("ios",)
MOBILE_LANE = "mac-worker"
MAC_REPO_ROOT = "/Users/norkz/Documents"  # the Mac worker's clones live under here
MAC_WORKTREES = ".hermes-validate"  # detached per-card worktrees: MAC_REPO_ROOT/MAC_WORKTREES/<card>
SIMULATORS = ("iphone", "ipad")  # both must pass the same SHA
SIM_STEPS = ("build", "test", "install", "launch")
MOBILE_STAGES = ("writer", "mac-iphone", "mac-ipad", "browserstack")  # in order; windows is deferred
DEFERRED_LANES = {"windows-worker": "the Windows lane is deferred until the Windows worker is repaired and "
                                    "is not part of the iOS/iPad mobile activation (docs/mobile-validation.md); "
                                    "run --mobile ios with --lane mac only"}
# Real-device stage after both local simulators pass (browserstack.py, docs/mobile-validation.md).
# Disabled until the operator writes the activation file; it never passes without API-verified evidence.
BROWSERSTACK_FLAG = bs.FLAG
DEVICE_STAGE = "devices"  # `Stage:` header of the BrowserStack device card and its round card
BROWSERSTACK_HTTP: Optional[bs.Http] = None  # test hook: the API transport for device verification
PEER_TRANSPORT = "hermes-peer"
RELAY_BACKEND = "local"  # the relay runs this script, thus on this host
PEER_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}(/[A-Za-z0-9][A-Za-z0-9_-]{0,63})?$")
BRANCH_RE = re.compile(r"^(?!/)(?!.*(?:\.\.|//|@\{|\.lock$))[A-Za-z0-9._/-]{1,120}(?<![/.])$")
CHECKOUT_RE = re.compile(r"^/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$")
REPO_URL_RE = re.compile(r"^(?:(?:https?|ssh|git|file)://\S+|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:\S+)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
RUNTIME_RE = re.compile(r"^(?:[0-9]+[smhd]?)?$")
PROTECTED_BRANCHES = {"main", "master", "develop", "release", "production"}
KINDS = ("code", "validate", "round", "review", "report")
REQUEST_MARKER = "===== Request (verbatim) ====="
TASK_TOKEN = "$HERMES_KANBAN_TASK"
RECORD_TYPES = ("peer-run", "evidence", "code-sha", "round", "verdict")
MAX_ATTEMPTS = 3  # automatic peer attempts per validation card; an operator unblock adds one
RUNNING = {"queued", "started", "running", "stopping"}
FAILED = {"failed", "cancelled", "interrupted"}
STATUS_ALIASES = {
    "pass": "pass", "passed": "pass", "ok": "pass", "success": "pass", "succeeded": "pass", "green": "pass",
    "fail": "fail", "failed": "fail", "failure": "fail", "error": "fail", "red": "fail",
    "blocked": "blocked", "block": "blocked",
}
EVIDENCE_RE = re.compile(r"BEGIN[-_ ]EVIDENCE\s*\n(.*?)\n\s*END[-_ ]EVIDENCE", re.S | re.I)
FENCE_RE = re.compile(r"(?:```|~~~)[ \t]*(?:json|JSON)?[ \t]*\n(.*?)(?:```|~~~)", re.S)
SECRET_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD))(\s*[=:]\s*)(?!<)[^\s'\"]+"), r"\1\2[REDACTED]"),
]

Runner = Callable[[list], subprocess.CompletedProcess]
GitRunner = Callable[[list, str], subprocess.CompletedProcess]


class WorkflowError(RuntimeError):
    pass


def default_runner(argv: list) -> subprocess.CompletedProcess:
    hermes = os.environ.get("HERMES_BIN", "hermes")
    return subprocess.run([hermes, *argv], capture_output=True, text=True, timeout=120)


def default_git(argv: list, cwd: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, timeout=120, env=env)


def hermes_json(run: Runner, argv: list):
    r = run(argv)
    if r.returncode != 0:
        raise WorkflowError(f"hermes {' '.join(argv[:3])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"hermes {' '.join(argv[:3])} returned non-JSON output") from exc


def card(run: Runner, task_id: str) -> dict:
    return hermes_json(run, ["kanban", "show", task_id, "--json"])


SUPERVISED_MARKER = "HERMES_KANBAN_SUPERVISED_TERMINAL"  # set by Hermes on the worker's own terminal only


def require_worker_context(action: str) -> None:
    """Refuse before any side effect when this is not a place Hermes lets write to the board:
    a delegate_task child's terminal, a cron run or any other fenced descendant. The card's
    own worker terminal carries the fence too, plus the supervised-terminal identity
    (HERMES_KANBAN_SUPERVISED_TERMINAL with HERMES_KANBAN_TASK); the orchestrator's Discord
    terminal carries neither."""
    fenced = bool(os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"))
    supervised = os.environ.get(SUPERVISED_MARKER) == "1" and bool(os.environ.get("HERMES_KANBAN_TASK"))
    if fenced and not supervised:
        raise WorkflowError(f"{action} must run in the card's own worker terminal, not inside delegate_task or a "
                            "cron run (Hermes refuses Kanban writes from delegate_task children)")


def own_card(task_id: str) -> None:
    """A worker terminal acts for its own card only (``$HERMES_KANBAN_TASK``)."""
    env_task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if env_task and env_task != task_id:
        raise WorkflowError(f"this terminal belongs to card {env_task}, not {task_id}")


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


def clip(value, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def defang(text: str) -> str:
    """Peer text placed in a card body must not forge our section markers."""
    return text.replace("=====", "==")


# ---------------------------------------------------------------- headers ---

def header_block(fields: list) -> str:
    return "\n".join(f"{k}: {v}" for k, v in fields if v not in (None, ""))


def card_header(body: str) -> dict:
    """`Key: value` lines before the first blank line of a body that opens with
    `Workflow:`. Only this script writes such bodies; the request always comes
    after the header, thus a request cannot spoof it."""
    fields = {}
    if not (body or "").startswith("Workflow:"):
        return fields
    for line in body.split("\n\n", 1)[0].splitlines():
        name, sep, value = line.partition(":")
        if sep:
            fields[name.strip().lower()] = value.strip()
    return fields


def header_of(show: dict) -> dict:
    return card_header((show.get("task") or {}).get("body") or "")


def request_of(show: dict) -> str:
    return ((show.get("task") or {}).get("body") or "").partition(REQUEST_MARKER)[2].strip("\n")


class Ctx:
    """The request-wide settings every card header repeats, thus any card can
    open the next round without a side table."""
    FIELDS = (("key", "Key"), ("title", "Title"), ("repo", "Repo"), ("base", "Base"), ("branch", "Branch"),
              ("lanes", "Lanes"), ("coder", "Coder"), ("reviewer", "Reviewer"), ("orchestrator", "Orchestrator"),
              ("max_rounds", "Max-rounds"), ("max_runtime", "Max-runtime"), ("code_max_runtime", "Code-max-runtime"),
              ("mobile", "Mobile"), ("checkout", "Checkout"))

    def __init__(self, **kw):
        for name, _ in self.FIELDS:
            setattr(self, name, kw.get(name, ""))
        self.lanes = dict(self.lanes or {})
        self.max_rounds = int(self.max_rounds or 5)

    def header(self) -> list:
        out = []
        for name, label in self.FIELDS:
            value = getattr(self, name)
            if name == "lanes":
                value = ", ".join(f"{lane}={target}" for lane, target in value.items())
            out.append((label, value))
        return out

    @classmethod
    def from_header(cls, h: dict) -> "Ctx":
        lanes = {}
        for item in (h.get("lanes") or "").split(","):
            lane, sep, target = item.strip().partition("=")
            if sep:
                lanes[lane] = target
        kw = {name: h.get(label.lower(), "") for name, label in cls.FIELDS if name != "lanes"}
        ctx = cls(**kw, lanes=lanes)
        if not (ctx.key and ctx.repo and ctx.branch and ctx.lanes and ctx.coder and ctx.reviewer):
            raise WorkflowError("card header is missing workflow settings (not a card from this workflow)")
        if ctx.checkout and not CHECKOUT_RE.match(ctx.checkout):
            raise WorkflowError(f"card header has an invalid checkout '{ctx.checkout}'")
        return ctx


def kind_of(show: dict, want: str) -> dict:
    h = header_of(show)
    if h.get("workflow") != want:
        tid = (show.get("task") or {}).get("id")
        raise WorkflowError(f"{tid} is not a {want} card (no 'Workflow: {want}' header)")
    return h


# ------------------------------------------------------- durable records ---

def state_dir() -> Path:
    """Run ledger under the shared Kanban root, thus every profile on this host
    (relay, coordinator, reviewer) reads the same files; leases and manifests
    (repo_lease.py) live next to it."""
    return rl.state_root()


def ledger_path(task_id: str) -> Path:
    return state_dir() / (re.sub(r"[^A-Za-z0-9_-]", "_", task_id) + ".jsonl")


def append_ledger(task_id: str, rec: dict) -> None:
    path = ledger_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_ledger(task_id: str) -> list:
    try:
        lines = ledger_path(task_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("type") in RECORD_TYPES:
            out.append(rec)
    return out


def card_records(show: dict) -> list:
    out = []
    for c in show.get("comments") or []:
        body = c.get("body") or ""
        rtype, sep, payload = body.partition(" ")
        if not sep or rtype not in RECORD_TYPES or c.get("author") != "kanban-workflow":
            continue
        try:
            rec = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rec.setdefault("type", rtype)
            rec.setdefault("rid", rec.get("idempotency_key") or rec.get("run_id") or payload)
            out.append(rec)
    return out


def records(show: dict, rtype: str) -> list:
    """Ledger + card records of one type, merged by `rid` (later writes win), oldest first."""
    task_id = (show.get("task") or {}).get("id") or ""
    merged, order = {}, []
    for rec in [*card_records(show), *read_ledger(task_id)]:
        if rec.get("type") != rtype:
            continue
        rid = rec.get("rid")
        if rid not in merged:
            order.append(rid)
            merged[rid] = dict(rec)
        elif float(rec.get("ts") or 0) >= float(merged[rid].get("ts") or 0):
            merged[rid].update(rec)
    out = [merged[rid] for rid in order]
    out.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def comment_record(run: Runner, task_id: str, rec: dict) -> bool:
    r = run(["kanban", "comment", task_id, f"{rec['type']} " + json.dumps(rec, sort_keys=True),
             "--author", "kanban-workflow"])
    return r.returncode == 0


def put_record(run: Runner, task_id: str, rtype: str, rec: dict) -> bool:
    """Ledger first (durable before anything else can fail), then the card.
    Returns whether the card copy landed; `sync_records` retries it later."""
    rec = {**rec, "type": rtype, "ts": rec.get("ts") or time.time()}
    rec.setdefault("rid", f"{rtype}:{rec['ts']}")
    append_ledger(task_id, rec)
    return comment_record(run, task_id, rec)


def sync_records(run: Runner, show: dict) -> None:
    """Mirror ledger records the card does not show yet (a comment that failed)."""
    task_id = (show.get("task") or {}).get("id") or ""
    on_card = {(r.get("type"), r.get("rid"), bool(r.get("run_id"))) for r in card_records(show)}
    for rec in read_ledger(task_id):
        if rec.get("type") == "peer-run" and not rec.get("run_id"):
            continue  # a pending marker; the card gets the started run
        if (rec.get("type"), rec.get("rid"), bool(rec.get("run_id"))) not in on_card:
            if comment_record(run, task_id, rec):
                on_card.add((rec.get("type"), rec.get("rid"), bool(rec.get("run_id"))))


def latest(show: dict, rtype: str, **match) -> Optional[dict]:
    for rec in reversed(records(show, rtype)):
        if all(rec.get(k) == v for k, v in match.items()):
            return rec
    return None


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
    """{lane profile: peer target} for every lane. Mac/Windows lanes default to the
    `mac`/`windows` peers; `--peer lane=target` renames a target or adds a lane."""
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
    missing = [lane for lane in lanes if lane not in peers]
    if missing:
        raise WorkflowError(f"lane(s) {', '.join(missing)} have no peer: validation lanes run on a worker's own "
                            "Hermes (mac, windows, or --peer <lane>=<peer>); host-side tests run on the code card")
    return peers


def check_repo(repo: str) -> str:
    """Peers check out by URL; a coordinator path (/opt/repos/...) does not exist on them."""
    repo = (repo or "").strip()
    if not repo:
        raise WorkflowError("--repo is required: the clone URL every lane checks out (https://..., git@host:org/repo)")
    if not REPO_URL_RE.match(repo):
        raise WorkflowError(f"--repo '{repo}' is not a clone URL; peers cannot reach a path on this host. "
                            "Pass the remote URL (git -C <path> remote get-url origin)")
    return repo


def same_remote(a: str, b: str) -> bool:
    norm = lambda url: url.strip().rstrip("/").removesuffix(".git")  # noqa: E731
    return norm(a) == norm(b)


def check_checkout(path: str, repo: str, git: GitRunner) -> str:
    """`--checkout`: the operator's existing checkout on this host that every code card of
    the request works in instead of a fresh clone (peers still check out by URL). It must
    be the canonical absolute path of a git working tree whose origin is --repo."""
    path = (path or "").strip()
    if not path:
        return ""
    if not CHECKOUT_RE.match(path) or any(part in (".", "..") for part in path.split("/")):
        raise WorkflowError(f"--checkout '{path}' must be a plain absolute path (no spaces, '.' or '..')")
    real = str(Path(path).resolve())
    if real != path:
        raise WorkflowError(f"--checkout '{path}' is not canonical; pass the resolved path '{real}'")
    work = str(Path("~/hermes-work").expanduser().resolve())
    if path == work or path.startswith(work + "/"):
        raise WorkflowError(f"--checkout '{path}' is a card's fresh checkout under {work}; omit --checkout for that")
    if not Path(path).is_dir():
        raise WorkflowError(f"--checkout '{path}' is not an existing directory on this host")
    r = git(["rev-parse", "--show-toplevel"], path)
    top = (r.stdout or "").strip() if r.returncode == 0 else ""
    if not top or str(Path(top).resolve()) != path:
        raise WorkflowError(f"--checkout '{path}' is not the top of a git working tree"
                            + (f" (its top is '{top}')" if top else ""))
    r = git(["remote", "get-url", "origin"], path)
    origin = (r.stdout or "").strip() if r.returncode == 0 else ""
    if not same_remote(origin, repo):
        raise WorkflowError(f"--checkout '{path}' has origin '{redact(origin) or 'none'}', not --repo '{repo}'")
    return path


def default_branch(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-") or "work"
    return f"feat/{slug}"


def check_branch(branch: str, base: str) -> str:
    if not BRANCH_RE.match(branch or ""):
        raise WorkflowError(f"invalid branch name '{branch}'")
    if branch == base or branch in PROTECTED_BRANCHES:
        raise WorkflowError(f"branch '{branch}' is protected; the coordinator writes to a feature branch")
    return branch


def check_settings(args) -> None:
    """Every value that lands in a card header is one plain token."""
    if not BRANCH_RE.match(args.base or ""):
        raise WorkflowError(f"invalid base branch '{args.base}'")
    for what in ("coder", "reviewer", "orchestrator"):
        if not PROFILE_RE.match(getattr(args, what) or ""):
            raise WorkflowError(f"invalid --{what} profile name '{getattr(args, what)}'")
    for what in ("max_runtime", "code_max_runtime"):
        if not RUNTIME_RE.match(getattr(args, what) or ""):
            raise WorkflowError(f"invalid --{what.replace('_', '-')} '{getattr(args, what)}' (e.g. 90m, 2h)")
    if args.max_rounds < 1:
        raise WorkflowError("--max-rounds must be at least 1")


def check_mobile(mobile: str, lanes: list) -> None:
    """`--mobile ios` runs on the Mac lane only: the Windows lane is deferred and
    BrowserStack is a stage after the Mac lane, not a lane of its own."""
    if not mobile:
        return
    if mobile not in MOBILE_TARGETS:
        raise WorkflowError(f"--mobile '{mobile}': supported: {', '.join(MOBILE_TARGETS)}")
    for lane in lanes:
        if lane in DEFERRED_LANES:
            raise WorkflowError(f"--mobile {mobile}: {DEFERRED_LANES[lane]}")
    if lanes != [MOBILE_LANE]:
        raise WorkflowError(f"--mobile {mobile} validates on the Mac lane only (iPhone and iPad simulators): "
                            f"pass --lane mac, not {', '.join(lanes)}")


def browserstack_stage(env=None) -> dict:
    """The real-device stage as the activation file sets it: "skipped: disabled"
    (nothing runs, never counted as passed), "blocked" for a partial or unsafe
    activation (fail closed), or "queued" with the devices it will run on."""
    act = bs.activation(env)
    if act["state"] == "active":
        return {"stage": "browserstack", "status": "queued", "devices": act["devices"],
                "reason": "real devices: " + ", ".join(act["devices"])}
    if act["state"] == "misconfigured":
        return {"stage": "browserstack", "status": "blocked", "reason": act["reason"]}
    return {"stage": "browserstack", "status": "skipped", "reason": "disabled"}


def stage_text(stage: dict) -> str:
    return f"{stage['status']}: {stage['reason']}" if stage.get("reason") else stage["status"]


def mobile_plan() -> list:
    """The staged order of a mobile run, for --dry-run and the docs."""
    return [{"stage": "writer", "who": "coordinator", "does": "commit, push, record the exact SHA"},
            {"stage": "mac-iphone", "who": MOBILE_LANE, "does": "iPhone simulator: build, test, install, launch"},
            {"stage": "mac-ipad", "who": MOBILE_LANE, "does": "iPad simulator: build, test, install, launch"},
            {**browserstack_stage(), "who": MOBILE_LANE,
             "does": "BrowserStack real iPhone and iPad: XCUITest suite, only after both simulators pass"},
            {"stage": "windows", "status": "deferred", "who": "none",
             "does": "same flow once the Windows worker is repaired; not part of the mobile activation"}]


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


def check_profiles(run: Runner, ctx: Ctx) -> None:
    """Refuse to build a graph the dispatcher cannot finish: every assignee must
    exist, every lane needs its peer registered with a key and a relay that runs
    here, and the reviewer must be neither the coder nor a lane."""
    problems = []
    if ctx.reviewer in ctx.lanes or ctx.reviewer in (ctx.coder, ctx.orchestrator):
        problems.append(f"reviewer '{ctx.reviewer}' also codes, relays or reports; review must be independent")
    if ctx.coder in ctx.lanes:
        problems.append(f"coder '{ctx.coder}' is also a lane; peers only validate")
    for name in dict.fromkeys([*ctx.lanes, ctx.coder, ctx.reviewer, ctx.orchestrator]):
        r = run(["-p", name, "config", "get", "terminal.backend"])
        if r.returncode != 0:
            problems.append(f"profile '{name}' does not exist (see docs/discord-kanban-workflow.md)")
            continue
        if name not in ctx.lanes:
            continue
        backend = ((r.stdout or "").strip().splitlines()[-1:] or [""])[0]
        if backend != RELAY_BACKEND:
            problems.append(f"lane '{name}' terminal.backend is '{backend}', not '{RELAY_BACKEND}'; the relay "
                            f"runs on this host: hermes -p {name} config set terminal.backend local")
            continue
        problem = peer_problem(run, name, ctx.lanes[name])
        if problem:
            problems.append(problem)
    if problems:
        raise WorkflowError("; ".join(problems))


HUMAN_GATES = """\
Human gates (mandatory):
- Never merge, deploy, release, publish, sign, or submit to the App Store or Play Store, and never
  change production. Those need the operator's explicit approval.
- Never type or request passwords, tokens, MFA codes or keys. At any login, MFA, signing,
  payment, admin/UAC or credential prompt, stop and call
  kanban_block(reason="human-approval: <what you hit>"). The operator finishes it and unblocks.
- Never run destructive commands (git reset --hard, clean -fdx, force push, deleting data)
  outside this card's own checkout without the operator's approval.
- Never send Discord messages yourself; the board notifies the requester."""

PROTECTED_LIST = ", ".join(sorted(PROTECTED_BRANCHES))
CODE_HUMAN_GATES = HUMAN_GATES.replace(
    """- Never merge, deploy, release, publish, sign, or submit to the App Store or Play Store, and never
  change production. Those need the operator's explicit approval.""",
    f"""- Merge only one feature branch into another, and only with the operator's explicit approval
  naming that source and that destination. Before each merge, check the checkout, the live tips of
  both branches and the approval; leave conflicts for the operator and never force push. Without
  that approval, do not merge.
- Merges into main or another protected branch ({PROTECTED_LIST}),
  deploys and releases each need their own separate explicit approval; a feature-to-feature
  approval never covers them.
- Never publish, sign, or submit to the App Store or Play Store, and never change production,
  without the operator's explicit approval.""")


SCANNER_RECOVERY = """\
7. A command the security scanner rejects is not a blocker when an approved equivalent
   exists. Read the rejection; when it names one (RECOVERY: dev_test_run for registered
   Go/PostgreSQL tests), make that call once as a new request, verify its result and
   continue. Never retry the rejected command, wrap it in a script, encode it, or ask for a
   weaker scanner or approval mode. Without an approved equivalent, kanban_block naming the
   exact command, the scanner finding and the missing capability."""


def label(lane: str) -> str:
    return LANE_LABELS.get(lane, lane)


def checkout_steps(ctx: Ctx) -> str:
    """Step 1 of a code card: a fresh clone per card, or the operator's designated checkout."""
    if not ctx.checkout:
        return f"""\
1. Work in a fresh checkout on this host: ~/hermes-work/{TASK_TOKEN}.
   git clone {ctx.repo} there. Check out {ctx.branch} when the remote has it, else create it
   from {ctx.base}. Never commit to {ctx.base} or main; never reuse another card's checkout."""
    co = ctx.checkout
    return f"""\
1. Work only in the operator's designated existing checkout on this host: {co}.
   Do not clone. It may hold the operator's own work: never run git reset, clean, stash,
   restore, checkout -- <path>, rm or any command that discards changes in it.
   Never commit to {ctx.base} or main. Before you edit anything, with your own terminal tool
   (never inside delegate_task) run:
   python3 {SCRIPT} preflight {TASK_TOKEN} --repo-dir {co}
   It takes this run's lease on the checkout and checks: origin is {ctx.repo}; no index.lock;
   the checkout is on {ctx.branch} and not diverged from origin/{ctx.branch}; {ctx.branch} is in no
   other worktree; no competing writer (the registered privileged CWD scanner, classified by
   process ancestry and an explicit list: your own shells, scripts and subagents, known
   editors, language servers, dev servers, idle shells and read-only git are not writers;
   another autonomous agent, a git mutation in progress, any other program or script, or
   anything it cannot inspect is); and that every uncommitted path is one this card recorded
   (resume) or the tree is clean. Read its JSON:
   - exit 0: proceed. The lease is yours; re-run preflight before each later edit round.
   - exit 2 with a failing "branch" check and a clean tree: git -C {co} fetch origin, then
     switch: git -C {co} switch {ctx.branch} if the local branch exists, else
     git -C {co} switch --track origin/{ctx.branch} if the remote has it, else
     git -C {co} switch -c {ctx.branch} origin/{ctx.base}; update only by
     git -C {co} merge --ff-only origin/{ctx.branch}; then run preflight again. Note the
     branch it was on in your summary.
   - exit 2 "lease" held by another live run, "writers" naming a foreign agent or git
     mutation, "diverged", "worktrees", "index.lock", or "dirty" with unknown paths: change
     nothing. Wait and retry preflight a few times when the holder may finish (another card
     of this request, a git command); otherwise kanban_block(reason="checkout: <the failing
     check and its detail verbatim>"). Never remove a lease, a lock or a file to pass.
   - exit 3 (unknown visibility: the scanner is missing, failed or incomplete): change
     nothing and block with the detail; the operator registers or repairs the scanner.
   After a coding subagent wrote files and after every edit of your own, before any other
   step (and before running preflight again), record what this card owns so the next run
   can resume it:
   python3 {SCRIPT} checkpoint {TASK_TOKEN} --repo-dir {co}
   It records only what changed since that passing preflight, under the same lease, with a
   clean writer scan (hashes only, outside the repository); it refuses without those, and a
   dirty path that was never this card's never passes preflight, so it is never adopted
   this way. Uncommitted work this card did not record is a conflict for every later run.
   After a commit, run preflight again before the next checkpoint. When the card ends
   (complete or block), run:
   python3 {SCRIPT} release {TASK_TOKEN} --repo-dir {co}
   Every later round of this request works in this same checkout and repeats these steps."""


def code_body(ctx: Ctx, n: int, request: str, feedback: str) -> str:
    fix = ""
    if feedback:
        fix = (f"\nFix this round (from round {n - 1}; fix the cause, never weaken, skip or delete a test):\n"
               f"{defang(feedback)}\n")
    work = ctx.checkout or f"~/hermes-work/{TASK_TOKEN}"
    xcui = ("\n   Mobile: keep XCUITest UI tests that exercise the functions this request changes; the\n"
            "   BrowserStack stage runs that UI test target on real iPhones and iPads when it is active."
            if ctx.mobile else "")
    hint = ("; the Xcode scheme, the UI test target, bundle id and the screens to check on iPhone and iPad"
            if ctx.mobile else "")
    return f"""\
{header_block([("Workflow", "code"), *ctx.header(), ("Round", n)])}

You are the coordinator and the only code writer for this request (round {n}). The Mac and
Windows workers never edit code: they test the exact commit you push and send evidence back.
Failures come back to you as the next round, without the operator.

Steps:
{checkout_steps(ctx)}
2. Write the code with a Claude coding subagent: delegate_task(goal=<the request or the
   failures below, the checkout path, the acceptance criteria>). It runs Claude Opus. For hard
   debugging or broad architecture work, start the goal with a line holding only #fable to
   run Claude Fable. Do not hand coding to the Mac or Windows workers or to a local model.
3. Read the full diff yourself. Run the host-side checks the repo has (lint, unit tests that run here).{xcui}
   Go tests against a registered local PostgreSQL test database run only through the
   dev_test_run tool (profile, packages, race, vet); never a terminal command carrying a
   database URL or a credential file, and never read that file. When dev_test_run is not
   among your tools, no approved equivalent exists on this host: kanban_block naming the
   exact command, the scanner finding and the missing capability (an operator-approved
   dev-test profile); do not assume one will appear.
4. Commit on {ctx.branch} (Conventional Commits, no Co-Authored-By trailer), then
   git push origin {ctx.branch}. Never force push. Never merge unless the merge gates below allow it.
5. With your own terminal tool (never inside delegate_task) run:
   python3 {SCRIPT} record-sha {TASK_TOKEN} --repo-dir {work} --notes "<what changed; what each platform should check{hint}>"
   It checks the tree is clean and the remote branch is at HEAD, and records the exact SHA
   the validators test. Exit 0: kanban_complete(summary=<the "summary" it printed>,
   metadata=<the "metadata" object it printed>). Exit 2: fix what it names (commit, push), run it again.
6. Unfinished work is not a blocker: a running delegate_task child is collected, a failing
   test is diagnosed and fixed until it passes within this card's budget (retry an unchanged
   command once only for a transient failure: network, flaky test, rate limit), a flaky tool
   is retried. Block (kanban_block with the exact
   check, command and output) only for a human decision (credentials, an approval named in
   the gates below, production or destructive actions), a confirmed competing writer, or a
   dependency on another card that cannot resolve from here. Never block for the mere
   existence of uncommitted work this card made: checkpoint it, then continue or commit.
{SCANNER_RECOVERY}

{CODE_HUMAN_GATES}
{fix}
{REQUEST_MARKER}
{request}
"""


def validate_body(ctx: Ctx, n: int, lane: str, code_id: str, request: str, devices: list = ()) -> str:
    """A validation card; with `devices` it is the BrowserStack real-device card of round n."""
    where = LANE_WHERE.get(lane, f"the '{lane}' worker")
    what = ("builds, tests, installs and launches it, and\nreturns structured evidence" if not devices else
            "builds the signed app and its XCUITest\nrunner, runs them on BrowserStack real devices "
            f"({', '.join(devices)}) and returns the\nBrowserStack build id. This host re-reads that build "
            "from the BrowserStack API; the\nworker's word alone never passes the stage")
    return f"""\
{header_block([("Workflow", "validate"), ("Key", ctx.key), ("Round", n), ("Transport", PEER_TRANSPORT),
               ("Peer", ctx.lanes[lane]), ("Lane", lane), ("Code", code_id), ("Repo", ctx.repo),
               ("Branch", ctx.branch), ("Mobile", ctx.mobile), ("Stage", DEVICE_STAGE if devices else ""),
               ("Devices", "|".join(devices))])}

You relay this card to {where}'s own Hermes agent. The worker only tests: it checks out the
exact commit that code card {code_id} pushed, {what}. Neither you nor the worker edits code; the
coordinator fixes failures in the next round.

Relay steps (run each one with your own terminal tool, never inside delegate_task):
1. Run: python3 {SCRIPT} dispatch {TASK_TOKEN}
   It sends the test-only task for the exact SHA with `hermes peer run` (idempotent per card
   and attempt) and records the run id in the run ledger and on this card.
   Exit 2 is a setup error: call kanban_block(reason="peer-setup: <its output>") and stop.
2. Run: python3 {SCRIPT} wait {TASK_TOKEN} --timeout 540
   Repeat while it exits 3. It asks the worker again for a missing evidence block and
   restarts a failed or interrupted run on its own (up to {MAX_ATTEMPTS} attempts).
3. Act on its exit code:
   0  evidence recorded, pass or fail: kanban_complete(summary=<the "summary" it printed>,
      metadata=<the "metadata" object it printed>). A failing build or test is a valid
      result; the round card sends it to the coordinator.
   4  blocked on the worker (login, MFA, signing, device, command approval):
      kanban_block(reason=<the "blocker" it printed>).
   1  no usable run after the retries: kanban_block(reason="peer-run: <its "error">").
   Copy only what the script printed; it redacts secrets. Never call kanban_request_review:
   review runs once, after every lane passed the same SHA.
   Never build, test or fix on this host, and never switch to another transport (SSH included).

{HUMAN_GATES}

{REQUEST_MARKER}
{request}
"""


def round_body(ctx: Ctx, n: int, code_id: str, request: str, stage: str = "") -> str:
    what = ("the BrowserStack real-device stage of validation round" if stage == DEVICE_STAGE
            else "validation round")
    nxt = ("the BrowserStack real-device card for the same SHA when\n     that stage is active "
           "(else, or after it passed, the independent review card)" if ctx.mobile and stage != DEVICE_STAGE
           else "the independent review card")
    return f"""\
{header_block([("Workflow", "round"), *ctx.header(), ("Round", n), ("Code", code_id), ("Stage", stage)])}

You close {what} {n} of this request. Do not edit code, test or review here.

1. With your own terminal tool (never inside delegate_task) run:
   python3 {SCRIPT} advance {TASK_TOKEN}
   It reads every lane's evidence for the SHA that code card {code_id} pushed.
   - Every lane passed: it opens {nxt}.
   - Any lane failed: it opens the next round on its own: a fix card for the coordinator with
     the failure evidence, then fresh validation cards for the new SHA.
2. Exit 0: kanban_complete(summary=<the "summary" it printed>, metadata=<the "metadata" it printed>).
   Exit 4: kanban_block(reason=<the "blocker" it printed>). The operator fixes it (a BrowserStack
   activation, a missing evidence) or unblocks to allow one more round.
   Exit 2: kanban_block(reason="round: <its output>").

{REQUEST_MARKER}
{request}
"""


MOBILE_REVIEW = (" on an iPhone and an iPad simulator (build, test, install, launch, screenshots);\n"
                 "   {devices}; Windows is deferred")
NO_DEVICES = "the BrowserStack real-device stage did not run (disabled): nothing was tested on real devices"


def review_body(ctx: Ctx, n: int, code_id: str, round_id: str, sha: str, request: str, devices: str = "") -> str:
    mobile = MOBILE_REVIEW.format(devices=devices or NO_DEVICES) if ctx.mobile else ""
    scratch = (f"\n   Never read from or write to the coordinator's checkout {ctx.checkout}." if ctx.checkout else "")
    return f"""\
{header_block([("Workflow", "review"), *ctx.header(), ("Round", n), ("Code", code_id),
               ("Round-card", round_id), ("Commit", sha)])}

You are the independent review gate. Every lane passed commit {sha} on {ctx.branch}.
Nothing reaches the operator as done until you approve this exact commit.

1. Run: python3 {SCRIPT} status {TASK_TOKEN} --json
   It lists every round, the pushed SHAs and each lane's evidence (SHA tested, commands, exit
   codes, test counts, devices, artifacts, dirty-tree check) and peer run ids.
2. Fetch {ctx.repo} into a scratch directory and read `git diff {ctx.base}...{sha}` in full.{scratch}
3. Check: the diff does what the request asks and nothing else; every lane tested exactly
   {sha}{mobile}; the tests are real and cover the change; no unapproved or unrelated files; no
   secrets; no production side effects (production config, data, release or store settings).
4. Verdict, with your own terminal tool:
   approve: python3 {SCRIPT} verdict {TASK_TOKEN} approve
   changes: python3 {SCRIPT} verdict {TASK_TOKEN} changes --reason "<numbered, actionable list>"
            (this opens the next fix round for the coordinator on its own)
   Exit 0: kanban_complete(summary=<the "summary" it printed>, metadata=<the "metadata" it printed>).
   Exit 4: kanban_block(reason=<the "blocker" it printed>).
   Only for an external blocker (missing access, a human decision): kanban_block.
Review only: never push to, merge, deploy or release the branch, and never edit it.

{REQUEST_MARKER}
{request}
"""


MOBILE_REPORT = """
   Mobile run: per simulator (iPhone, iPad) the name, OS, UDID, test result bundle, install
   and launch exit codes, bundle id and screenshot paths. State the BrowserStack stage exactly
   as the gate prints it: "passed" only with the BrowserStack build id, dashboard URL and the
   real devices the gate lists as verified via the BrowserStack API; otherwise e.g.
   "skipped: disabled" (not tested on real devices). Windows is deferred. Never call a
   skipped, blocked or unverified stage tested or passed."""


def report_body(ctx: Ctx, request: str) -> str:
    mobile = MOBILE_REPORT if ctx.mobile else ""
    return f"""\
{header_block([("Workflow", "report"), ("Key", ctx.key), ("Title", ctx.title)])}

You write the final report. Steps:
1. Run: python3 {SCRIPT} gate {TASK_TOKEN}
   Exit 0 means the final SHA passed every lane and the independent reviewer approved that SHA.
   Any other exit: call kanban_block(reason="review-gate: <its output>") and stop.
2. Run: python3 {SCRIPT} status {TASK_TOKEN} --json
3. Call kanban_complete(summary=<one-line outcome>, result=<report>). The report lists: the
   final branch and SHA; per round the SHA, each lane's verdict (tests, failures, device) and
   peer run id, and what the coordinator fixed; the review verdict; then "Needs your
   approval:" with every merge, release, signing, store submission, production change or
   login still open.{mobile} The gateway posts the outcome to the Discord thread and wakes the
   orchestrator there to deliver the report.

{HUMAN_GATES}

{REQUEST_MARKER}
{request}
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


def link(run: Runner, parent: str, child: str) -> None:
    r = run(["kanban", "link", parent, child])
    if r.returncode != 0:
        raise WorkflowError(f"kanban link {parent} {child} failed: {(r.stderr or r.stdout).strip()[:300]}")


def subscribe(run: Runner, task_id: str, origin: dict, mode: str) -> None:
    argv = ["kanban", "notify-subscribe", task_id, "--platform", origin.get("platform") or "discord",
            "--chat-id", origin["chat_id"], "--delivery-mode", mode]
    for flag, field in (("--thread-id", "thread_id"), ("--user-id", "user_id"), ("--chat-type", "chat_type")):
        if origin.get(field):
            argv += [flag, origin[field]]
    r = run(argv)
    if r.returncode != 0:
        raise WorkflowError(f"notify-subscribe {task_id} failed: {(r.stderr or r.stdout).strip()[:300]}")


def copy_subscriptions(run: Runner, source: str, targets: list) -> None:
    """New round cards inherit the thread of the card that opened them (status lines only)."""
    r = run(["kanban", "notify-list", source, "--json"])
    if r.returncode != 0:
        return
    try:
        subs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return
    for sub in subs if isinstance(subs, list) else []:
        if sub.get("chat_id"):
            for tid in targets:
                subscribe(run, tid, sub, "notify")


def short_title(title: str, limit: int = 80) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    return title if len(title) <= limit else title[: limit - 1] + "…"


def create_round(run: Runner, ctx: Ctx, n: int, request: str, parent: Optional[str], feedback: str) -> dict:
    """Code card -> one validation card per lane -> round card, idempotent per round."""
    code = create_card(run, f"[code {n}] {ctx.title}", ctx.coder, code_body(ctx, n, request, feedback),
                       f"{ctx.key}:code:{n}", parents=[parent] if parent else [], max_runtime=ctx.code_max_runtime)
    lanes = {}
    for lane in ctx.lanes:
        v = create_card(run, f"[{label(lane)} {n}] {ctx.title}", lane, validate_body(ctx, n, lane, code["id"], request),
                        f"{ctx.key}:{lane}:{n}", parents=[code["id"]], max_runtime=ctx.max_runtime)
        lanes[lane] = v["id"]
    rnd = create_card(run, f"[round {n}] {ctx.title}", ctx.orchestrator, round_body(ctx, n, code["id"], request),
                      f"{ctx.key}:round:{n}", parents=list(lanes.values()))
    return {"round": n, "code": code["id"], "lanes": lanes, "round_card": rnd["id"]}


def read_request(args) -> str:
    if args.body_file:
        try:
            return Path(args.body_file).expanduser().read_text(encoding="utf-8").strip("\n")
        except OSError as exc:
            raise WorkflowError(f"--body-file: {exc}") from exc
    return args.body or args.title


def intake(args, run: Runner = default_runner, git: Optional[GitRunner] = None) -> dict:
    lanes = list(dict.fromkeys(lane_profile(lane) for lane in args.lane))
    if not lanes:
        raise WorkflowError("at least one --lane is required (mac, windows, or a lane with --peer)")
    check_mobile(args.mobile, lanes)
    peers = peer_targets(lanes, args.peer)
    check_settings(args)
    repo = check_repo(args.repo)
    checkout = check_checkout(args.checkout, repo, git or default_git)
    origin = discord_origin(args)
    base_key = idempotency_base(args.key, origin)
    request = read_request(args)
    title = defang(short_title(args.title))
    branch = check_branch(args.branch or default_branch(title), args.base)
    ctx = Ctx(key=base_key, title=title, repo=repo, base=args.base, branch=branch, lanes=peers,
              coder=args.coder, reviewer=args.reviewer, orchestrator=args.orchestrator,
              max_rounds=args.max_rounds, max_runtime=args.max_runtime, code_max_runtime=args.code_max_runtime,
              mobile=args.mobile, checkout=checkout)

    plan = {
        "key": base_key, "repo": repo, "base": args.base, "branch": branch, "max_rounds": ctx.max_rounds,
        "code": {"assignee": ctx.coder, "title": f"[code 1] {title}", "idempotency_key": f"{base_key}:code:1"},
        "lanes": [{"assignee": lane, "title": f"[{label(lane)} 1] {title}", "transport": PEER_TRANSPORT,
                   "peer": peers[lane], "idempotency_key": f"{base_key}:{lane}:1"} for lane in lanes],
        "round": {"assignee": ctx.orchestrator, "title": f"[round 1] {title}",
                  "idempotency_key": f"{base_key}:round:1"},
        "request": {"assignee": ctx.orchestrator, "title": f"Report: {title}",
                    "idempotency_key": f"{base_key}:request"},
        "reviewer": ctx.reviewer,
        "discord": origin,
    }
    if checkout:
        plan["checkout"] = checkout
    if ctx.mobile:
        plan["mobile"] = ctx.mobile
        plan["stages"] = mobile_plan()
    if args.dry_run:
        plan["dry_run"] = True
        return plan

    check_profiles(run, ctx)
    first = create_round(run, ctx, 1, request, None, "")
    plan["code"]["id"] = first["code"]
    for spec in plan["lanes"]:
        spec["id"] = first["lanes"][spec["assignee"]]
    plan["round"]["id"] = first["round_card"]
    report = create_card(run, plan["request"]["title"], ctx.orchestrator, report_body(ctx, request),
                         plan["request"]["idempotency_key"], parents=[first["round_card"]])
    plan["request"]["id"] = report["id"]

    plan["subscribed"] = False
    if origin:
        # Work cards: passive status lines (SHA pushed, lane passed/failed, blocked).
        # Report card: notify + wake the orchestrator so it delivers the report.
        for tid in [first["code"], *first["lanes"].values(), first["round_card"]]:
            subscribe(run, tid, origin, "notify")
        subscribe(run, report["id"], origin, "notify+wake")
        plan["subscribed"] = True
    return plan


# -------------------------------------------------------------- code card ---

def git_out(git: GitRunner, argv: list, cwd: str) -> str:
    r = git(argv, cwd)
    if r.returncode != 0:
        raise WorkflowError(f"git {' '.join(argv[:2])} failed: {redact((r.stderr or r.stdout).strip())[:300]}")
    return (r.stdout or "").strip()


def code_sha(run: Runner, code_id: str) -> dict:
    rec = latest(card(run, code_id), "code-sha")
    if not rec or not SHA_RE.match(str(rec.get("sha") or "")):
        raise WorkflowError(f"code card {code_id} has no recorded SHA yet (the coordinator runs record-sha)")
    return rec


def checkout_preflight(task_id: str, h: dict, checkout: str, *, scan_runner=None, require_clean: bool = False,
                       git: GitRunner = default_git) -> dict:
    """repo_lease preflight for a code card's designated checkout, owned by this run."""
    try:
        owner = rl.cli_owner(task_id)  # refuses a lease owned by this short-lived process
        return rl.preflight(task_id, checkout, h.get("repo", ""), h.get("branch", ""), h.get("base", "main"),
                            git=git, scan_runner=scan_runner or rl.default_scan_runner, owner=owner,
                            require_clean=require_clean)
    except rl.LeaseError as exc:
        raise WorkflowError(str(exc)) from exc


def record_sha(task_id: str, repo_dir: str, notes: str, run: Runner = default_runner,
               git: GitRunner = default_git, scan_runner=None) -> dict:
    """Verify the checkout is committed and pushed, then record the exact SHA. In a designated
    checkout the lease, writer scan and clean-tree checks are re-validated right before the
    record is written."""
    require_worker_context("record-sha")
    own_card(task_id)
    show = card(run, task_id)
    h = kind_of(show, "code")
    branch, repo, n = h.get("branch", ""), h.get("repo", ""), int(h.get("round") or 1)
    cwd = str(Path(repo_dir).expanduser())
    checkout = h.get("checkout", "")
    if checkout and str(Path(cwd).resolve()) != checkout:
        raise WorkflowError(f"this card works in the designated checkout {checkout}, not '{repo_dir}': "
                            f"run record-sha with --repo-dir {checkout}")
    head = git_out(git, ["rev-parse", "HEAD"], cwd)
    current = git_out(git, ["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if current != branch:
        raise WorkflowError(f"checkout is on '{current}', the card's branch is '{branch}'")
    dirty = git_out(git, ["status", "--porcelain", "--untracked-files=all"], cwd)
    if dirty:
        raise WorkflowError("uncommitted or untracked paths; commit them (or move files that do not belong to this "
                            f"card out of the checkout) first: {clip(dirty, 300)}")
    if checkout:
        pre = checkout_preflight(task_id, h, checkout, scan_runner=scan_runner, require_clean=True)
        if not pre["ok"]:
            failed = "; ".join(f"{c['check']}: {c['detail']}" for c in pre["checks"] if not c["ok"])
            raise WorkflowError(f"the designated checkout is not safe to record from ({failed})")
    remote = git_out(git, ["ls-remote", repo, f"refs/heads/{branch}"], cwd).split()
    remote_sha = remote[0] if remote else ""
    if remote_sha != head:
        raise WorkflowError(f"push first: {branch} on {repo} is at {remote_sha[:12] or 'nothing'}, HEAD is {head[:12]}")
    rec = {"rid": f"code-sha:{head}", "sha": head, "branch": branch, "repo": repo, "base": h.get("base", ""),
           "round": n, "notes": clip(redact(notes or ""), 4000)}
    on_card = put_record(run, task_id, "code-sha", rec)
    return {"task": task_id, "summary": f"SHA_PUSHED {head[:12]} on {branch} (round {n})",
            "metadata": {"state": "SHA_PUSHED", "commit": head, "branch": branch, "round": n},
            "recorded_on_card": on_card}


def checkout_command(cmd: str, task_id: str, repo_dir: str, manifest: str = "", run: Runner = default_runner,
                     git: GitRunner = default_git, scan_runner=None) -> tuple:
    """preflight | checkpoint | adopt | release for a code card's designated checkout.
    Returns (exit code, result). The card header is the source of repo/branch/base/checkout."""
    require_worker_context(cmd)
    own_card(task_id)
    h = kind_of(card(run, task_id), "code")
    checkout = h.get("checkout", "")
    if not checkout:
        raise WorkflowError(f"{task_id} works in a fresh clone, not a designated checkout; {cmd} applies to --checkout cards")
    if str(Path(repo_dir).expanduser().resolve()) != checkout:
        raise WorkflowError(f"this card works in the designated checkout {checkout}, not '{repo_dir}'")
    try:
        if cmd == "preflight":
            result = checkout_preflight(task_id, h, checkout, scan_runner=scan_runner, git=git)
            return result["exit"], result
        identity = rl.repo_identity(checkout, git)
        if identity["branch"] != h.get("branch", ""):
            raise WorkflowError(f"checkout is on '{identity['branch']}', the card's branch is '{h.get('branch')}'")
        if cmd == "checkpoint":
            return 0, rl.checkpoint(task_id, identity, git, owner=rl.cli_owner(task_id),
                                    scan_runner=scan_runner or rl.default_scan_runner)
        if cmd == "adopt":
            return 0, rl.adopt_manifest(task_id, identity, git, manifest)
        result = rl.release_lease(identity, rl.owner_from_env(task_id))
        return (0 if result["status"] in ("released", "none") else 2), result
    except rl.LeaseError as exc:
        raise WorkflowError(str(exc)) from exc


# -------------------------------------------------------- validation lanes ---

def validate_lane(show: dict) -> tuple:
    """(lane, target, header) of a validation card, or raise."""
    task = show.get("task") or {}
    h = card_header(task.get("body") or "")
    if h.get("workflow") != "validate" or h.get("transport") != PEER_TRANSPORT:
        raise WorkflowError(f"{task.get('id')} is not a validation card (no 'Workflow: validate' header)")
    target, lane = h.get("peer", ""), task.get("assignee") or ""
    if not PEER_TARGET_RE.match(target) or not lane or not h.get("code"):
        raise WorkflowError(f"{task.get('id')}: validation card without a valid peer, assignee or code card")
    return lane, target, h


def mac_workspace(repo: str, branch: str, sha: str, wt: str) -> str:
    """Steps 1-2 of every Mac task: a detached worktree of the Mac's own clone under
    MAC_REPO_ROOT at the exact SHA; the clone's own checkout and files never move."""
    return f"""\
1. Find this machine's clone of the repository under {MAC_REPO_ROOT}: the git checkout whose
   origin is {repo} (https and ssh forms of the same repo match). Paths in the request below
   (/opt/repos/..., /home/...) are on the coordinator, not on this machine; never use them.
   No clone: report "blocked" with "blocker": "workspace: no clone of {repo} under {MAC_REPO_ROOT}".
2. Never change that clone's own checkout: validate in a detached worktree of it,
   {wt}
   git -C <clone> fetch origin {branch}
   git -C <clone> worktree add --detach {wt} {sha}
   When {wt} exists from an earlier attempt, reuse it instead:
   git -C {wt} status --porcelain --untracked-files=no   (any output: report "blocked" with
                         "blocker": "workspace: <dir> has uncommitted tracked changes")
   git -C {wt} checkout --detach {sha}
   Then: cd {wt}
         git rev-parse HEAD       (must print {sha} exactly; else report "blocked" with
                                   "blocker": "workspace: HEAD is <sha>, not {sha}")
   Run every later step in {wt}; it is the "repo_dir" you report."""


def mac_worktree(task_id: str) -> str:
    return f"{MAC_REPO_ROOT}/{MAC_WORKTREES}/{task_id}"


def validation_task(task_id: str, lane: str, h: dict, sha_rec: dict, request: str, attempt: int, note: str) -> str:
    """What the worker machine's own Hermes receives: test-only, exact SHA."""
    where = LANE_WHERE.get(lane, f"the '{lane}' worker")
    platform = LANE_PLATFORM.get(lane, "this worker's platform")
    sha, branch, repo = sha_rec["sha"], sha_rec.get("branch") or h.get("branch"), sha_rec.get("repo") or h.get("repo")
    work = f"~/hermes-work/{task_id}"
    lead = f"Attempt {attempt}. {note}\n\n" if note else ""
    notes = sha_rec.get("notes") or "(none)"
    if h.get("stage") == DEVICE_STAGE:
        return lead + device_task(task_id, where, repo, branch, sha, h, notes, request)
    if h.get("mobile"):
        return lead + mobile_task(task_id, where, repo, branch, sha, h.get("round"), notes, request)
    mac = lane == MOBILE_LANE
    fields = {
        "task": task_id, "status": "pass|fail|blocked", "sha_tested": sha, "platform": "...", "os": "...",
        "tools": "...", "commands": [{"cmd": "...", "exit_code": 0}],
        "tests": {"passed": 0, "failed": 0, "skipped": 0}, "failures": [], "artifacts": [],
        "device": "...", "dirty_tree": False, "blocker": "", "summary": "..."}
    if mac:
        fields = {**fields, "repo_dir": mac_worktree(task_id)}
    example = json.dumps(fields)
    if mac:
        checkout = mac_workspace(repo, branch, sha, mac_worktree(task_id)) + (
            f"\n   Keep build output outside the clone, under {work}.")
    else:
        checkout = f"""\
1. Work in {work} (create it fresh; never reuse another card's directory).
   git clone {repo} there (or fetch), then git checkout --detach {sha}.
   Confirm `git rev-parse HEAD` prints {sha}. Paths in the request below are on the
   coordinator, not on this machine; use only this repository URL."""
    steps = [
        "Build and test what this platform can check for the change. Keep each exact command, its\n"
        "   exit code, the test counts and the failure messages.",
        "Install and launch the app where it applies; name the simulator, emulator or device.",
        "Run `git status --porcelain --untracked-files=no` at the end; report changed tracked files.",
        "A failing build or test is a valid result: report \"fail\" with the failures. Do not fix them.",
        "At a login, MFA, signing, payment, admin/UAC or credential prompt, or with no usable device,\n"
        "   stop and report \"blocked\" with \"blocker\": \"human-approval: <what>\" or \"device: <what>\".",
        "End your final reply with the evidence block, exactly these marker lines around one JSON object:",
    ]
    first = 3 if mac else 2
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(steps, first))
    repo_dir = '; "repo_dir" is the worktree you used' if mac else ""
    return f"""\
{lead}You are {where}'s own Hermes agent. Kanban card {task_id} on the coordinator asks you to
validate one exact commit. You are a test-only worker: you never change source code. The
coordinator writes all code; it fixes what you report and sends you a new commit.

Repository: {repo}
Branch: {branch}
Commit: {sha}
Platform: {platform}
Round: {h.get("round")}

What changed, from the coordinator:
{defang(notes)}

Allowed: clone or fetch, check out the exact commit, install dependencies, build, run tests,
install and launch on a simulator, emulator or connected device, read logs, take screenshots.
Forbidden: editing or adding tracked files, committing, pushing, creating branches, merging,
deploying, releasing, store submission, signing with credentials, typing secrets, sending
Discord messages, and destructive commands outside {work}.

Steps:
{checkout}
{numbered}
BEGIN-EVIDENCE
{example}
END-EVIDENCE
   "status" is pass, fail or blocked; "sha_tested" is the full SHA you checked out{repo_dir}.
   Never put secrets in the reply.

Request context (from Discord, verbatim):
{request}
"""


def mobile_task(task_id: str, where: str, repo: str, branch: str, sha: str, rnd, notes: str, request: str) -> str:
    """The iOS/iPad lane: a detached worktree of the Mac's own clone under MAC_REPO_ROOT
    at the exact SHA (the clone's checkout is never moved), then an iPhone and an iPad
    simulator each build, test, install and launch."""
    work = f"~/hermes-work/{task_id}"
    wt = mac_worktree(task_id)
    phone, pad = (f"-destination 'platform=iOS Simulator,id=<{kind}-udid>'" for kind in SIMULATORS)
    sim = {"kind": "iphone|ipad", "name": "...", "os": "iOS ...", "udid": "...",
           "destination": "platform=iOS Simulator,id=<udid>", "build_exit": 0, "test_exit": 0,
           "install_exit": 0, "launch_exit": 0, "bundle_id": "...", "result_bundle": f"{work}/<kind>.xcresult",
           "tests": {"passed": 0, "failed": 0, "skipped": 0}, "screenshots": [f"{work}/<kind>.png"],
           "log_excerpt": "..."}
    example = json.dumps({
        "task": task_id, "status": "pass|fail|blocked", "sha_tested": sha, "repo_dir": wt,
        "platform": "iOS simulator", "os": "macOS ...", "tools": "Xcode ...",
        "commands": [{"cmd": "...", "exit_code": 0}], "tests": {"passed": 0, "failed": 0, "skipped": 0},
        "failures": [], "artifacts": [], "simulators": [sim], "device": "...", "dirty_tree": False,
        "blocker": "", "summary": "..."})
    return f"""\
You are {where}'s own Hermes agent. Kanban card {task_id} on the coordinator asks you to
validate one exact commit of an iOS/iPadOS app on an iPhone simulator AND an iPad simulator.
You are a test-only worker: you never change source code. The coordinator writes all code;
it fixes what you report and sends you a new commit.

Repository: {repo}
Branch: {branch}
Commit: {sha}
Platform: iOS/iPadOS simulators (iPhone and iPad), Xcode
Round: {rnd}

What changed, from the coordinator:
{defang(notes)}

Allowed: fetch, check out the exact commit detached, resolve packages, build, run tests,
install and launch on the simulators, read logs, take screenshots.
Forbidden: editing or adding tracked files, committing, pushing, creating branches, merging,
deploying, releasing, store submission, signing with credentials, typing secrets, sending
Discord messages, BrowserStack or any paid service, and destructive commands
(git reset --hard, git clean, git stash) in any checkout.

Steps (stop at the first blocker; keep each exact command and its exit code):
{mac_workspace(repo, branch, sha, wt)}
3. Pick one available iPhone and one available iPad simulator; boot both:
   xcrun simctl list devices available --json
   xcrun simctl boot <udid>                 (once per simulator; "already booted" is fine)
   xcrun simctl bootstatus <udid> -b
4. Build once for both, then test on each. Find <scheme> with `xcodebuild -list`; add
   -workspace <name>.xcworkspace or -project <name>.xcodeproj when the repo needs it. Keep
   build output outside the clone:
   xcodebuild build-for-testing -scheme <scheme> {phone} {pad} -derivedDataPath {work}/DerivedData
   xcodebuild test-without-building -scheme <scheme> {phone} -derivedDataPath {work}/DerivedData -resultBundlePath {work}/iphone.xcresult
   xcodebuild test-without-building -scheme <scheme> {pad} -derivedDataPath {work}/DerivedData -resultBundlePath {work}/ipad.xcresult
5. Install, launch and capture on each simulator (the .app is under
   {work}/DerivedData/Build/Products/<config>-iphonesimulator/; read <bundle-id> with
   /usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' <app>/Info.plist):
   xcrun simctl install <udid> <app>
   xcrun simctl launch <udid> <bundle-id>
   xcrun simctl io <udid> screenshot {work}/<kind>.png
   xcrun simctl spawn <udid> log show --last 2m --style compact --predicate 'process == "<executable>"'   (keep the last lines)
6. Run `git status --porcelain --untracked-files=no` in {wt} at the end; report changed
   tracked files as "dirty_tree".
7. A failing build, test, install or launch is a valid result: report "fail" with the failures
   and the exit codes. Do not fix them. "pass" needs both simulators with every exit code 0.
8. At a login, MFA, signing, payment or credential prompt, or with no usable iPhone or iPad
   simulator, stop and report "blocked" with "blocker": "human-approval: <what>" or "device: <what>".
9. End your final reply with the evidence block, exactly these marker lines around one JSON
   object, with one "simulators" entry with "kind": "iphone" and one with "kind": "ipad":
BEGIN-EVIDENCE
{example}
END-EVIDENCE
   "status" is pass, fail or blocked; "sha_tested" is the full 40-character SHA that
   `git rev-parse HEAD` printed; "repo_dir" is the clone you used. Never put secrets in the reply.

Request context (from Discord, verbatim):
{request}
"""


def device_task(task_id: str, where: str, repo: str, branch: str, sha: str, h: dict, notes: str,
                request: str) -> str:
    """The BrowserStack real-device stage on the Mac: the exact SHA in a worktree of the
    Mac's clone, a signed device build plus the XCUITest runner, one App Automate build
    tagged with the SHA on the card's devices. Credentials come from the Mac's own
    0600 activation file and are never echoed; the coordinator verifies the build id."""
    work = f"~/hermes-work/{task_id}"
    wt = mac_worktree(task_id)
    devices = bs.devices_of(h.get("devices", ""))
    auth = f'( set -a; . ~/.hermes/{bs.CONFIG_NAME}; set +a; curl -sS -u "${bs.USER_VAR}:${bs.KEY_VAR}"'
    build = json.dumps({"app": "<app_url>", "testSuite": "<test_suite_url>", "devices": devices,
                        "project": "hermes-kanban", "buildTag": sha, "deviceLogs": True})
    example = json.dumps({
        "task": task_id, "status": "pass|fail|blocked", "sha_tested": sha, "repo_dir": wt,
        "build_id": "<build_id from the /build response>", "app_url": "bs://...", "test_suite_url": "bs://...",
        "devices": devices, "commands": [{"cmd": "...", "exit_code": 0}], "failures": [],
        "dirty_tree": False, "blocker": "", "summary": "..."})
    return f"""\
You are {where}'s own Hermes agent. Kanban card {task_id} on the coordinator asks you to run
the real-device stage for one exact commit of an iOS/iPadOS app on BrowserStack App Automate
(XCUITest). The iPhone and iPad simulators already passed this commit. You are a test-only
worker: you never change source code; the coordinator fixes what fails.

Repository: {repo}
Branch: {branch}
Commit: {sha}
Real devices: {', '.join(devices)}
Round: {h.get("round")}

What changed, from the coordinator (the UI tests cover these functions):
{defang(notes)}

Allowed: fetch, check out the exact commit detached, build for a generic iOS device with the
signing team the operator already set up, package the app and the UI test runner, upload them
to BrowserStack and start, poll and read one build, using only the commands below.
Forbidden: editing or adding tracked files, committing, pushing, merging, releasing, store
submission, changing signing settings, creating certificates or profiles, typing or printing
secrets (never echo the credentials, never `set -x`, never paste the activation file), buying or
changing the BrowserStack plan, and destructive commands in any checkout.

Steps (stop at the first blocker; keep each exact command and its exit code, with the
credentials left as the variable names):
0. The BrowserStack credentials live only in ~/.hermes/{bs.CONFIG_NAME} on this Mac (mode 0600,
   written by the operator). Check without printing them:
   test "$(stat -f %Lp ~/.hermes/{bs.CONFIG_NAME})" = 600 && ( set -a; . ~/.hermes/{bs.CONFIG_NAME}; test -n "${bs.USER_VAR}" -a -n "${bs.KEY_VAR}" )
   Non-zero: report "blocked" with "blocker": "browserstack: credentials not provisioned on the Mac".
{mac_workspace(repo, branch, sha, wt)}
3. Build for real devices with the operator's signing setup (a signing, keychain or account
   prompt is a human gate: report "blocked" with "blocker": "human-approval: signing: <what>"):
   xcodebuild build-for-testing -scheme <scheme> -destination 'generic/platform=iOS' -derivedDataPath {work}/DerivedData
4. Package from {work}/DerivedData/Build/Products/<config>-iphoneos/:
   mkdir -p {work}/ipa/Payload && cp -R <App>.app {work}/ipa/Payload/ && (cd {work}/ipa && zip -qry {work}/app.ipa Payload)
   (cd <products dir> && zip -qry {work}/uitests.zip <App>UITests-Runner.app)
5. Upload both and start one build tagged with the exact SHA:
   {auth} -X POST "{bs.API}/app" -F "file=@{work}/app.ipa" )          -> "app_url"
   {auth} -X POST "{bs.API}/test-suite" -F "file=@{work}/uitests.zip" ) -> "test_suite_url"
   {auth} -X POST "{bs.API}/build" -H "Content-Type: application/json" -d '{build}' )   -> "build_id"
6. Poll until "status" is no longer queued or running (every 60 s, at most 90 minutes):
   {auth} "{bs.API}/builds/<build_id>" )
7. Run `git status --porcelain --untracked-files=no` in {wt}; report changed tracked files as "dirty_tree".
8. A failing build, packaging step, upload or real-device test is a valid result: report "fail"
   with the failures and exit codes. Do not fix them. A plan limit, quota, an expired trial or an
   HTTP 401/403 from BrowserStack is "blocked" with "blocker": "browserstack: <what>".
9. End your final reply with the evidence block, exactly these marker lines around one JSON
   object; "build_id" is the id BrowserStack returned (the coordinator re-reads that build from
   the BrowserStack API; a made-up or mismatched id fails the stage):
BEGIN-EVIDENCE
{example}
END-EVIDENCE
   Never put secrets in the reply.

Request context (from Discord, verbatim):
{request}
"""


def report_only_message(task_id: str, sha: str, mobile: str = "", stage: str = "") -> str:
    extra = ('\nInclude "repo_dir" and one "simulators" entry with "kind": "iphone" and one with "kind": "ipad"\n'
             '(name, os, udid, destination, build/test/install/launch exit codes, bundle_id, result_bundle,\n'
             'tests, screenshots, log_excerpt).') if mobile else ""
    if stage == DEVICE_STAGE:
        extra = ('\nInclude "repo_dir" and the BrowserStack "build_id" that the /build call returned (the\n'
                 'coordinator re-reads that build from the BrowserStack API). Never include credentials.')
    return f"""\
Kanban card {task_id}: your last run for commit {sha} ended without the evidence block.
Do not start the build or tests again. Reply now with only the evidence for the work you
already did, between these marker lines, with "task": "{task_id}" and "sha_tested":
BEGIN-EVIDENCE
{{"task": "{task_id}", "status": "pass|fail|blocked", "sha_tested": "{sha}", "commands": [], "tests": {{}}, "failures": [], "dirty_tree": false, "blocker": "", "summary": ""}}
END-EVIDENCE
When you did not finish, report "fail" with what ran, or "blocked" with the blocker.{extra}"""


def peer_runs(show: dict) -> list:
    return records(show, "peer-run")


def peer_status(run: Runner, lane: str, target: str, run_id: str) -> dict:
    return hermes_json(run, ["-p", lane, "peer", "status", target, run_id, "--json"])


def start_run(run: Runner, show: dict, lane: str, target: str, h: dict, sha_rec: dict,
              attempt: int, kind: str = "run", note: str = "", idem: str = "") -> dict:
    """Ledger a pending marker, send the peer run, record its id (ledger, then card).
    A crash between the two leaves the pending marker; the next dispatch re-sends the
    same idempotency key and the peer replays the same run."""
    task_id = show["task"]["id"]
    idem = idem or f"kanban:{task_id}:{attempt}" + (":report" if kind == "report" else "")
    if kind == "report":
        message = report_only_message(task_id, sha_rec["sha"], h.get("mobile", ""), h.get("stage", ""))
    else:
        message = validation_task(task_id, lane, h, sha_rec, request_of(show), attempt, redact(note))
    pending = {"rid": idem, "idempotency_key": idem, "attempt": attempt, "kind": kind, "lane": lane,
               "peer": target, "sha": sha_rec["sha"], "transport": PEER_TRANSPORT, "run_id": "",
               "events_seen": len(show.get("events") or [])}
    append_ledger(task_id, {**pending, "type": "peer-run", "ts": time.time()})
    started = hermes_json(run, ["-p", lane, "peer", "run", target, message, "--idempotency-key", idem, "--json"])
    if not started.get("run_id"):
        raise WorkflowError(f"peer '{target}' returned no run id")
    rec = {**pending, "run_id": str(started["run_id"]), "session_id": str(started.get("session_id") or "")}
    on_card = put_record(run, task_id, "peer-run", rec)
    return {**rec, "resumed": bool(started.get("replayed")), "recorded_on_card": on_card}


def unblocked_since(show: dict, rec: dict) -> bool:
    events = (show.get("events") or [])[int(rec.get("events_seen") or 0):]
    return any(e.get("kind") == "unblocked" for e in events)


def dispatch(task_id: str, run: Runner = default_runner) -> dict:
    """Start (or resume) the peer run for a validation card. A new attempt starts
    only for a first dispatch, or an operator unblock after the previous run ended;
    a pending (unrecorded) run is re-sent with its own idempotency key."""
    require_worker_context("dispatch")
    show = card(run, task_id)
    lane, target, h = validate_lane(show)
    problem = peer_problem(run, lane, target)
    if problem:
        raise WorkflowError(problem)
    sha_rec = code_sha(run, h["code"])
    sync_records(run, show)
    runs = peer_runs(show)
    if not runs:
        return start_run(run, show, lane, target, h, sha_rec, 1)
    last = runs[-1]
    if not last.get("run_id"):
        return start_run(run, show, lane, target, h, sha_rec, int(last.get("attempt") or 1),
                         kind=last.get("kind") or "run", idem=last["idempotency_key"])
    if not unblocked_since(show, last):
        return {**last, "resumed": True}
    remote = peer_status(run, lane, target, last["run_id"])
    if remote.get("status") in RUNNING | {"waiting_for_approval"}:
        return {**last, "resumed": True}  # the operator cleared a gate on a live run
    return start_run(run, show, lane, target, h, sha_rec, int(last.get("attempt") or 1) + 1,
                     note="The operator cleared the earlier block. Validate the same commit again from a clean checkout.")


def extract_report(output: str) -> Optional[dict]:
    """The evidence object from a remote reply, tolerant of formatting: the last
    BEGIN/END-EVIDENCE block, else the last fenced JSON block with a "status",
    else the last bare JSON object with a "status"."""
    text = output or ""

    def parse(chunk: str) -> Optional[dict]:
        chunk = chunk.strip()
        fenced = FENCE_RE.search(chunk)
        if fenced:
            chunk = fenced.group(1).strip()
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    for chunk in reversed(EVIDENCE_RE.findall(text)):
        obj = parse(chunk)
        if obj is not None:
            return obj
    for chunk in reversed(FENCE_RE.findall(text)):
        obj = parse(chunk)
        if obj is not None and "status" in obj:
            return obj
    decoder, found, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and "status" in obj:
            found = obj
        i = text.find("{", end)
    return found


class EvidenceError(ValueError):
    pass


def _int(value) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "no", "none", "clean", "0")
    return bool(value)


def _exit(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def mobile_evidence(raw: dict, status: str, sha: str, tested: str) -> tuple:
    """(status, extra fields, failures) for an iOS/iPad lane. Fails closed: a "pass"
    needs the exact full SHA, a clone under MAC_REPO_ROOT, and an iPhone and an iPad
    simulator that each built, tested, installed and launched with exit 0."""
    failures = []
    repo_dir = str(raw.get("repo_dir") or "").strip()
    if status != "blocked":
        if tested != sha:
            raise EvidenceError(f"mobile evidence must name the full SHA {sha}, not {tested[:40] or 'nothing'}")
        mac_repo_dir(raw)
    sims = {}
    for item in raw.get("simulators") or [] if isinstance(raw.get("simulators"), list) else []:
        kind = str((item or {}).get("kind") or "").strip().lower() if isinstance(item, dict) else ""
        if kind in SIMULATORS and kind not in sims:
            tests = item.get("tests") if isinstance(item.get("tests"), dict) else {}
            sims[kind] = {
                "kind": kind, **{k: clip(item.get(k), 300) for k in
                                 ("name", "os", "udid", "destination", "bundle_id", "result_bundle")},
                **{f"{step}_exit": _exit(item.get(f"{step}_exit")) for step in SIM_STEPS},
                "tests": {k: _int(tests.get(k)) for k in ("passed", "failed", "skipped")},
                "screenshots": [clip(x, 300) for x in item.get("screenshots") or [] if x][:10]
                if isinstance(item.get("screenshots"), list) else [],
                "log_excerpt": clip(item.get("log_excerpt"), 1500),
            }
    for kind in SIMULATORS:
        sim = sims.get(kind)
        if sim is None:
            if status == "pass":
                raise EvidenceError(f"no {kind} simulator evidence; a pass needs an iPhone and an iPad simulator")
            continue
        bad = [f"{step} exit {sim[f'{step}_exit']}" for step in SIM_STEPS if sim[f"{step}_exit"] not in (0, None)]
        if sim["tests"]["failed"]:
            bad.append(f"{sim['tests']['failed']} test(s) failed")
        if bad:
            failures.append(f"{kind} ({sim['name'] or 'unnamed'}, {sim['os'] or 'os n/a'}): {', '.join(bad)}")
        elif status == "pass":
            missing = [f"{step} exit code" for step in SIM_STEPS if sim[f"{step}_exit"] is None]
            missing += [k for k in ("name", "os", "udid", "bundle_id") if not sim[k]]
            missing += ["screenshot"] if not sim["screenshots"] else []
            if missing:
                raise EvidenceError(f"{kind} simulator evidence lacks {', '.join(missing)}")
    if failures and status == "pass":
        status = "fail"
    device = "; ".join(f"{s['name']} ({s['os']}, {s['udid']})" for s in sims.values())
    return status, {"repo_dir": clip(repo_dir, 300), "simulators": list(sims.values()), "device": device}, failures


def mac_repo_dir(raw: dict) -> str:
    """The worktree the Mac reports; it must be under MAC_REPO_ROOT (its own clone)."""
    repo_dir = str(raw.get("repo_dir") or "").strip()
    if not repo_dir.startswith(MAC_REPO_ROOT + "/") or ".." in repo_dir.split("/"):
        raise EvidenceError(f"validated in '{clip(repo_dir, 120) or 'no repo_dir'}', not a clone under {MAC_REPO_ROOT}")
    return repo_dir


def device_evidence(raw: dict, task_id: str, sha: str, lane: str, status: str, devices: list) -> dict:
    """The BrowserStack stage. Fails closed: a "pass" exists only when this host read the
    build from the BrowserStack API with its own credentials (bs.verify) and the build is
    tagged with the exact SHA and passed on a real iPhone and a real iPad. The Mac's own
    status can only make it worse (fail, blocked), never better."""
    base = {"task": task_id, "lane": lane, "sha": sha, "stage": DEVICE_STAGE, "platform": "BrowserStack real devices",
            "verified_by": "", "build_id": "", "build_status": "", "dashboard_url": "", "devices": [],
            "requested_devices": list(devices)}
    tested = str(raw.get("sha_tested") or "").strip().lower()
    commands = []
    for c in (raw.get("commands") or [])[:40] if isinstance(raw.get("commands"), list) else []:
        if isinstance(c, dict):
            code = c.get("exit_code")
            commands.append({"cmd": clip(c.get("cmd"), 300), "exit_code": code if isinstance(code, int) else None})
    failures = raw.get("failures") or []
    failures = [clip(f, 500) for f in (failures if isinstance(failures, list) else [failures]) if f][:30]
    out = {**base, "sha_tested": tested, "status": status, "commands": commands, "failures": failures,
           "tests": {"passed": 0, "failed": 0, "skipped": 0}, "artifacts": [], "device": "",
           "dirty_tree": False, "blocker": "", "summary": clip(raw.get("summary"), 1000), "repo_dir": ""}
    if status == "blocked":
        return {**out, "blocker": clip(raw.get("blocker"), 500) or "the worker blocked without a reason"}
    act = bs.activation()
    if act["state"] != "active":
        return {**out, "status": "blocked",
                "blocker": f"browserstack: the stage is {act['state']} on the coordinator ({act['reason']}); "
                           "the device result cannot be verified, thus it is not a pass"}
    if tested != sha:
        raise EvidenceError(f"device evidence must name the full SHA {sha}, not {tested[:40] or 'nothing'}")
    out["repo_dir"] = clip(mac_repo_dir(raw), 300)
    if _truthy(raw.get("dirty_tree")):
        out["dirty_tree"] = True
        out["failures"].append("tracked files changed on the worker during the device stage")
    build_id = str(raw.get("build_id") or "").strip()
    if not build_id:
        if status == "pass":
            raise EvidenceError("a real-device pass needs the BrowserStack build_id")
        out["status"] = "fail"
        out["failures"] = out["failures"] or ["the device stage failed before a BrowserStack build started"]
        return out
    try:
        api = bs.verify(build_id, sha, http=BROWSERSTACK_HTTP)
    except bs.VerifyError as exc:
        raise EvidenceError(f"BrowserStack build {clip(build_id, 64)}: {exc}") from None
    sessions = [x for d in api["devices"] for x in d["sessions"]]
    out.update({k: api[k] for k in ("build_id", "build_status", "dashboard_url", "verified_by", "devices")})
    out["tests"] = {"passed": sum(x["passed"] for x in sessions),
                    "failed": sum(x["failed"] + x["error"] + x["timedout"] for x in sessions),
                    "skipped": sum(x["skipped"] for x in sessions)}
    out["device"] = "; ".join(f"{d['device']} (iOS {d['os_version']})" for d in api["devices"])
    out["artifacts"] = [api["dashboard_url"]]
    out["failures"] += [f for f in api["failures"] if f not in out["failures"]]
    out["status"] = "pass" if api["status"] == "pass" and status == "pass" and not out["failures"] else "fail"
    if out["status"] == "fail" and not out["failures"]:
        out["failures"].append("the worker reported fail without details")
    return out


def normalize_evidence(raw: dict, task_id: str, sha: str, lane: str, mobile: str = "", stage: str = "",
                       devices: list = ()) -> dict:
    """Validate the worker's evidence against the card and the exact SHA, and
    bound every field. A dirty tree or failed tests turn a "pass" into "fail"."""
    task = str(raw.get("task") or task_id)
    if task != task_id:
        raise EvidenceError(f"evidence is for task {task}, not {task_id}")
    status = STATUS_ALIASES.get(str(raw.get("status") or "").strip().lower())
    if status is None:
        raise EvidenceError(f"evidence status {raw.get('status')!r} is not pass, fail or blocked")
    if stage == DEVICE_STAGE:
        return redact(device_evidence(raw, task_id, sha, lane, status, devices))
    tested = str(raw.get("sha_tested") or raw.get("commit") or raw.get("sha") or "").strip().lower()
    if status != "blocked":
        if not tested:
            raise EvidenceError("evidence has no sha_tested")
        if len(tested) < 7 or not sha.startswith(tested):
            raise EvidenceError(f"the worker tested {tested[:12]}, not {sha[:12]}")
    tests = raw.get("tests") if isinstance(raw.get("tests"), dict) else {}
    tests = {k: _int(tests.get(k)) for k in ("passed", "failed", "skipped")}
    failures = raw.get("failures") or []
    failures = [clip(f, 500) for f in (failures if isinstance(failures, list) else [failures]) if f][:30]
    commands = []
    for c in (raw.get("commands") or [])[:40] if isinstance(raw.get("commands"), list) else []:
        if isinstance(c, dict):
            code = c.get("exit_code")
            commands.append({"cmd": clip(c.get("cmd"), 300), "exit_code": code if isinstance(code, int) else None})
        elif c:
            commands.append({"cmd": clip(c, 300), "exit_code": None})
    extra = {}
    if mobile:
        status, extra, sim_failures = mobile_evidence(raw, status, sha, tested)
        failures += [f for f in sim_failures if f not in failures]
    elif lane == MOBILE_LANE and status != "blocked":
        extra = {"repo_dir": clip(mac_repo_dir(raw), 300)}  # the Mac validates in its own clone
    dirty = raw.get("dirty_tree")
    if status != "blocked" and _truthy(dirty):
        detail = f": {clip(dirty, 300)}" if isinstance(dirty, (str, list)) else ""
        failures.append(f"tracked files changed on the worker during validation{detail}")
        status = "fail"
    if status == "pass" and tests["failed"]:
        failures.append(f"{tests['failed']} test(s) failed")
        status = "fail"
    if status == "fail" and not failures:
        failures.append("the worker reported fail without details")
    blocker = clip(raw.get("blocker"), 500) if status == "blocked" else ""
    if status == "blocked" and not blocker:
        blocker = "the worker blocked without a reason"
    out = {
        "task": task_id, "lane": lane, "sha": sha, "sha_tested": tested, "status": status,
        "platform": clip(raw.get("platform"), 200), "os": clip(raw.get("os"), 200),
        "tools": clip(json.dumps(raw["tools"]) if isinstance(raw.get("tools"), (dict, list)) else raw.get("tools"), 500),
        "commands": commands, "tests": tests, "failures": failures,
        "artifacts": [clip(a, 300) for a in raw.get("artifacts") or [] if a][:30]
        if isinstance(raw.get("artifacts"), list) else [],
        "device": clip(raw.get("device"), 300), "dirty_tree": _truthy(dirty) if status != "blocked" else False,
        "blocker": blocker, "summary": clip(raw.get("summary"), 1000),
    }
    if mobile:
        out.update({**extra, "mobile": mobile, "device": extra["device"] or out["device"]})
    else:
        out.update(extra)
    return redact(out)


def lane_state(lane: str, verdict: str, stage: str = "") -> str:
    name = "DEVICES" if stage == DEVICE_STAGE else label(lane).upper()
    return f"{name}_{'PASSED' if verdict == 'pass' else 'FAILED'}"


def settle(task_id: str, lane: str, rec: dict, remote: dict, sha: str, mobile: str = "",
           stage: str = "", devices: list = ()) -> Optional[tuple]:
    """(exit code, result) for a remote run that needs the relay to act, else None.
    Code 1 results carry "retry": "report" when the worker only forgot the block."""
    state = remote.get("status")
    base = {"task": task_id, "peer": rec["peer"], "run_id": rec["run_id"], "attempt": rec.get("attempt"),
            "remote_status": state}
    if state == "waiting_for_approval":
        what = ((remote.get("approval") or {}).get("description") or "a command")[:200]
        return 4, {**base, "blocker": f"human-approval: the {rec['peer']} worker waits for approval of {what} "
                                      f"(card {task_id}, run {rec['run_id']}); approve or deny it there, then unblock"}
    if state in FAILED:
        return 1, {**base, "error": f"remote run {state}: {(remote.get('error') or '')[:300]}"}
    if state != "completed":
        return None
    raw = extract_report(remote.get("output") or "")
    if raw is None:
        return 1, {**base, "error": "remote run completed without the evidence block", "retry": "report"}
    try:
        ev = normalize_evidence(raw, task_id, sha, lane, mobile, stage, devices)
    except EvidenceError as exc:
        return 1, {**base, "error": f"unusable evidence: {exc}"}
    if ev["status"] == "blocked":
        return 4, {**base, "blocker": ev["blocker"], "evidence": ev}
    st = lane_state(lane, ev["status"], stage)
    t = ev["tests"]
    detail = (f"{t['passed']} passed, {t['failed']} failed, {t['skipped']} skipped" if ev["status"] == "pass"
              else clip("; ".join(ev["failures"]), 300))
    if ev.get("simulators"):
        detail += f" [{clip(ev['device'], 200)}]"
    meta = {"state": st, "verdict": ev["status"], "commit": sha, "lane": lane, "transport": PEER_TRANSPORT,
            "peer": rec["peer"], "run_id": rec["run_id"], "tests": t}
    if stage == DEVICE_STAGE:
        if ev["build_id"]:
            detail += f" [BrowserStack build {ev['build_id']}: {clip(ev['device'], 200)}; {ev['dashboard_url']}]"
        meta.update({"stage": DEVICE_STAGE, "build_id": ev["build_id"], "dashboard_url": ev["dashboard_url"],
                     "verified_by": ev["verified_by"]})
    return 0, {**base, "verdict": ev["status"], "summary": f"{st} at {sha[:12]}: {detail}",
               "metadata": meta, "evidence": ev}


def wait(task_id: str, timeout: float, interval: float, run: Runner = default_runner,
         sleep: Callable[[float], None] = time.sleep) -> tuple:
    """Poll the recorded peer run. Exit codes: 0 evidence recorded (pass or fail),
    4 blocked (human gate or device), 1 no usable run after the automatic retries,
    3 still running after `timeout` seconds."""
    require_worker_context("wait")
    show = card(run, task_id)
    lane, target, h = validate_lane(show)
    sha_rec = code_sha(run, h["code"])
    sha = sha_rec["sha"]
    sync_records(run, show)
    started = [r for r in peer_runs(show) if r.get("run_id")]
    if not started:
        raise WorkflowError(f"{task_id}: no peer run recorded; run `dispatch {task_id}` first")
    rec = started[-1]
    attempts = max(int(r.get("attempt") or 1) for r in started)
    limit = MAX_ATTEMPTS + sum(1 for e in show.get("events") or [] if e.get("kind") == "unblocked")
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        remote = peer_status(run, lane, target, rec["run_id"])
        settled = settle(task_id, lane, rec, remote, sha, h.get("mobile", ""), h.get("stage", ""),
                         bs.devices_of(h.get("devices", "")))
        if settled:
            code, result = settled
            if code == 0:
                ev = {**result["evidence"], "rid": f"evidence:{rec['run_id']}", "run_id": rec["run_id"],
                      "attempt": rec.get("attempt"), "peer": rec["peer"]}
                if not latest(show, "evidence", rid=ev["rid"]):
                    result["recorded_on_card"] = put_record(run, task_id, "evidence", ev)
                return 0, redact({k: v for k, v in result.items() if k != "evidence"})
            if code == 4:
                return 4, redact({k: v for k, v in result.items() if k != "evidence"})
            if result.get("retry") == "report" and rec.get("kind") != "report":
                rec = start_run(run, show, lane, target, h, sha_rec, int(rec.get("attempt") or 1), kind="report")
            elif attempts < limit:
                attempts += 1
                rec = start_run(run, show, lane, target, h, sha_rec, attempts,
                                note=f"The previous attempt ended without usable evidence ({result['error']}). "
                                     "Validate the same commit again from a clean checkout.")
            else:
                return 1, redact({k: v for k, v in result.items() if k != "retry"})
            show = card(run, task_id)
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            return 3, {"task": task_id, "peer": rec["peer"], "run_id": rec["run_id"],
                       "attempt": rec.get("attempt"), "remote_status": remote.get("status")}
        sleep(min(interval, left))


# --------------------------------------------------------- rounds, review ---

def children_of_kind(run: Runner, show: dict, kind: str) -> list:
    out = []
    for cid in show.get("children") or []:
        child = card(run, cid)
        if header_of(child).get("workflow") == kind:
            out.append(cid)
    return out


def report_card_for(run: Runner, show: dict) -> str:
    found = children_of_kind(run, show, "report")
    if not found:
        raise WorkflowError(f"{show['task']['id']}: no report card waits on this card")
    return found[0]


def round_evidence(run: Runner, round_show: dict, sha: str) -> list:
    """One row per validation card of a round: its latest evidence for `sha`."""
    rows = []
    for vid in round_show.get("parents") or []:
        v = card(run, vid)
        vh = header_of(v)
        if vh.get("workflow") != "validate":
            continue
        lane = v["task"].get("assignee") or vh.get("lane")
        ev = latest(v, "evidence", sha=sha)
        row = {"id": vid, "lane": lane, "status": v["task"].get("status"),
               "verdict": ev.get("status") if ev else None, "evidence": ev,
               "run_id": (ev or {}).get("run_id")}
        if row["status"] != "done":
            row["problem"] = f"{vid} ({lane}) is {row['status']}"
        elif not ev:
            row["problem"] = f"{vid} ({lane}) is done without evidence for {sha[:12]}"
        rows.append(row)
    return rows


def feedback_from(rows: list, sha: str) -> str:
    lines = [f"Commit {sha} failed validation:"]
    for row in rows:
        ev = row["evidence"] or {}
        if row["verdict"] == "pass":
            lines.append(f"- {row['lane']}: passed")
            continue
        lines.append(f"- {row['lane']} ({ev.get('platform') or 'platform n/a'}; {ev.get('device') or 'no device named'}):")
        for f in ev.get("failures") or []:
            lines.append(f"    * {f}")
        for c in ev.get("commands") or []:
            if c.get("exit_code") not in (0, None):
                lines.append(f"    $ {c['cmd']}  (exit {c['exit_code']})")
        for sim in ev.get("simulators") or []:
            lines.append(f"    {sim['kind']}: {sim.get('name') or '?'} {sim.get('os') or ''} {sim.get('udid') or ''}; "
                         + ", ".join(f"{step} exit {sim.get(f'{step}_exit')}" for step in SIM_STEPS)
                         + (f"; log: {clip(sim['log_excerpt'], 300)}" if sim.get("log_excerpt") else ""))
        if ev.get("build_id"):
            lines.append(f"    BrowserStack build {ev['build_id']} ({ev.get('build_status') or '?'}): "
                         f"{ev.get('dashboard_url')} (session videos, device logs)")
    return clip("\n".join(lines), 6000)


def device_stage(rows: list) -> dict:
    """The browserstack entry of a device round, from its verified evidence only."""
    ev = (rows[0].get("evidence") if rows else None) or {}
    verified = ev.get("verified_by") == bs.VERIFIED_BY and bool(ev.get("build_id"))
    status = "pass" if verified and all(r["verdict"] == "pass" for r in rows) else (
        "fail" if any(r["verdict"] == "fail" for r in rows) else "missing")
    out = {"stage": "browserstack", "status": status, "build_id": ev.get("build_id") or "",
           "dashboard_url": ev.get("dashboard_url") or "", "verified_by": ev.get("verified_by") or "",
           "devices": [f"{d['device']} (iOS {d['os_version']})" for d in ev.get("devices") or []]}
    if status == "pass":
        out["reason"] = (f"build {out['build_id']} on {', '.join(out['devices'])}, verified via the "
                         f"BrowserStack API: {out['dashboard_url']}")
    elif status == "fail":
        out["reason"] = clip("; ".join(ev.get("failures") or []), 300)
    return out


def mobile_stages(rows: list, sha: str, stage: str = "") -> list:
    """The staged outcome of a mobile round, in MOBILE_STAGES order, plus the
    deferred Windows lane. BrowserStack runs only after both simulators passed;
    a device round reports the simulators as passed (it opens only after they did)."""
    if stage == DEVICE_STAGE:
        return ([{"stage": "writer", "status": "pushed", "commit": sha}]
                + [{"stage": f"mac-{kind}", "status": "pass"} for kind in SIMULATORS]
                + [device_stage(rows), {"stage": "windows", "status": "deferred"}])
    sims = {}
    for row in rows:
        for sim in (row.get("evidence") or {}).get("simulators") or []:
            exits = [sim.get(f"{step}_exit") for step in SIM_STEPS]
            if sim["tests"]["failed"] or any(code not in (0, None) for code in exits):
                sims[sim["kind"]] = "fail"
            else:
                sims[sim["kind"]] = "pass" if None not in exits else "incomplete"
    out = [{"stage": "writer", "status": "pushed", "commit": sha}]
    out += [{"stage": f"mac-{kind}", "status": sims.get(kind, "missing")} for kind in SIMULATORS]
    if rows and all(r["verdict"] == "pass" for r in rows) and all(sims.get(k) == "pass" for k in SIMULATORS):
        out.append(browserstack_stage())
    else:
        out.append({"stage": "browserstack", "status": "skipped", "reason": "the local simulators did not both pass"})
    return out + [{"stage": "windows", "status": "deferred"}]


def open_next_round(run: Runner, show: dict, ctx: Ctx, n: int, feedback: str) -> dict:
    """Round n+1 after `show` (a round or review card), linked into the report card
    before `show` completes, thus the report never runs between rounds."""
    report = report_card_for(run, show)
    nxt = create_round(run, ctx, n + 1, request_of(show), show["task"]["id"], feedback)
    link(run, nxt["round_card"], report)
    copy_subscriptions(run, show["task"]["id"], [nxt["code"], *nxt["lanes"].values(), nxt["round_card"]])
    return nxt


def round_limit(ctx: Ctx, show: dict) -> int:
    """Max rounds, plus one per operator unblock of this card."""
    return ctx.max_rounds + sum(1 for e in show.get("events") or [] if e.get("kind") == "unblocked")


def open_device_stage(run: Runner, show: dict, ctx: Ctx, n: int, code_id: str, devices: list) -> dict:
    """[devices n] (the Mac runs the BrowserStack build) -> [devices-round n], linked into
    the report before the local round completes, thus the report never runs in between."""
    task_id, request = show["task"]["id"], request_of(show)
    report = report_card_for(run, show)
    dev = create_card(run, f"[devices {n}] {ctx.title}", MOBILE_LANE,
                      validate_body(ctx, n, MOBILE_LANE, code_id, request, devices),
                      f"{ctx.key}:devices:{n}", parents=[task_id], max_runtime=ctx.max_runtime)
    rnd = create_card(run, f"[devices-round {n}] {ctx.title}", ctx.orchestrator,
                      round_body(ctx, n, code_id, request, DEVICE_STAGE),
                      f"{ctx.key}:devices-round:{n}", parents=[dev["id"]])
    link(run, rnd["id"], report)
    copy_subscriptions(run, task_id, [dev["id"], rnd["id"]])
    return {"devices": dev["id"], "devices_round": rnd["id"]}


def advance(task_id: str, run: Runner = default_runner) -> tuple:
    """Close a round. Exit 0 with the next step opened (BrowserStack device stage,
    review or next fix round), 4 when a lane has no evidence, the BrowserStack
    activation is partial, or the fix-loop limit is reached."""
    require_worker_context("advance")
    show = card(run, task_id)
    h = kind_of(show, "round")
    ctx, n, stage = Ctx.from_header(h), int(h.get("round") or 1), h.get("stage", "")
    sync_records(run, show)
    sha = code_sha(run, h["code"])["sha"]
    rows = round_evidence(run, show, sha)
    problems = [r["problem"] for r in rows if r.get("problem")]
    if not rows:
        problems.append("round card has no validation cards")
    if problems:
        return 4, {"task": task_id, "blocker": f"round {n}: " + "; ".join(problems)}
    lanes = {r["lane"]: {"id": r["id"], "verdict": r["verdict"], "run_id": r["run_id"]} for r in rows}
    stages = mobile_stages(rows, sha, stage) if ctx.mobile else None
    if ctx.mobile and stage == DEVICE_STAGE and all(r["verdict"] == "pass" for r in rows) \
            and stages[3]["status"] != "pass":
        return 4, {"task": task_id, "blocker": f"browserstack: round {n} has no API-verified device result "
                                               f"for {sha[:12]}"}
    if all(r["verdict"] == "pass" for r in rows) and ctx.mobile and stage != DEVICE_STAGE \
            and stages[3]["status"] in ("queued", "blocked"):
        if stages[3]["status"] == "blocked":
            return 4, {"task": task_id, "blocker": f"browserstack: {stages[3]['reason']}. Complete the activation "
                                                   f"(docs/mobile-validation.md) or set {BROWSERSTACK_FLAG}=false "
                                                   "in it, then unblock this card"}
        nxt = open_device_stage(run, show, ctx, n, h["code"], stages[3]["devices"])
        state = "SIMULATORS_PASSED"
        summary = (f"SIMULATORS_PASSED {sha[:12]}: iPhone and iPad simulators passed; BrowserStack real devices "
                   f"queued ({', '.join(stages[3]['devices'])}): device card {nxt['devices']}")
    elif all(r["verdict"] == "pass" for r in rows):
        report = report_card_for(run, show)
        devices = stages[3]["reason"] if ctx.mobile and stage == DEVICE_STAGE else ""
        if devices:
            devices = f"BrowserStack real devices passed: {devices}"
        review = create_card(run, f"Review: {ctx.title} (round {n})", ctx.reviewer,
                             review_body(ctx, n, h["code"], task_id, sha, request_of(show), devices),
                             f"{ctx.key}:review:{n}", parents=[task_id])
        link(run, review["id"], report)
        copy_subscriptions(run, task_id, [review["id"]])
        state, nxt = "VALIDATED", {"review": review["id"]}
        summary = f"VALIDATED {sha[:12]}: every lane passed; review {review['id']}"
        if ctx.mobile:
            summary = (f"VALIDATED {sha[:12]}: iPhone and iPad simulators passed; "
                       f"browserstack {stage_text(stages[3])}; windows deferred; review {review['id']}")
    else:
        limit = round_limit(ctx, show)
        failing = ", ".join(f"{r['lane'] if stage != DEVICE_STAGE else 'browserstack'} ({r['id']})"
                            for r in rows if r["verdict"] != "pass")
        if n >= limit:
            return 4, {"task": task_id, "blocker": f"fix-loop: round {n} of {limit} failed at {sha[:12]} on {failing}; "
                                                   "unblock this card to allow one more round"}
        opened = open_next_round(run, show, ctx, n, feedback_from(rows, sha))
        state, nxt = "FIX_REQUIRED", opened
        summary = f"FIX_REQUIRED at {sha[:12]}: {failing} failed; round {n + 1} code card {opened['code']}"
    rec = {"rid": f"round:{n}:{sha}", "state": state, "round": n, "commit": sha, "lanes": lanes, "next": nxt}
    meta = {"state": state, "round": n, "commit": sha, "lanes": lanes, "next": nxt}
    if ctx.mobile:
        rec["stages"] = meta["stages"] = stages
    if stage:
        rec["stage"] = meta["stage"] = stage
    on_card = put_record(run, task_id, "round", rec)
    return 0, {"task": task_id, "state": state, "summary": summary, "recorded_on_card": on_card, "metadata": meta}


def verdict(task_id: str, decision: str, reason: str, run: Runner = default_runner) -> tuple:
    """The reviewer's verdict on a review card: approve, or changes (opens the next round)."""
    require_worker_context("verdict")
    show = card(run, task_id)
    h = kind_of(show, "review")
    ctx, n, sha = Ctx.from_header(h), int(h.get("round") or 1), h.get("commit", "")
    profile = os.environ.get("HERMES_PROFILE", "")
    if profile and profile in (ctx.coder, *ctx.lanes):
        raise WorkflowError(f"profile '{profile}' wrote or relayed this work; it cannot review it")
    if decision == "approve":
        rec = {"rid": f"verdict:{n}:{sha}", "verdict": "approve", "round": n, "commit": sha, "reviewer": profile}
        on_card = put_record(run, task_id, "verdict", rec)
        return 0, {"task": task_id, "summary": f"REVIEW_APPROVED {sha[:12]} on {ctx.branch}",
                   "recorded_on_card": on_card,
                   "metadata": {"state": "REVIEW_APPROVED", "verdict": "approve", "commit": sha, "round": n}}
    reason = (reason or "").strip()
    if not reason:
        raise WorkflowError("verdict changes needs --reason with a numbered, actionable list")
    limit = round_limit(ctx, show)
    if n >= limit:
        return 4, {"task": task_id, "blocker": f"fix-loop: review asked for changes after round {n} of {limit}; "
                                               f"unblock this card to allow one more round. Reason: {clip(reason, 300)}"}
    opened = open_next_round(run, show, ctx, n, f"The independent reviewer asked for changes to {sha}:\n{reason}")
    rec = {"rid": f"verdict:{n}:{sha}", "verdict": "changes", "round": n, "commit": sha, "reviewer": profile,
           "reason": clip(redact(reason), 2000), "next": opened}
    on_card = put_record(run, task_id, "verdict", rec)
    return 0, {"task": task_id, "summary": f"CHANGES_REQUESTED on {sha[:12]}: round {n + 1} code card {opened['code']}",
               "recorded_on_card": on_card,
               "metadata": {"state": "CHANGES_REQUESTED", "verdict": "changes", "commit": sha, "round": n,
                            "next": opened}}


# -------------------------------------------------------------------- gate ---

def review_verdict(show: dict, sha: str, ctx: Ctx) -> tuple:
    """(ok, reason) for the final review card from `hermes kanban show --json`."""
    task = show.get("task") or {}
    if task.get("status") != "done":
        return False, f"review is {task.get('status')}"
    if header_of(show).get("commit") != sha:
        return False, f"review covers {header_of(show).get('commit', '')[:12]}, not the final {sha[:12]}"
    rec = latest(show, "verdict")
    if not rec or rec.get("verdict") != "approve" or rec.get("commit") != sha:
        return False, f"no approve verdict for {sha[:12]} (verdict: {(rec or {}).get('verdict') or 'none'})"
    approvals = [r for r in show.get("runs") or [] if r.get("outcome") == "completed"]
    if not approvals:
        return False, "no completing run recorded"
    approver = approvals[-1].get("profile") or ""
    if approver in (ctx.coder, *ctx.lanes):
        return False, f"approved by {approver}, who wrote or relayed the work"
    return True, f"approved by {approver} at {sha[:12]}"


def gate_devices(run: Runner, rid: str, rshow: dict, device_round: Optional[tuple], lanes: list, sha: str) -> dict:
    """The gate row of the BrowserStack stage. "passed" only from evidence this host
    verified via the BrowserStack API for `sha`; a disabled stage is reported as not
    run (never passed); a stage that was queued or blocked must finish first."""
    if device_round:
        did, dshow = device_round
        rows = round_evidence(run, dshow, sha)
        problems = [r["problem"] for r in rows if r.get("problem")] or ([] if rows else ["no device card"])
        st = device_stage(rows) if not problems else {"status": "missing", "reason": "; ".join(problems)}
        if st["status"] == "pass":
            return {"id": did, "ok": True, "reason": f"browserstack passed {sha[:12]} on real devices: "
                                                     f"{st['reason']}; windows deferred"}
        return {"id": did, "ok": False, "reason": f"browserstack {st['status']} at {sha[:12]}: "
                                                  f"{st.get('reason') or 'no verified BrowserStack build'}"}
    stages = (latest(rshow, "round") or {}).get("stages") or mobile_stages(lanes, sha)
    st = next((x for x in stages if x.get("stage") == "browserstack"), {"status": "missing"})
    if st.get("status") == "skipped":
        return {"id": rid, "ok": True, "reason": f"browserstack {stage_text(st)} (not run: nothing was tested on "
                                                 "real devices, never counted as passed); windows deferred"}
    return {"id": rid, "ok": False, "reason": f"browserstack {stage_text(st)}: the real-device stage of {sha[:12]} "
                                              "did not finish"}


def gate(task_id: str, run: Runner = default_runner) -> tuple:
    """Exit 0 iff every card the report waits on is done, the last round's SHA
    passed every lane with recorded evidence, and an independent reviewer
    approved exactly that SHA."""
    report = card(run, task_id)
    parents = report.get("parents") or []
    if not parents:
        return False, [{"id": task_id, "ok": False, "reason": "report card has no round cards"}]
    shows = {pid: card(run, pid) for pid in parents}
    rows = [{"id": pid, "ok": False, "reason": f"status is {s['task'].get('status')}"}
            for pid, s in shows.items() if s["task"].get("status") != "done"]
    rounds = {int(header_of(s).get("round") or 0): (pid, s) for pid, s in shows.items()
              if header_of(s).get("workflow") == "round" and not header_of(s).get("stage")}
    device_rounds = {int(header_of(s).get("round") or 0): (pid, s) for pid, s in shows.items()
                     if header_of(s).get("workflow") == "round" and header_of(s).get("stage") == DEVICE_STAGE}
    reviews = {int(header_of(s).get("round") or 0): (pid, s) for pid, s in shows.items()
               if header_of(s).get("workflow") == "review"}
    if not rounds:
        return False, rows + [{"id": task_id, "ok": False, "reason": "no round cards"}]
    n = max(rounds)
    rid, rshow = rounds[n]
    ctx = Ctx.from_header(header_of(rshow))
    try:
        sha = code_sha(run, header_of(rshow)["code"])["sha"]
    except WorkflowError as exc:
        return False, rows + [{"id": rid, "ok": False, "reason": str(exc)}]
    lanes = round_evidence(run, rshow, sha)
    if not lanes:
        rows.append({"id": rid, "ok": False, "reason": "final round has no validation cards"})
    for row in lanes:
        ok = not row.get("problem") and row["verdict"] == "pass"
        reason = row.get("problem") or (f"{row['lane']} passed {sha[:12]} (run {row['run_id']})" if ok
                                        else f"{row['lane']} {row['verdict']} at {sha[:12]}")
        rows.append({"id": row["id"], "ok": ok, "reason": reason})
    if ctx.mobile:
        rows.append(gate_devices(run, rid, rshow, device_rounds.get(n), lanes, sha))
    if n not in reviews or max(reviews) != n:
        rows.append({"id": rid, "ok": False, "reason": f"round {n} has no review card"})
    else:
        vid, vshow = reviews[n]
        ok, reason = review_verdict(vshow, sha, ctx)
        rows.append({"id": vid, "ok": ok, "reason": reason})
    return all(r["ok"] for r in rows), rows


# ------------------------------------------------------------------ status ---

def card_state(show: dict) -> str:
    t, h = show.get("task") or {}, header_of(show)
    kind, status = h.get("workflow"), t.get("status")
    if kind == "code":
        if status == "done" and latest(show, "code-sha"):
            return "SHA_PUSHED"
        return {"running": "CODING", "blocked": "CODING_BLOCKED"}.get(status, "CODING_QUEUED")
    if kind == "validate":
        lab = "DEVICES" if h.get("stage") == DEVICE_STAGE else label(t.get("assignee") or h.get("lane", "")).upper()
        ev = latest(show, "evidence")
        if ev and status == "done":
            return lane_state(t.get("assignee") or h.get("lane", ""), ev.get("status"), h.get("stage", ""))
        return {"running": f"{lab}_VALIDATING", "blocked": f"{lab}_BLOCKED", "todo": f"{lab}_WAITING_FOR_SHA",
                "done": f"{lab}_DONE_WITHOUT_EVIDENCE"}.get(status, f"{lab}_QUEUED")
    if kind == "round":
        rec = latest(show, "round")
        if status == "done" and rec:
            return rec.get("state") or "ROUND_DONE"
        return "FIX_LOOP_BLOCKED" if status == "blocked" else "WAITING_FOR_EVIDENCE"
    if kind == "review":
        rec = latest(show, "verdict")
        if rec:
            return "REVIEW_APPROVED" if rec.get("verdict") == "approve" else "CHANGES_REQUESTED"
        return {"running": "IN_REVIEW", "blocked": "REVIEW_BLOCKED"}.get(status, "WAITING_FOR_REVIEW")
    if kind == "report":
        return {"done": "COMPLETE", "blocked": "GATE_BLOCKED", "running": "REPORTING"}.get(status, "WAITING")
    return str(status or "").upper()


def status(task_id: str, run: Runner = default_runner) -> list:
    """Every card of the request that `task_id` belongs to, ordered by round, with
    its state; read from the cards and the ledger (no network call)."""
    start = card(run, task_id)
    key = header_of(start).get("key")
    seen, queue, shows = set(), [task_id], {}
    while queue:
        tid = queue.pop()
        if tid in seen:
            continue
        seen.add(tid)
        show = start if tid == task_id else card(run, tid)
        if key and header_of(show).get("key") != key:
            continue
        shows[tid] = show
        queue += [*(show.get("parents") or []), *(show.get("children") or [])]
    order = {k: i for i, k in enumerate(KINDS)}
    rows = []
    for tid, show in shows.items():
        t, h = show["task"], header_of(show)
        row = {"id": tid, "kind": h.get("workflow") or "", "round": int(h.get("round") or 0) or None,
               "status": t.get("status"), "state": card_state(show), "assignee": t.get("assignee"),
               "title": t.get("title")}
        if h.get("stage"):
            row["stage"] = h["stage"]
        if row["kind"] == "code":
            rec = latest(show, "code-sha")
            row.update({"commit": (rec or {}).get("sha"), "branch": h.get("branch")})
        elif row["kind"] == "validate":
            runs = [r for r in peer_runs(show) if r.get("run_id")]
            ev = latest(show, "evidence")
            row.update({"transport": PEER_TRANSPORT, "peer": h.get("peer"),
                        "run_id": runs[-1]["run_id"] if runs else None, "attempts": len(runs),
                        "evidence": ev})
        elif row["kind"] in ("round", "review"):
            rec = latest(show, "round" if row["kind"] == "round" else "verdict")
            row.update({"commit": h.get("commit") or (rec or {}).get("commit"), "record": rec})
        rows.append(row)
    rows.sort(key=lambda r: (10**6 if r["kind"] == "report" else (r["round"] or 0), bool(r.get("stage")),
                             order.get(r["kind"], 99), r["id"]))
    return rows


# --------------------------------------------------------------------- CLI ---

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("intake", help="create round 1 (code, validation, round cards), the report card and the "
                                      "Discord subscriptions")
    i.add_argument("--title", required=True)
    i.add_argument("--body", default="", help="the operator's request, verbatim (default: the title)")
    i.add_argument("--body-file", default="", help="read the request from a file (a long CSV-derived task set)")
    i.add_argument("--lane", action="append", default=[],
                   help="validation lane: mac, windows, or a lane profile with --peer (repeatable)")
    i.add_argument("--repo", default="", help="clone URL every card checks out (never a local path)")
    i.add_argument("--checkout", default="",
                   help="the operator's existing checkout on this host (absolute path, origin == --repo) the "
                        "code cards work in instead of a fresh clone; peers still use --repo")
    i.add_argument("--base", default="main", help="base branch the feature branch starts from")
    i.add_argument("--branch", default="", help="feature branch the coordinator writes (default: feat/<title>)")
    i.add_argument("--coder", default=os.environ.get("HERMES_WORKFLOW_CODER", "default"),
                   help="coordinator profile that writes all code (Claude Opus/Fable through delegate_task)")
    i.add_argument("--reviewer", default=os.environ.get("HERMES_WORKFLOW_REVIEWER", "reviewer"))
    i.add_argument("--orchestrator", default=os.environ.get("HERMES_WORKFLOW_ORCHESTRATOR", "default"))
    i.add_argument("--max-rounds", type=int, default=5, help="fix/retest rounds before the loop blocks for you")
    i.add_argument("--key", default="", help="idempotency key (default: discord:<chat>:<message>)")
    i.add_argument("--max-runtime", default="2h", help="per-run cap for validation cards ('' for none)")
    i.add_argument("--code-max-runtime", default="6h", help="per-run cap for code cards ('' for none)")
    i.add_argument("--peer", action="append", default=[], metavar="LANE=PEER[/PROFILE]",
                   help="peer target for a lane (default: mac-worker=mac, windows-worker=windows)")
    i.add_argument("--mobile", default="", choices=["", *MOBILE_TARGETS],
                   help="iOS/iPad-first mobile validation on the Mac lane only: iPhone and iPad simulators, "
                        "then BrowserStack real devices when activated (disabled by default); Windows deferred "
                        "(docs/mobile-validation.md)")
    i.add_argument("--dry-run", action="store_true", help="print the plan, create nothing")
    for name in ("chat-id", "thread-id", "message-id", "user-id", "chat-type"):
        i.add_argument(f"--discord-{name}", default="")

    r = sub.add_parser("record-sha", help="code card: verify the push and record the exact SHA")
    r.add_argument("task_id")
    r.add_argument("--repo-dir", default=".", help="the card's checkout")
    r.add_argument("--notes", default="", help="what changed and what each platform should check")
    for name, text in (("preflight", "code card (--checkout): lease, competing-writer scan, branch and dirty-tree "
                                     "provenance checks before editing; exit 0 ok, 2 conflict, 3 unknown"),
                       ("checkpoint", "code card (--checkout): record this card's own uncommitted files for resume"),
                       ("adopt", "code card (--checkout): bootstrap the manifest from an authorized {path: sha256} file"),
                       ("release", "code card (--checkout): release this run's lease on the checkout")):
        c = sub.add_parser(name, help=text)
        c.add_argument("task_id")
        c.add_argument("--repo-dir", required=True, help="the designated checkout")
        if name == "adopt":
            c.add_argument("--manifest", required=True)

    d = sub.add_parser("dispatch", help="validation card: start or resume the peer run for the exact SHA")
    d.add_argument("task_id")
    w = sub.add_parser("wait", help="validation card: poll; exit 0 evidence, 4 blocked, 1 failed, 3 running")
    w.add_argument("task_id")
    w.add_argument("--timeout", type=float, default=540, help="seconds to keep polling (0: poll once)")
    w.add_argument("--interval", type=float, default=30, help="seconds between polls")

    a = sub.add_parser("advance", help="round card: open the review, or the next fix round")
    a.add_argument("task_id")
    v = sub.add_parser("verdict", help="review card: approve, or request changes (opens the next round)")
    v.add_argument("task_id")
    v.add_argument("decision", choices=["approve", "changes"])
    v.add_argument("--reason", default="")

    for name, text in (("gate", "exit 0 iff the final SHA passed every lane and an independent review"),
                       ("status", "show every card of the request and its state")):
        s = sub.add_parser(name, help=text)
        s.add_argument("task_id")
        s.add_argument("--json", action="store_true")
    return p


def emit(result) -> None:
    print(json.dumps(result, indent=2))


def main(argv=None, run: Runner = default_runner, git: GitRunner = default_git) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "intake":
            emit(intake(args, run, git))
            return 0
        if args.cmd == "record-sha":
            emit(record_sha(args.task_id, args.repo_dir, args.notes, run, git))
            return 0
        if args.cmd in ("preflight", "checkpoint", "adopt", "release"):
            code, result = checkout_command(args.cmd, args.task_id, args.repo_dir, getattr(args, "manifest", ""), run, git)
            emit(result)
            return code
        if args.cmd == "dispatch":
            emit(dispatch(args.task_id, run))
            return 0
        if args.cmd in ("wait", "advance", "verdict"):
            if args.cmd == "wait":
                code, result = wait(args.task_id, args.timeout, args.interval, run)
            elif args.cmd == "advance":
                code, result = advance(args.task_id, run)
            else:
                code, result = verdict(args.task_id, args.decision, args.reason, run)
            emit(result)
            return code
        if args.cmd == "gate":
            ok, rows = gate(args.task_id, run)
            if args.json:
                emit({"ok": ok, "checks": rows})
            else:
                for r in rows:
                    print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['id']}  {r['reason']}")
                print("review gate: PASS" if ok else "review gate: FAIL")
            return 0 if ok else 1
        rows = status(args.task_id, run)
        if args.json:
            emit(rows)
        else:
            for r in rows:
                extra = f"  {r['commit'][:12]}" if r.get("commit") else ""
                if r.get("transport"):
                    extra += f"  [peer {r['peer']} run {r['run_id'] or '-'}]"
                print(f"{r['id']}  {r['status']:<8} {r['state']:<22} {r['assignee'] or '-':<16} {r['title']}{extra}")
        return 0
    except WorkflowError as exc:
        print(f"kanban-workflow: {redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
