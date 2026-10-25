"""Bounded per-session task state for the model-router.

Holds only opaque routing state — a task hash, the decided tier/effort and the
explicit-max authorization bit. Never the task text, never the payload. Entries
expire on a sliding TTL with an absolute lifetime cap, and the store itself is
LRU-bounded, so state (and any Astra authorization) cannot bleed into a later
task or session.

Two properties make the absolute cap real rather than cosmetic:

  * ``put`` for the SAME task hash preserves the original ``created`` stamp.
    An agent loop re-derives the same current task on every tool iteration
    and re-puts it; without this, each put would restart the absolute clock
    and the cap would never fire.
  * When an entry dies of old age (absolute cap, not idle TTL) its task hash
    is retired for that session. A retired hash can neither be re-stored nor
    re-authorize Astra — the same historical ``#max`` message replayed at the
    tail of the transcript cannot renew authorization indefinitely. A task
    with different text (fresh hash) is unaffected.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class TaskState:
    task_hash: str
    tier: str
    effort: str
    authorized_max: bool = False
    created: Optional[float] = None  # set by the store; None = not yet stored
    updated: float = 0.0


class TaskStateStore:
    """LRU + TTL store keyed by session id. All operations are O(1)-ish and
    never raise on missing/expired entries."""

    def __init__(
        self,
        ttl: float = 1800.0,
        *,
        max_entries: int = 256,
        max_age_factor: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = max(60.0, float(ttl or 1800.0))
        self.max_entries = max(8, int(max_entries))
        self.max_age = self.ttl * max(1.0, float(max_age_factor))
        self._clock = clock
        self._states: "OrderedDict[str, TaskState]" = OrderedDict()
        # session -> task hash whose absolute lifetime is exhausted. Bounded LRU.
        self._retired: "OrderedDict[str, str]" = OrderedDict()

    def get(self, session_id: str) -> Optional[TaskState]:
        if not session_id:
            return None
        state = self._states.get(session_id)
        if state is None:
            return None
        now = self._clock()
        if (now - state.created) > self.max_age:
            self._states.pop(session_id, None)
            self._retire(session_id, state.task_hash)
            return None
        if (now - state.updated) > self.ttl:
            self._states.pop(session_id, None)
            return None
        self._states.move_to_end(session_id)
        return state

    def put(self, session_id: str, state: TaskState) -> None:
        if not session_id:
            return
        if self.retired(session_id, state.task_hash):
            return  # absolute lifetime exhausted: the same task never restarts the clock
        now = self._clock()
        prev = self._states.get(session_id)
        if state.created is None:
            # Same task keeps its original birth time; only a new hash starts fresh.
            if prev is not None and prev.task_hash == state.task_hash:
                state.created = prev.created if prev.created is not None else now
            else:
                state.created = now
        state.updated = now
        if (now - state.created) > self.max_age:
            self._states.pop(session_id, None)
            self._retire(session_id, state.task_hash)
            return
        self._states[session_id] = state
        self._states.move_to_end(session_id)
        while len(self._states) > self.max_entries:
            self._states.popitem(last=False)

    def _retire(self, session_id: str, task_hash: str) -> None:
        if not task_hash:
            return
        self._retired[session_id] = task_hash
        self._retired.move_to_end(session_id)
        while len(self._retired) > self.max_entries:
            self._retired.popitem(last=False)

    def retired(self, session_id: str, task_hash: str) -> bool:
        """True when this exact task already spent its absolute lifetime here."""
        return bool(task_hash) and self._retired.get(session_id) == task_hash

    def touch(self, session_id: str) -> None:
        """Sliding-TTL refresh for a continuation turn (absolute cap still applies)."""
        state = self.get(session_id)
        if state is not None:
            state.updated = self._clock()
