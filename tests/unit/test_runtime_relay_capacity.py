import asyncio
from uuid import uuid4

from acp_gateway.connectors.protocol import Close, Open, RuntimeRequest, encode_frame
from acp_gateway.connectors.relay import RelayPeer


async def test_runtime_worker_does_not_use_agent_stream_capacity(monkeypatch):
    monkeypatch.setattr("acp_gateway.connectors.relay.MAX_STREAMS", 1)
    epoch, first, second = uuid4(), uuid4(), uuid4()
    frames = [
        RuntimeRequest(epoch=epoch, request=uuid4(), action="diagnostics"),
        Open(epoch=epoch, stream=first, alias="code"),
        Open(epoch=epoch, stream=second, alias="code"),
    ]

    class Connection:
        async def __aiter__(self):
            for frame in frames:
                yield encode_frame(frame)

    async def blocked(*args):
        await asyncio.Event().wait()

    peer = RelayPeer(
        Connection(),
        epoch,
        ["code"],
        local_factory=blocked,
        runtime_inventory=True,
        runtime_diagnostics=True,
        runtime_handler=blocked,
    )
    peer._local = blocked
    replies = []

    async def send(frame):
        replies.append(frame)

    peer.send = send
    try:
        await peer._read()
        assert first in peer.streams
        assert len(peer.local_workers) == 1
        assert len(peer.runtime_workers) == 1
        assert len(replies) == 1 and isinstance(replies[0], Close)
        assert replies[0].stream == second and replies[0].code == "access_denied"
    finally:
        for task in peer.tasks:
            task.cancel()
        await asyncio.gather(*peer.tasks, return_exceptions=True)


def test_inventory_request_keeps_original_wire_shape():
    request = RuntimeRequest(epoch=uuid4(), request=uuid4())
    encoded = encode_frame(request)
    assert '"action"' not in encoded
    assert '"alias"' not in encoded
