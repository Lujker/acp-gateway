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
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            get_console = ctypes.WinDLL("kernel32").GetConsoleWindow
            get_console.argtypes = []
            get_console.restype = wintypes.HWND
            get_logger(__name__).info("connector runtime", console_attached=bool(get_console()))
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
        from acp_gateway.connectors.profiles import LocalProfiles

        profiles = LocalProfiles(config, launch, maintenance, clients, policies, managers)
        from acp_gateway.connectors.maintenance import can_reserve
        from acp_gateway.update_service import LocalUpdates

        updates = LocalUpdates(
            config,
            role="connector",
            target=launch.computer_id,
            idle_check=lambda: can_reserve(maintenance.status()),
            activity_status=maintenance.status,
        )

        async def local_factory(alias, *, diagnostic_failure=None):
            maintenance.check_admission()
            if clients[alias].profile.backend == "claude_code":
                from acp_gateway.adapters.claude_code import ClaudeCodeProcessManager

                if alias not in managers:
                    managers[alias] = ClaudeCodeProcessManager(
                        clients[alias].profile.claude_code,
                        policies[alias],
                        sessions=clients[alias].managed_sessions(),
                    )
                return PolicyTransport(
                    await managers[alias].open(on_failed_cleanup=diagnostic_failure),
                    policies[alias],
                )
            if clients[alias].profile.backend == "codex":
                from acp_gateway.adapters.codex import CodexProcessManager

                if alias not in managers:
                    managers[alias] = CodexProcessManager(
                        clients[alias].profile.codex,
                        policies[alias],
                        sessions=clients[alias].managed_sessions(),
                    )
                return PolicyTransport(await managers[alias].open(), policies[alias])
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

        async def ensure_updater():
            from acp_gateway.update_bootstrap import ensure_updates

            try:
                result = await asyncio.to_thread(
                    ensure_updates,
                    config,
                    role="connector",
                    target=launch.computer_id,
                )
                if not result.ready:
                    get_logger(__name__).warning("updater not ready", reason=result.reason)
            except Exception:
                get_logger(__name__).exception("updater provisioning failed")

        from acp_gateway.connectors.computer_diagnostics import ComputerDiagnostics

        diagnostics = ComputerDiagnostics(config, maintenance, updates, clients, local_factory)
        maintenance.services.append(diagnostics)

        async def connect(instance):
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            stopping = False
            shell = None

            def shell_service():
                nonlocal shell
                if shell is None:
                    from acp_gateway.connectors.shell_service import LocalShell

                    def owned(alias, sid, cwd):
                        client = clients.get(alias)
                        return bool(
                            client
                            and client.profile.backend in {"codex", "claude_code"}
                            and client.managed_sessions().contains(sid, cwd)
                        )

                    shell = LocalShell(
                        directory / "shell-operations.db",
                        profiles=lambda: config.settings.agents,
                        authorize=owned,
                        admission=maintenance.check_admission,
                    )
                    maintenance.services.append(shell)
                return shell

            async def shell_request(request):
                return await shell_service().request(request)

            async def runtime_request(request):
                if request.action not in {None, "inventory"}:
                    return await diagnostics.request(request)
                return connector_inventory(
                    config.settings.agents, managers, request.page, request.candidates
                )

            # Recover pending shell fences before publishing activity, even when
            # its original profile was removed. New profiles can enable it lazily.
            if (directory / "shell-operations.db").exists() or any(
                p.backend in {"codex", "claude_code"} for p in config.settings.agents
            ):
                shell_service()

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
            from acp_gateway.adapters.runtime_inventory import connector_inventory, watch_idle

            idle_cleanup = asyncio.create_task(watch_idle(managers))
            provisioning = asyncio.create_task(ensure_updater())
            try:
                await run_connector(
                    launch.dispatcher_url,
                    credential=credential,
                    hello=lambda: launch.hello(config),
                    fingerprint=launch.tls_fingerprint,
                    local_factory=local_factory,
                    installation_report=updates.report,
                    update_handler=updates.request,
                    profiles_handler=(
                        profiles.request if config.settings.connector.remote_profiles else None
                    ),
                    shell_handler=shell_request,
                    runtime_handler=runtime_request,
                )
            except asyncio.CancelledError:
                if not stopping:
                    raise
                get_logger(__name__).info("computer connector stopped")
            finally:
                idle_cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await idle_cleanup
                provisioning.cancel()
                with suppress(asyncio.CancelledError):
                    await provisioning
                activity.cancel()
                with suppress(asyncio.CancelledError, OSError, ValueError):
                    await activity
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
                if handles_sigterm:
                    loop.remove_signal_handler(signal.SIGTERM)
                try:
                    if shell is not None:
                        await shell.close()
                finally:
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


def connector_installation_report():
    from acp_gateway.updates import installation_report

    return installation_report(role="connector")
