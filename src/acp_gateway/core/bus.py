"""In-process event bus: channels and the local API subscribe to gateway events.

Publishing never blocks the core. Each subscriber has a bounded queue; when a
subscriber falls behind, new events for it are dropped and counted rather
than stalling the turn that produced them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from acp_gateway.agents.events import AgentEvent
from acp_gateway.log import get_logger
from acp_gateway.storage.records import Conversation, Job, SessionRecord

DEFAULT_QUEUE_SIZE = 1000


@dataclass(frozen=True)
class GatewayEvent:
    conversation: Conversation


@dataclass(frozen=True)
class SessionCreated(GatewayEvent):
    session: SessionRecord


@dataclass(frozen=True)
class JobStarted(GatewayEvent):
    job: Job


@dataclass(frozen=True)
class JobProgress(GatewayEvent):
    """A raw agent event of a running job (tool calls, chunks, usage...)."""

    job_id: str
    event: AgentEvent


@dataclass(frozen=True)
class JobFinished(GatewayEvent):
    """The job reached a final status; ``job.answer`` holds the whole answer."""

    job: Job


EventFilter = Callable[[GatewayEvent], bool]


def for_channel(channel: str) -> EventFilter:
    return lambda event: event.conversation.channel == channel


def for_conversation(conversation: Conversation) -> EventFilter:
    return lambda event: event.conversation == conversation


class Subscription:
    """An async iterator of events; close it (or use ``with``) to unsubscribe."""

    def __init__(self, bus: EventBus, accept: EventFilter | None, maxsize: int) -> None:
        self._bus = bus
        self._accept = accept
        self._queue: asyncio.Queue[GatewayEvent | None] = asyncio.Queue(maxsize)
        self.dropped = 0
        self.closed = False

    def _offer(self, event: GatewayEvent) -> None:
        if self.closed or (self._accept is not None and not self._accept(event)):
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                self._bus._log.warning(
                    "slow event subscriber, events dropped", dropped=self.dropped
                )

    async def get(self) -> GatewayEvent | None:
        """The next event, or ``None`` once the subscription is closed."""
        if self.closed and self._queue.empty():
            return None
        return await self._queue.get()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._bus._subscribers.discard(self)
        # Wake a reader blocked in get(); if the queue is full it is not blocked.
        if not self._queue.full():
            self._queue.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[GatewayEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[GatewayEvent]:
        while (event := await self.get()) is not None:
            yield event

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()
        self._log = get_logger(__name__)

    def subscribe(
        self, accept: EventFilter | None = None, *, maxsize: int = DEFAULT_QUEUE_SIZE
    ) -> Subscription:
        subscription = Subscription(self, accept, maxsize)
        self._subscribers.add(subscription)
        return subscription

    def publish(self, event: GatewayEvent) -> None:
        for subscription in tuple(self._subscribers):
            try:
                subscription._offer(event)
            except Exception:
                self._log.exception("event filter failed")

    def close(self) -> None:
        for subscription in tuple(self._subscribers):
            subscription.close()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
