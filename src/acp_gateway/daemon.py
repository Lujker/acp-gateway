"""Compose the configured core and run one owner API per data directory."""

import secrets
from contextlib import contextmanager

import uvicorn
from filelock import FileLock, Timeout

from acp_gateway.agents import AgentClient
from acp_gateway.api.app import create_app
from acp_gateway.channels.cli import CliChannel
from acp_gateway.config import AppConfig
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
        agents = {
            p.alias: AgentClient(
                p,
                config.secrets.get(p.secret_env) if p.secret_env else None,
                pin_dir=data_dir / "pins",
            )
            for p in config.settings.agents
        }
        core = GatewayCore(store, agents, policy=config.settings.policy)
        cli = CliChannel()
        core.add_channel(cli)
        yield create_app(core, cli, token)
    finally:
        if store is not None:
            store.close()
        lock.release()


def serve(config: AppConfig) -> None:
    settings = config.settings
    configure_logging(settings.logging.level, settings.logging.format)
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
