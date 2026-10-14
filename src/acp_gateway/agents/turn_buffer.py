"""Bounded, ordered turn updates with reserved completion signalling."""

import asyncio
import sys
from dataclasses import dataclass, fields, is_dataclass
from itertools import chain

from acp_gateway.agents.events import AgentEvent

MAX_TURN_EVENTS = 256
MAX_TURN_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class TurnDone:
    error: str | None = None


def _size(value, limit):
    """Bound traversal itself; avoid copying large JSON trees or encoding strings."""
    size, seen, stack = 0, set(), [iter((value,))]
    while stack:
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        size += sys.getsizeof(item)
        if size > limit:
            return size
        if is_dataclass(item):
            stack.append(iter(tuple(getattr(item, field.name) for field in fields(item))))
        elif isinstance(item, dict):
            stack.append(chain.from_iterable(item.items()))
        elif isinstance(item, (list, tuple)):
            stack.append(iter(item))
    return size


class TurnBuffer:
    """Notification callbacks never block; overflow fails rather than truncates."""

    def __init__(self):
        # The extra slot is reserved for normal completion at exact capacity.
        self._queue = asyncio.Queue(maxsize=MAX_TURN_EVENTS + 1)
        self.bytes = 0
        self.failed = False
        self._sealed = False

    def put_nowait(self, event: AgentEvent | TurnDone):
        if self._sealed:
            return
        if isinstance(event, TurnDone):
            self._sealed = True
            self._queue.put_nowait((event, 0))
            return
        size = _size(event, MAX_TURN_BYTES - self.bytes)
        if self._queue.qsize() >= MAX_TURN_EVENTS or self.bytes + size > MAX_TURN_BYTES:
            self.failed = self._sealed = True
            while not self._queue.empty():
                self._queue.get_nowait()
            self.bytes = 0
            self._queue.put_nowait((TurnDone("agent turn event buffer exceeded"), 0))
            return
        self.bytes += size
        self._queue.put_nowait((event, size))

    async def get(self):
        event, size = await self._queue.get()
        self.bytes -= size
        return event

    def qsize(self):
        return self._queue.qsize()
