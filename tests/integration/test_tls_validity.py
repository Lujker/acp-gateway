"""Fixture clock tolerance must not disable production certificate validity checks."""

import asyncio
import contextlib
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from acp_gateway.agents.tls import pinned_context
from fakes.certs import make_cert


@pytest.mark.parametrize("offset,verify_code", [(60, None), (600, 9), (-172800, 10)])
async def test_pinned_tls_tolerates_fixture_skew_but_rejects_invalid_dates(
    tmp_path, offset, verify_code
):
    cert, key = make_cert(tmp_path, now=datetime.now(UTC) + timedelta(seconds=offset))
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    client_tls = pinned_context(ssl.PEM_cert_to_DER_cert(Path(cert).read_text()))

    async def handle(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async with await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_tls) as server:
        port = server.sockets[0].getsockname()[1]
        if verify_code is None:
            _, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_tls)
            writer.close()
            await writer.wait_closed()
        else:
            with pytest.raises(ssl.SSLCertVerificationError) as error:
                await asyncio.open_connection("127.0.0.1", port, ssl=client_tls)
            assert error.value.verify_code == verify_code
