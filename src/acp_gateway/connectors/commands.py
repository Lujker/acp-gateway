"""Foreground registration modes; the owner API remains a separate listener."""

import asyncio
import ssl

from filelock import FileLock, Timeout

from acp_gateway.connectors.control import (
    ControlDispatcher,
    read_credential,
    run_connector,
    validate_connect_path,
)
from acp_gateway.connectors.protocol import AgentManifest, Hello
from acp_gateway.storage import Store


def run(config, args):
    if args.command == "connector":
        if not config.settings.agents:
            raise ValueError("configure at least one local agent before registering a computer")
        try:
            hello = Hello(
                computer_id=args.computer_id,
                agents=[
                    AgentManifest(alias=p.alias, display_name=p.display_name)
                    for p in config.settings.agents
                ],
            )
        except ValueError:
            raise ValueError("invalid computer ID or agent manifest") from None
        asyncio.run(
            run_connector(
                args.dispatcher_url,
                credential=read_credential(args.token_file),
                hello=hello,
                fingerprint=args.tls_fingerprint,
            )
        )
        return 0

    if not 1 <= args.port <= 65535:
        raise ValueError("dispatcher port must be between 1 and 65535")
    validate_connect_path(args.connect_path)
    if bool(args.tls_cert) != bool(args.tls_key):
        raise ValueError("provide both --tls-cert and --tls-key to enable TLS")
    tls = None
    if args.tls_cert is not None:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.tls_cert, args.tls_key)
    directory = config.settings.resolved_data_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = FileLock(directory / "dispatcher.lock", timeout=0, mode=0o600)
    try:
        lock.acquire()
    except Timeout:
        raise ValueError("another dispatcher owns this data directory") from None
    store = None
    try:
        store = Store.open_in(directory)
        dispatcher = ControlDispatcher(store.computers, connect_path=args.connect_path)

        async def serve():
            async with dispatcher.listen(args.host, args.port, tls=tls):
                print("dispatcher listening; computer registration only", flush=True)
                await asyncio.Future()

        asyncio.run(serve())
    finally:
        if store is not None:
            store.close()
        lock.release()
    return 0
