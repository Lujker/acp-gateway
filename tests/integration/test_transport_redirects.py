"""A redirect must never receive the credentials of the configured agent."""

from http import HTTPStatus

import pytest
from websockets.asyncio.server import serve

from acp_gateway.agents import AgentUnavailable
from acp_gateway.agents.transport import PinnedWebSocketTransport


@pytest.mark.parametrize("status", [HTTPStatus.FOUND, HTTPStatus.TEMPORARY_REDIRECT])
async def test_redirect_is_refused_before_contacting_target(status):
    received = []

    async def handler(connection):
        await connection.close()

    def capture(connection, request):
        received.append(request.headers.get("X-Secret-Key"))

    async with serve(handler, "127.0.0.1", 0, process_request=capture) as target:
        target_port = target.sockets[0].getsockname()[1]

        def redirect(connection, request):
            response = connection.respond(status, "Redirect")
            response.headers["Location"] = f"ws://127.0.0.1:{target_port}/acp"
            return response

        async with serve(handler, "127.0.0.1", 0, process_request=redirect) as origin:
            port = origin.sockets[0].getsockname()[1]
            with pytest.raises(AgentUnavailable, match=f"HTTP {status.value}"):
                await PinnedWebSocketTransport.connect(
                    f"ws://127.0.0.1:{port}/acp",
                    headers={"X-Secret-Key": "test-agent-secret"},
                    pin=None,
                )
    assert received == []
