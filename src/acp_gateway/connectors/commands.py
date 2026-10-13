"""Foreground registration modes; the owner API remains a separate listener."""

import asyncio
import re
import signal
import ssl
import sys
from contextlib import suppress

from filelock import FileLock, Timeout

from acp_gateway.agents import AgentClient
from acp_gateway.connectors.control import (
    ControlDispatcher,
    read_credential,
    run_connector,
    validate_connect_path,
)
from acp_gateway.connectors.launch import ConnectorLaunch, local_policies
from acp_gateway.connectors.lifecycle import control_record, watch_stop
from acp_gateway.connectors.policy import PolicyTransport
from acp_gateway.log import configure_logging, get_logger
from acp_gateway.storage import Store


def run(config, args):
    if args.command == "connector":
        launch = ConnectorLaunch.from_args(args, config)
        control_id = getattr(args, "control_id", None)
        if control_id is not None and not re.fullmatch(r"[a-f0-9]{32}", control_id):
            raise ValueError("invalid connector control identity")
        credential = read_credential(launch.token_file)
        settings = config.settings.logging
        configure_logging(
            settings.level,
            settings.format,
            file=settings.file,
            max_bytes=settings.max_bytes,
            backup_count=settings.backup_count,
        )
        clients = {
            p.alias: AgentClient(
                p,
                config.secrets.get(p.secret_env) if p.secret_env else None,
                pin_dir=config.settings.resolved_data_dir() / "pins",
            )
            for p in config.settings.agents
        }
        policies = local_policies(config)
        managers = {}
        from acp_gateway.connectors.maintenance import ConnectorMaintenance, watch

        maintenance = ConnectorMaintenance(config.settings.agents, managers)

        async def local_factory(alias):
            maintenance.check_admission()
            if clients[alias].profile.backend == "hermes":
                from acp_gateway.adapters.hermes_sessions import HermesSessionManager

                if alias not in managers:

                    async def engine():
                        transport, _ = await clients[alias].open_transport()
                        return transport

                    managers[alias] = HermesSessionManager(engine, policies[alias])
                return PolicyTransport(await managers[alias].open(), policies[alias])
            transport, _ = await clients[alias].open_transport()
            return PolicyTransport(transport, policies[alias])

        async def connect(instance):
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            stopping = False

            def terminate():
                nonlocal stopping
                if stopping:
                    return
                stopping = True
                task.cancel()

            handles_sigterm = sys.platform != "win32"
            if handles_sigterm:
                loop.add_signal_handler(signal.SIGTERM, terminate)
            watcher = asyncio.create_task(watch_stop(directory, instance, terminate))
            activity = asyncio.create_task(watch(directory, instance, maintenance))
            try:
                await run_connector(
                    launch.dispatcher_url,
                    credential=credential,
                    hello=launch.hello(config),
                    fingerprint=launch.tls_fingerprint,
                    local_factory=local_factory,
                )
            except asyncio.CancelledError:
                if not stopping:
                    raise
                get_logger(__name__).info("computer connector stopped")
            finally:
                activity.cancel()
                with suppress(asyncio.CancelledError, OSError, ValueError):
                    await activity
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
                if handles_sigterm:
                    loop.remove_signal_handler(signal.SIGTERM)
                for manager in managers.values():
                    await manager.close()

        directory = config.settings.resolved_data_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = FileLock(directory / "connector.lock", timeout=0, mode=0o600)
        try:
            lock.acquire()
        except Timeout:
            raise ValueError("another computer connector owns this data directory") from None
        try:
            with control_record(directory, control_id=control_id) as instance:
                asyncio.run(connect(instance))
        finally:
            lock.release()
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
