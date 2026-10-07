"""Deterministic classification of the CURRENT user task.

Routing decisions are made from the newest *genuine* user turn only — never
from the whole conversation corpus, system prompts, assistant output, or tool
results. That distinction is what keeps a short status question cheap even when
the session history carries megabytes of transcripts, and what stops control
tags quoted inside pasted logs from forcing the most expensive tier.

Rules implemented here (all deterministic, no network, no LLM calls):

  * The current task is the last ``role == "user"`` message (Chat Completions
    ``messages``) or user input item (Responses ``input``). Tool results,
    function-call outputs, assistant/reasoning items and system/instructions
    text are never part of it.
  * The Hermes gateway ships that user message as ONE flat string with runtime
    material around the sender's text (verified read-only against the installed
    core, see ``genuine_user_text``): reply/trigger metadata, media notes and
    channel backfill are PREPENDED (backfill ends with a literal
    ``[New message]`` marker, then an inline ``[sender] `` prefix), while
    recalled memory (``<memory-context>…</memory-context>``) and
    ``--- Attached Context ---`` blocks are APPENDED. Classification and
    authorization operate on the sender-authored segment only, so recalled
    memories, other users' channel messages, quoted replies and attached files
    can never look like the current task or grant authority.
  * Explicit max/Astra authorization is a LEADING STANDALONE control line —
    the first non-empty line of the current task being exactly ``#max`` or
    ``#astra``. Inline ``#max`` / ``#deep`` / ``#hard`` only *hint* at a hard
    task (Sol at most); they never grant Astra.
  * Tags inside fenced code blocks or ``>``-quoted lines are inert.
  * Classification never returns the task text — callers get a tier, an
    effort, stable reason labels, and an opaque hash for correlation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Control vocabulary
# --------------------------------------------------------------------------- #
CONTROL_MAX = ("#max", "#astra")
CONTROL_MIN = ("#quick", "#routine", "#cheap", "#simple", "#low")
# Leading standalone coding-force controls: the first non-empty line of the
# current task being exactly ``#opus`` or ``#fable`` forces that Claude coding
# model regardless of task shape. Like ``#max`` these are LEADING STANDALONE
# only — an inline or quoted ``#opus`` is inert (not even a hint) — and they
# take precedence over ``#max``/``#astra`` and the routine controls. The router
# can only honor them on a coding-family seam (claude-max-proxy); see router.py.
CONTROL_CODING = {"#opus": "opus", "#fable": "fable"}

_FORCE_MIN_RE = re.compile(r"(?<![A-Za-z0-9_])#(?:quick|routine|cheap|simple|low)\b", re.IGNORECASE)
# Inline hard-intent hints. Deliberately includes #max: away from the leading
# control line it is a hint, not an authorization.
_HARD_HINT_RE = re.compile(r"(?<![A-Za-z0-9_])#(?:max|astra|deep|hard)\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Hermes wire-format markers (formats verified against the installed core:
# gateway/run_inbound.py, agent/turn_context.py, agent/memory_manager.py,
# agent/prompt_builder.py, agent/context_references.py).
# ---------------------------------------------------------------------------
# Appended runtime context. Cutting at the FIRST occurrence is the safe
# direction: a sender who fakes one of these lines only truncates their own
# text (loses signals/authority); recalled memory that embeds a fake
# ``[New message]`` marker is discarded before the marker search runs.
# ``<specialist-routing>`` is this plugin's own pre_llm_call note (appended to
# the current user message by Hermes); cutting it keeps the router from
# classifying its own suggestion on the same turn's API calls.
_APPENDED_CUT_RE = re.compile(
    r"(?m)^(?:<memory-context>|<specialist-routing>|--- Attached Context ---|--- Context Warnings ---)\s*$"
)
# Channel backfill terminator: f"{context}\n\n[New message]\n{sender_text}".
# The gateway's own marker is the LAST one in the (already appendix-free)
# string — anything a prior channel message or reply quote embeds sits earlier.
NEW_MESSAGE_MARKER = "\n[New message]\n"
# Prepended single-paragraph runtime blocks on the DM path (no backfill).
# Deliberately narrow: only templates the gateway itself emits. Arbitrary
# leading bracket lines a user pastes are NOT stripped, so pasted transcripts
# cannot maneuver a #max line into leading position.
_LEADING_RUNTIME_BLOCK_RE = re.compile(
    r"^(?:\[Triggering message id: `[^`\n]*`[^\n]*\]"
    r"|\[Replying to[^\n]*\]"
    r"|\[The user sent [^\n]*\]"
    r"|\[voice message [^\n]*\])\s*(?:\n|$)"
)
# Inline sender prefix added in shared multi-user sessions: f"[{name}] {text}".
_SENDER_PREFIX_RE = re.compile(r"^\[[^\[\]\n]{1,96}\] ")
# Mid-turn steer delivery (a genuine direct user message).
_STEER_OPEN_PREFIX = "[OUT-OF-BAND USER MESSAGE"
_STEER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
# Gateway placeholder for a message with no text content (runtime text, not a task).
_EMPTY_PLACEHOLDER = "(The user sent a message with no text content)"
# Async delegate_task completion re-injected as a user-role message
# (tools/process_registry_notifications.py): "[ASYNC DELEGATION COMPLETE — id]",
# "... BATCH COMPLETE — id]", "... TASK FAILED — id, task i/n]". Only the
# header at the very start of the sender segment counts; prose that mentions it
# elsewhere is still a task.
_ASYNC_DELEGATION_RE = re.compile(r"^\[ASYNC DELEGATION [A-Z][A-Z ]* [\u2014\u2013-] [^\]\n]+\]")


def is_runtime_envelope(task_text: str) -> bool:
    """True when the sender segment is a Hermes runtime notification, not a task."""
    return bool(_ASYNC_DELEGATION_RE.match(task_text))


def runtime_marker(text: str) -> str:
    """Label of the Hermes wire/runtime text in ``text`` ('' when none).

    For text that must be pure content (a verified attachment routed as the
    task): any template this module strips or refuses counts, wherever the
    cut would apply, so routing never runs on gateway or runtime envelopes."""
    t = text.strip()
    if _ASYNC_DELEGATION_RE.match(t):
        return "runtime-envelope"
    if t.startswith(_STEER_OPEN_PREFIX) or _STEER_CLOSE in t:
        return "steer"
    if _LEADING_RUNTIME_BLOCK_RE.match(t):
        return "runtime-block"
    if _APPENDED_CUT_RE.search(t):
        return "appended-context"
    if NEW_MESSAGE_MARKER.strip() in t:
        return "backfill-marker"
    return ""


def split_sender_segment(text: str) -> Tuple[str, str]:
    """``(head, segment)``: the sender-authored segment and the runtime text before it.

    ``segment`` is :func:`genuine_user_text`; ``head`` is what was stripped in
    front of it (backfill through the gateway's ``[New message]`` marker, or the
    leading runtime blocks), which is where the gateway's per-attachment
    document notes live (``attachments.separate_sender_prose``).
    """
    t = text
    stripped = t.strip()
    if stripped.startswith(_STEER_OPEN_PREFIX):
        lines = stripped.splitlines()
        body = "\n".join(lines[1:])
        close = body.rfind(_STEER_CLOSE)
        if close != -1:
            t = body[:close]
    m = _APPENDED_CUT_RE.search(t)
    if m:
        t = t[: m.start()]
    idx = t.rfind(NEW_MESSAGE_MARKER)
    if idx != -1:
        start = idx + len(NEW_MESSAGE_MARKER)
    else:
        start = len(t) - len(t.lstrip())
        while True:
            blk = _LEADING_RUNTIME_BLOCK_RE.match(t[start:])
            if not blk:
                break
            rest = t[start + blk.end():]
            start = len(t) - len(rest.lstrip())
    seg = t[start:]
    start += len(seg) - len(seg.lstrip())
    seg = seg.strip()
    prefix = _SENDER_PREFIX_RE.match(seg)
    if prefix:
        rest = seg[prefix.end():]
        start += prefix.end() + len(rest) - len(rest.lstrip())
        seg = rest.strip()
    if seg == _EMPTY_PLACEHOLDER:
        seg = ""
    return t[:start], seg


def genuine_user_text(text: str) -> str:
    """Reduce a wire-format user message to its sender-authored segment.

    Strips, in provenance-safe order: appended memory/attached context (first
    marker wins), everything up to the gateway's ``[New message]`` marker
    (last marker wins — see comments above for why the directions differ), the
    known prepended runtime metadata blocks on the marker-less DM path, and a
    single inline ``[sender] `` prefix. Plain text without any runtime markers
    passes through unchanged. Returns '' when nothing sender-authored remains.
    An inline attachment relay stays in the segment; callers that must not
    score it use ``attachments.separate_sender_prose``.
    """
    return split_sender_segment(text)[1]


# Responses API item types that are never a user task.
_SKIP_ITEM_TYPES = {
    "function_call", "function_call_output", "tool_result", "item_reference",
    "reasoning", "web_search_call", "computer_call", "computer_call_output",
    "file_search_call",
}
_TEXT_PART_TYPES = {"text", "input_text"}

_CONT_RE = re.compile(
    r"(?i)^(?:please\s+)?(?:continue|go on|go ahead|proceed|resume|keep going|"
    r"carry on|next(?: step)?|yes(?: please)?|yep|yeah|ok(?:ay)?|do it|"
    r"sounds good|approved?|lgtm|ship it)[.! ]*$"
)
_GREETING_RE = re.compile(
    r"(?i)^(?:hi|hello|hey|yo|thanks|thank you|good (?:morning|afternoon|evening))\b"
)
_STATUS_START_RE = re.compile(
    r"(?i)^(?:what|what's|whats|which|who|whose|when|where|how (?:many|much|long|old)|"
    r"is|are|was|were|did|does|do|has|have|had|status|any (?:update|news)|"
    r"show|list|report|summar(?:y|ise|ize)|check)\b"
)
_PLANNING_RE = re.compile(
    r"(?i)\b(?:plan|planning|roadmap|strategy|design doc|proposal|propose|options|"
    r"trade[- ]?offs?|compare|evaluat(?:e|ion)|prioriti[sz]e|spec(?:ification)?)\b"
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_CONSTRAINT_RE = re.compile(r"(?i)\b(?:must|should|require[sd]?|constraint)\b")

# Hard-work categories. Sol requires SUBSTANTIVE complexity: two distinct
# categories, or one category plus another signal — a single keyword is not
# enough (that routes to the balanced Terra tier instead).
HARD_CATEGORIES: Tuple[Tuple[str, re.Pattern], ...] = (
    ("arch", re.compile(r"(?i)\b(?:architect(?:ure|ural)?|redesign|rewrite|system design|end[- ]to[- ]end design)\b")),
    ("security", re.compile(r"(?i)\b(?:threat model(?:ing)?|security (?:review|audit)|vulnerabilit(?:y|ies)|pentest)\b")),
    ("concurrency", re.compile(r"(?i)\b(?:race conditions?|deadlocks?|concurren(?:t|cy)|thread[- ]safe(?:ty)?|distributed (?:system|lock|transaction)s?)\b")),
    ("migration", re.compile(r"(?i)\b(?:migrat(?:e|ion|ing)|schema (?:design|change|migration)|backfill)\b")),
    ("debug", re.compile(r"(?i)\b(?:root[- ]cause|heisenbug|intermittent(?:ly)? fail(?:s|ing|ure)?|memory leak|data corruption|crash(?:es|ing)? under load)\b")),
    ("refactor", re.compile(r"(?i)\b(?:refactor(?:ing)?|restructur(?:e|ing))\b")),
)


@dataclass(frozen=True)
class CurrentTask:
    """The newest genuine user turn, reduced to routing-safe signals."""

    found: bool
    text: str          # raw current-task text; transient, never logged/persisted
    visible: str       # text minus control line, code fences, and quoted lines
    task_hash: str     # sha256[:16] of the normalized text; '' when not found
    authorized_max: bool
    forced_routine: bool
    hard_hint: bool
    forced_coding: str = ""  # '' | 'opus' | 'fable' — leading #opus/#fable control


@dataclass(frozen=True)
class Decision:
    """Routing decision. ``reasons`` are stable labels only — never content."""

    tier: str
    effort: str
    reasons: Tuple[str, ...]
    score: int
    authorized_max: bool = False
    substantive: bool = True
    continuation: bool = False
    status_query: bool = False
    forced_coding: str = ""  # '' | 'opus' | 'fable' — leading #opus/#fable control

    @property
    def label(self) -> str:
        return ",".join(self.reasons) or "none"


# --------------------------------------------------------------------------- #
# Current-task extraction
# --------------------------------------------------------------------------- #
def _user_part_texts(content: Any) -> List[str]:
    """Text parts of a user message; tool_result/image/output parts are skipped."""
    out: List[str] = []
    if isinstance(content, str):
        out.append(content)
    elif isinstance(content, (list, tuple)):
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                ptype = part.get("type")
                text = part.get("text")
                if isinstance(text, str) and (ptype is None or ptype in _TEXT_PART_TYPES):
                    out.append(text)
    return out


def _iter_items(request: Any) -> Iterator[Any]:
    if not isinstance(request, dict):
        return
    for key in ("messages", "input"):
        value = request.get(key)
        if key == "input" and isinstance(value, str) and value.strip():
            yield {"role": "user", "content": value}
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield item


def extract_current_user_task(request: Any) -> CurrentTask:
    """The newest genuine user turn from a Chat Completions or Responses payload.

    Walks the items in reverse and returns the first user-authored text,
    skipping tool results, function-call outputs, assistant/reasoning items and
    anything that only carries non-text parts.
    """
    for item in reversed(list(_iter_items(request))):
        if not isinstance(item, dict):
            continue
        if item.get("type") in _SKIP_ITEM_TYPES:
            continue
        if item.get("role") != "user":
            continue
        if item.get("tool_call_id") or item.get("tool_calls"):
            continue
        text = "\n".join(t for t in _user_part_texts(item.get("content")) if t).strip()
        if text:
            return task_from_text(text)
    return CurrentTask(False, "", "", "", False, False, False)


def _strip_quoted(text: str) -> str:
    """Remove fenced code blocks and ``>``-quoted lines: tags there are inert."""
    segments = text.split("```")
    outside = "\n".join(segments[0::2])  # even segments are outside fences
    lines = [ln for ln in outside.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def task_from_text(text: str) -> CurrentTask:
    """Build a :class:`CurrentTask` from raw current-turn user text.

    The wire text is first reduced to its sender-authored segment
    (:func:`genuine_user_text`); every downstream signal — the control line,
    routine/hard hints, length, the task hash — comes from that segment only,
    so the hash stays stable across tool iterations even as backfill and
    recalled memory around the same task change.
    """
    return task_from_sender_text(genuine_user_text(text))


def task_from_sender_text(text: str) -> CurrentTask:
    """:func:`task_from_text` for text that is already sender-authored (verbatim)."""
    if not text:
        # Runtime-only content (placeholder, bare memory block): no current
        # textual task. Callers fall back to the conservative middle tier
        # rather than classifying older history as if it were current.
        return CurrentTask(False, "", "", "", False, False, False)
    lines = text.splitlines()
    authorized = False
    forced = False
    forced_coding = ""
    control_index: Optional[int] = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        # Precedence on the single control line: coding force (#opus/#fable)
        # first, then max authorization, then routine force.
        if low in CONTROL_CODING:
            forced_coding, control_index = CONTROL_CODING[low], i
        elif low in CONTROL_MAX:
            authorized, control_index = True, i
        elif low in CONTROL_MIN:
            forced, control_index = True, i
        break  # only the first non-empty line can be a control line
    body = "\n".join(l for i, l in enumerate(lines) if i != control_index) if control_index is not None else text
    visible = _strip_quoted(body)
    if not forced and _FORCE_MIN_RE.search(visible):
        forced = True
    hard_hint = bool(_HARD_HINT_RE.search(visible))
    normalized = " ".join(text.split()).lower()
    task_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16] if normalized else ""
    return CurrentTask(True, text, visible, task_hash, authorized, forced, hard_hint, forced_coding)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify(task: CurrentTask, prev_state: Any = None) -> Decision:
    """Deterministically classify the current task into (tier, effort).

    ``prev_state`` is an optional bounded per-session state (``task_state.TaskState``);
    it is only consulted for short continuation turns, which retain the current
    task's tier/effort/authorization instead of resetting them.
    """
    if not task.found:
        # No user turn (internal/synthetic call): conservative middle, never max.
        return Decision("medium", "medium", ("no-user-turn",), 0, substantive=False)

    if task.forced_coding:
        # Highest-precedence explicit control. The tier here only feeds the log
        # label and a degrade target; the router forces the exact coding model
        # and leaves reasoning effort to the coding proxy. It does not persist
        # state — the force applies to this request only.
        tier = "high" if task.forced_coding == "fable" else "medium"
        return Decision(tier, "medium", (f"force:{task.forced_coding}",), 99,
                        forced_coding=task.forced_coding, substantive=False)

    if task.authorized_max:
        return Decision("max", "high", ("auth:max",), 99, authorized_max=True)
    if task.forced_routine:
        return Decision("routine", "low", ("forced:routine",), 0)

    visible = task.visible
    if len(visible) <= 48 and _CONT_RE.match(visible):
        if prev_state is not None and getattr(prev_state, "tier", None):
            return Decision(
                prev_state.tier, prev_state.effort, ("continue:retained",), 0,
                authorized_max=bool(getattr(prev_state, "authorized_max", False)),
                substantive=False, continuation=True,
            )
        return Decision("medium", "medium", ("continue:fresh",), 0,
                        substantive=False, continuation=True)

    hard_labels = [f"kw:{name}" for name, rx in HARD_CATEGORIES if rx.search(visible)]
    hard = len(hard_labels)

    if len(visible) <= 40 and _GREETING_RE.match(visible) and hard == 0:
        return Decision("routine", "low", ("greeting",), 0, substantive=False, status_query=True)
    # Status/lookup/report question: cheapest tier, and it does not reset the
    # session's current task state. When the task carries a fenced block the
    # trailing "?" alone is not enough — a pasted snippet plus "why does this
    # fail?" is debug work — but an explicit status opener ("is it done?",
    # "what does the log say?") still routes low.
    has_fence = "```" in task.text
    visible_lines = [ln.strip() for ln in visible.splitlines() if ln.strip()]
    last_line = visible_lines[-1] if visible_lines else ""
    if (
        len(visible) <= 200 and hard == 0
        and (_STATUS_START_RE.match(visible) or _STATUS_START_RE.match(last_line)
             or (visible.rstrip().endswith("?") and not has_fence))
        and len(_BULLET_RE.findall(visible)) < 3
    ):
        return Decision("routine", "low", ("status-query",), 0,
                        substantive=False, status_query=True)

    extras: List[str] = []
    n = len(visible)
    if n > 4000:
        extras.extend(("len>4k", "len>1.2k"))
    elif n > 1200:
        extras.append("len>1.2k")
    if "```" in task.text:
        extras.append("code")
    if len(_BULLET_RE.findall(visible)) >= 3 or len(_CONSTRAINT_RE.findall(visible)) >= 3:
        extras.append("constraints")
    planning = bool(_PLANNING_RE.search(visible))
    if task.hard_hint:
        extras.append("tag:hard")

    signals = hard + len(extras)
    reasons = tuple(hard_labels + extras + (["planning"] if planning else []))

    if task.hard_hint or hard >= 2 or (hard == 1 and len(extras) >= 2):
        effort = "high" if (signals >= 3 or task.hard_hint) else "medium"
        return Decision("high", effort, reasons or ("hard",), signals)
    if hard == 1 or planning or "constraints" in extras or len(extras) >= 2:
        return Decision("elevated", "medium", reasons or ("ambiguous",), signals)
    return Decision("medium", "medium", reasons or ("exec",), signals)
