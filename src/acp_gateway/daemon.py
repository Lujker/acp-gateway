"""Compose the configured core and run one owner API per data directory."""

import secrets
import ssl
from contextlib import contextmanager

import uvicorn
from filelock import FileLock, Timeout

from acp_gateway.agents import AgentClient
from acp_gateway.api.app import create_app
from acp_gateway.channels.cli import CliChannel
from acp_gateway.channels.mcp import McpChannel
from acp_gateway.config import AppConfig
from acp_gateway.connectors.control import ControlDispatcher
from acp_gateway.core import GatewayCore
from acp_gateway.log import configure_logging
from acp_gateway.storage import Store


def owner_token(config: AppConfig):
    token = config.secrets.get(config.settings.gateway.api_token_env)
    if token is None:
        raise ValueError(f"missing owner API token: {config.settings.gateway.api_token_env}")
    mcp = config.secrets.get(config.settings.gateway.mcp_token_env)
    if mcp is not None and secrets.compare_digest(
        token.get_secret_value().encode(), mcp.get_secret_value().encode()
    ):
        raise ValueError("owner API and MCP tokens must be different")
    for profile in config.settings.agents:
        agent_secret = config.secrets.get(profile.secret_env) if profile.secret_env else None
        if agent_secret is not None and secrets.compare_digest(
            token.get_secret_value().encode(), agent_secret.get_secret_value().encode()
        ):
            raise ValueError("owner API and agent credentials must be different")
    return token


@contextmanager
def configured_app(config: AppConfig):
    token = owner_token(config)
    mcp_token = config.secrets.get(config.settings.gateway.mcp_token_env)
    if mcp_token is not None:
        for profile in config.settings.agents:
            secret = config.secrets.get(profile.secret_env) if profile.secret_env else None
            if secret is not None and secrets.compare_digest(
                mcp_token.get_secret_value().encode(), secret.get_secret_value().encode()
            ):
                raise ValueError("MCP and agent credentials must be different")
    missing = config.missing_agent_secrets()
    if missing:
        raise ValueError("missing agent secrets: " + ", ".join(missing))
    data_dir = config.settings.resolved_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = FileLock(data_dir / "daemon.lock", timeout=0, mode=0o600)
    try:
        lock.acquire()
    except Timeout as exc:
        raise ValueError("another gateway daemon already owns this data directory") from exc
    store = None
    try:
        store = Store.open_in(data_dir)
        dispatcher = (
            ControlDispatcher(store.computers, connect_path=config.settings.connector.connect_path)
            if config.settings.connector.enabled
            else None
        )
        agents = {
            p.address: AgentClient(
                p,
                config.secrets.get(p.secret_env) if p.secret_env else None,
                pin_dir=data_dir / "pins",
                transport_factory=(lambda p=p: dispatcher.open_agent(p.computer_id, p.alias))
                if p.backend == "connector"
                else None,
            )
            for p in config.settings.agents
        }
        core = GatewayCore(store, agents, policy=config.settings.policy)
        cli = CliChannel()
        core.add_channel(cli)
        if config.settings.telegram.enabled:
            from acp_gateway.channels.telegram import TelegramChannel

            telegram_token = config.secrets.get(config.settings.telegram.token_env)
            if telegram_token is None:
                raise ValueError("missing Telegram bot token")
            for name in config.referenced_secret_names():
                if name == config.settings.telegram.token_env:
                    continue
                other = config.secrets.get(name)
                if other is not None and secrets.compare_digest(
                    telegram_token.get_secret_value().encode(), other.get_secret_value().encode()
                ):
                    raise ValueError("Telegram and other credentials must be different")
            core.add_channel(TelegramChannel(config.settings.telegram, telegram_token))
        mcp = McpChannel(core, mcp_token) if mcp_token is not None else None
        if mcp is not None:
            core.add_channel(mcp)
        ingress = None
        if dispatcher is not None:
            listener = config.settings.connector
            tls = None
            if listener.tls_cert is not None:
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls.minimum_version = ssl.TLSVersion.TLSv1_2
                tls.load_cert_chain(listener.tls_cert, listener.tls_key)

            def ingress():
                return dispatcher.listen(listener.host, listener.port, tls=tls)

        app = create_app(core, cli, token, mcp=mcp, ingress=ingress)
        app.state.dispatcher = dispatcher
        yield app
    finally:
        if store is not None:
            store.close()
        lock.release()


def serve(config: AppConfig) -> None:
    settings = config.settings
    configure_logging(
        settings.logging.level,
        settings.logging.format,
        file=settings.logging.file,
        max_bytes=settings.logging.max_bytes,
        backup_count=settings.logging.backup_count,
    )
    with configured_app(config) as app:
        uvicorn.run(
            app,
            host=settings.gateway.host.strip("[]"),
            port=settings.gateway.port,
            workers=1,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
