"""Event bus: filtering, slow subscribers, unsubscribe."""

import asyncio
from datetime import UTC, datetime

from acp_gateway.agents.events import MessageChunk
from acp_gateway.core import (
    EventBus,
    JobProgress,
    JobStarted,
    for_channel,
    for_conversation,
)
from acp_gateway.storage import Conversation, Job, JobStatus

CLI = Conversation("cli", "default", "work")
TG = Conversation("telegram", "1", "work")


def progress(conversation: Conversation, text: str = "x") -> JobProgress:
    return JobProgress(conversation, "job1", MessageChunk("s1", text))


def started(conversation: Conversation) -> JobStarted:
    job = Job("job1", 1, conversation, "s1", JobStatus.RUNNING, datetime.now(UTC))
    return JobStarted(conversation, job)


async def test_subscribers_get_matching_events_in_order():
    bus = EventBus()
    everything = bus.subscribe()
    telegram = bus.subscribe(for_channel("telegram"))
    cli = bus.subscribe(for_conversation(CLI))

    events = [started(CLI), progress(TG, "a"), progress(CLI, "b")]
    for event in events:
        bus.publish(event)
    bus.close()

    assert [e async for e in everything] == events
    assert [e async for e in telegram] == [events[1]]
    assert [e async for e in cli] == [events[0], events[2]]


async def test_slow_subscriber_drops_instead_of_blocking():
    bus = EventBus()
    slow = bus.subscribe(maxsize=2)
    for i in range(5):
        bus.publish(progress(CLI, str(i)))
    assert slow.dropped == 3
    slow.close()
    assert [e.event.text async for e in slow] == ["0", "1"]


async def test_close_wakes_a_waiting_reader():
    bus = EventBus()
    subscription = bus.subscribe()
    reader = asyncio.create_task(subscription.get())
    await asyncio.sleep(0)
    subscription.close()
    assert await asyncio.wait_for(reader, timeout=1) is None
    assert bus.subscriber_count == 0


async def test_context_manager_unsubscribes():
    bus = EventBus()
    with bus.subscribe() as subscription:
        assert bus.subscriber_count == 1
    assert subscription.closed
    bus.publish(progress(CLI))  # no subscribers left: nothing to deliver, no error
    assert await subscription.get() is None


async def test_failing_filter_does_not_break_other_subscribers():
    bus = EventBus()

    def broken(event):
        raise RuntimeError("boom")

    bus.subscribe(broken)
    healthy = bus.subscribe()
    bus.publish(progress(CLI))
    healthy.close()
    assert len([e async for e in healthy]) == 1
