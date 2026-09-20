"""Bounded per-session task state for the model-router.

Holds only opaque routing state — a task hash, the decided tier/effort and the
explicit-max authorization bit. Never the task text, never the payload. Entries
expire on a sliding TTL with an absolute lifetime cap, and the store itself is
LRU-bounded, so state (and any Astra authorization) cannot bleed into a later
task or session.
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
    created: float = 0.0
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

    def get(self, session_id: str) -> Optional[TaskState]:
        if not session_id:
            return None
        state = self._states.get(session_id)
        if state is None:
            return None
        now = self._clock()
        if (now - state.updated) > self.ttl or (now - state.created) > self.max_age:
            self._states.pop(session_id, None)
            return None
        self._states.move_to_end(session_id)
        return state

    def put(self, session_id: str, state: TaskState) -> None:
        if not session_id:
            return
        now = self._clock()
        state.created = state.created or now
        state.updated = now
        self._states[session_id] = state
        self._states.move_to_end(session_id)
        while len(self._states) > self.max_entries:
            self._states.popitem(last=False)

    def touch(self, session_id: str) -> None:
        """Sliding-TTL refresh for a continuation turn (absolute cap still applies)."""
        state = self.get(session_id)
        if state is not None:
            state.updated = self._clock()
