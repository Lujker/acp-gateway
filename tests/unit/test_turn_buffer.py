import pytest

from acp_gateway.agents.events import MessageChunk, ThoughtChunk, ToolCallStarted
from acp_gateway.agents.turn_buffer import MAX_TURN_BYTES, MAX_TURN_EVENTS, TurnBuffer, TurnDone


async def test_completion_has_reserved_capacity_and_preserves_order():
    buffer = TurnBuffer()
    for index in range(MAX_TURN_EVENTS):
        buffer.put_nowait(MessageChunk("s1", str(index)))
    buffer.put_nowait(TurnDone())
    for index in range(MAX_TURN_EVENTS):
        assert (await buffer.get()).text == str(index)
    assert (await buffer.get()).error is None
    assert buffer.bytes == 0 and not buffer.failed


async def test_event_flood_is_bounded_and_wakes_consumer_with_failure():
    buffer = TurnBuffer()
    for _ in range(MAX_TURN_EVENTS * 10):
        buffer.put_nowait(ThoughtChunk("s1", "reasoning"))
    assert buffer.failed and buffer.qsize() == 1 and buffer.bytes == 0
    buffer.put_nowait(TurnDone())  # Late request completion cannot mask failure.
    assert (await buffer.get()).error == "agent turn event buffer exceeded"
    assert buffer.qsize() == 0


@pytest.mark.parametrize("kind", ["thought", "tool"])
async def test_one_oversized_non_answer_event_fails_without_copying_payload(kind):
    buffer = TurnBuffer()
    text = "x" * MAX_TURN_BYTES
    event = (
        ThoughtChunk("s1", text)
        if kind == "thought"
        else ToolCallStarted("s1", "t1", "tool", raw_input={"nested": [text]})
    )
    buffer.put_nowait(event)
    assert buffer.failed and buffer.bytes == 0 and buffer.qsize() == 1
    assert (await buffer.get()).error


async def test_consumption_releases_budget_for_long_incremental_turn():
    buffer = TurnBuffer()
    for _ in range(MAX_TURN_EVENTS * 4):
        event = ThoughtChunk("s1", "x" * 10000)
        buffer.put_nowait(event)
        assert await buffer.get() == event
        assert buffer.bytes == 0
    buffer.put_nowait(TurnDone())
    assert not buffer.failed and (await buffer.get()).error is None
