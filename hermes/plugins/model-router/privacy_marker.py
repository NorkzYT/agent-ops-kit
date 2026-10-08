"""Strict per-request ``#privacy`` marker — the only force-local control.

Policy: a request is private (answered by the local model or refused, never
sent to a cloud provider) when, and only when, the CURRENT genuine user turn
carries a standalone ``#privacy`` token in the sender's own text. Nothing else
makes a request private:

  * no content classification — ID numbers, cards, bank numbers, secrets,
    credentials and configured terms/paths do not route anything
    (``privacy.scan_request`` remains for offline tooling only);
  * ``#private`` / ``#local`` are inert;
  * no session state — the next turn without the marker is public again.

Provenance (the marker must be authored by the sender for this request):

  * only the newest user-role turn counts; system/developer prompts,
    assistant turns, tool results and Responses function outputs never do;
  * inside that turn only the sender segment counts
    (``task_intent.split_sender_segment``): gateway backfill before
    ``[New message]``, prepended runtime blocks (reply quotes, document notes),
    appended memory/attached context and this plugin's specialist note are
    inert, and Hermes runtime envelopes (delegation results, Kanban wakes,
    process notices) are not sender tasks;
  * fenced code and ``>``-quoted lines are inert, like every other control;
  * a verified inline attachment relay is file content, not prose: only the
    typed prose counts. An upload sent with no typed text is the prompt, so
    its text counts; when the relay boundary cannot be proven the whole sender
    segment is checked (both fail toward local).

History: an earlier marked exchange — the marked user turn and every item up
to the next user turn — is withheld from later public requests
(:func:`withhold_marked_history`), so the private turn and its local answer are
not replayed to the cloud. Roles and tool-call ids/names are kept so provider
pairing rules still hold.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from . import attachments as att
from . import task_intent as ti
from .privacy import PrivacyDecision

MARKER = "#privacy"
_MARKER_RE = re.compile(r"(?<![A-Za-z0-9_#])#privacy(?![A-Za-z0-9_\-])", re.IGNORECASE)

WITHHELD = "[withheld: this earlier turn was marked private and answered locally]"

PUBLIC = PrivacyDecision(False, ())
MARKED = PrivacyDecision(True, ("marker",))


def has_marker(text: str) -> bool:
    """A standalone ``#privacy`` token anywhere in ``text`` (case-insensitive)."""
    return bool(_MARKER_RE.search(text or ""))


def turn_is_marked(raw_text: str) -> bool:
    """Does this wire-format user turn carry a sender-authored ``#privacy``?"""
    raw = raw_text or ""
    if not raw.strip() or ti.runtime_envelope(raw):
        return False
    sep = att.separate_sender_prose(raw)
    if not sep.proven:
        text = ti.genuine_user_text(raw)  # unprovable relay boundary: check it all
    else:
        text = sep.prose or sep.attached  # an upload sent as the whole prompt counts
    return has_marker(ti.task_from_sender_text(text).visible)


def _is_user_turn(item: Any) -> bool:
    return (isinstance(item, dict) and item.get("role") == "user"
            and item.get("type") not in ti.SKIP_ITEM_TYPES
            and not item.get("tool_call_id") and not item.get("tool_calls")
            and bool(_turn_text(item)))


def _turn_text(item: dict) -> str:
    return "\n".join(t for t in ti.user_part_texts(item.get("content")) if t).strip()


def decide(request: Any) -> PrivacyDecision:
    """Privacy decision for one outbound request: the newest user turn only."""
    items = list(ti.iter_items(request))
    for item in reversed(items):
        if _is_user_turn(item):
            return MARKED if turn_is_marked(_turn_text(item)) else PUBLIC
    return PUBLIC


# --------------------------------------------------------------------------- #
# History withholding for public requests
# --------------------------------------------------------------------------- #
_KEEP_KEYS = ("role", "type", "id", "call_id", "tool_call_id", "name", "status")
_TEXT_TYPES = ("text", "input_text", "output_text")


def _withhold_parts(parts: Any) -> List[Any]:
    """Content parts minus their payload. tool_use/tool_result blocks keep their
    ids (pairing) and come first; everything else becomes one placeholder."""
    out: List[Any] = []
    text_type: Optional[str] = None
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "tool_use":
            out.append({**{k: part[k] for k in ("type", "id", "name") if k in part}, "input": {}})
        elif isinstance(part, dict) and part.get("type") == "tool_result":
            out.append({**{k: part[k] for k in ("type", "tool_use_id") if k in part}, "content": WITHHELD})
        elif text_type is None:
            ptype = part.get("type") if isinstance(part, dict) else None
            text_type = ptype if ptype in _TEXT_TYPES else "text"
    if text_type or not out:
        out.append({"type": text_type or "text", "text": WITHHELD})
    return out


def _withhold_call(call: Any) -> Any:
    if not isinstance(call, dict):
        return call
    out = {k: call[k] for k in ("id", "type", "call_id", "name") if k in call}
    fn = call.get("function")
    if isinstance(fn, dict):
        out["function"] = {**({"name": fn["name"]} if "name" in fn else {}), "arguments": "{}"}
    return out


def _withhold_item(item: Any) -> Any:
    """One history item with its payload replaced. ``None`` drops the item."""
    if isinstance(item, str):
        return WITHHELD
    if not isinstance(item, dict):
        return item
    if item.get("type") == "reasoning":
        return None
    out = {k: item[k] for k in _KEEP_KEYS if k in item}
    for key, value in item.items():
        if key == "content":
            out[key] = (value if value is None
                        else _withhold_parts(value) if isinstance(value, (list, tuple)) else WITHHELD)
        elif key == "tool_calls" and isinstance(value, (list, tuple)):
            out[key] = [_withhold_call(c) for c in value]
        elif key == "function_call" and isinstance(value, dict):
            out[key] = _withhold_call({"function": value})["function"]
        elif key == "arguments":
            out[key] = "{}"
        elif key in ("output", "result", "tool_result"):
            out[key] = WITHHELD
        elif key == "input":
            out[key] = {} if isinstance(value, dict) else WITHHELD
        # Anything else (reasoning_content, refusal, annotations, audio, ...) is dropped.
    return out


def withhold_marked_history(request: Any) -> Tuple[Any, int]:
    """``(request', n)``: ``request`` with every earlier marked exchange
    withheld; ``n`` items were replaced or dropped. The input is never mutated
    and is returned as-is (same object) when nothing is withheld. The newest
    user turn and everything after it (the current task's own tool loop) are
    untouched — a marked current turn never reaches a public request."""
    if not isinstance(request, dict):
        return request, 0
    replaced: dict = {}
    total = 0
    for key in ("messages", "input"):
        items = request.get(key)
        if not isinstance(items, (list, tuple)):
            continue
        turns = [i for i, item in enumerate(items) if _is_user_turn(item)]
        if len(turns) < 2:
            continue
        current, earlier = turns[-1], set(turns[:-1])
        out: List[Any] = []
        marked, count = False, 0
        for i, item in enumerate(items):
            if i in earlier:
                marked = turn_is_marked(_turn_text(item))
            if marked and i < current:
                count += 1
                new = _withhold_item(item)
                if new is not None:
                    out.append(new)
            else:
                out.append(item)
        if count:
            replaced[key] = out
            total += count
    if not replaced:
        return request, 0
    return {**request, **replaced}, total
