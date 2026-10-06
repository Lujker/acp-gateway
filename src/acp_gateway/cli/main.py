"""``acpgw`` command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from acp_gateway import __version__, paths
from acp_gateway.config import AppConfig, load_config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acpgw", description="ACP Gateway")
    parser.add_argument("--version", action="version", version=f"acpgw {__version__}")
    parser.add_argument("--config", type=Path, help="path to config.yaml")
    parser.add_argument("--env-file", type=Path, help="path to the .env file with secrets")
    commands = parser.add_subparsers(dest="command", required=True)

    config_cmd = commands.add_parser("config", help="inspect configuration")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("check", help="validate configuration and show a summary")

    commands.add_parser("paths", help="show platform directories used by the gateway")
    return parser


def _cmd_config_check(cfg: AppConfig) -> int:
    s = cfg.settings
    print(f"config file: {cfg.config_path or '(none, defaults)'}")
    print(f"env file:    {cfg.env_file or '(none)'}")
    print(f"data dir:    {s.resolved_data_dir()}")
    print(f"gateway:     http://{s.gateway.host}:{s.gateway.port}")
    for name in (s.gateway.api_token_env, s.gateway.mcp_token_env):
        print(f"  {name}: {'set' if name in cfg.secrets else 'missing'}")

    print(f"agents:      {len(s.agents)}")
    for agent in s.agents:
        if agent.tls_fingerprint:
            tls = "pinned"
        elif agent.uses_tls:
            tls = "trust-on-first-use"
        else:
            tls = "none (loopback)" if not agent.allow_insecure_transport else "NONE (insecure)"
        secret = "-"  # noqa: S105 (display placeholder)
        if agent.secret_env:
            state = "set" if agent.secret_env in cfg.secrets else "missing"
            secret = f"{agent.secret_env} ({state})"
        print(f"  - {agent.alias} [{agent.kind}/{agent.backend}] {agent.display_name}")
        print(f"    url: {agent.url}  tls: {tls}")
        print(f"    secret: {secret}  cwd: {agent.default_cwd}")
        print(f"    endpoint: {agent.acp_endpoint}  session mode: {agent.session_mode or '-'}")

    missing = cfg.missing_agent_secrets()
    if missing:
        print("error: missing agent secrets: " + ", ".join(missing), file=sys.stderr)
        return 1
    print("ok")
    return 0


def _cmd_paths() -> int:
    print(f"config: {paths.config_dir()}")
    print(f"data:   {paths.data_dir()}")
    print(f"logs:   {paths.log_dir()}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "paths":
        return _cmd_paths()

    try:
        cfg = load_config(args.config, args.env_file)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"error: invalid configuration\n{exc}", file=sys.stderr)
        return 2

    if args.command == "config" and args.config_command == "check":
        return _cmd_config_check(cfg)
    return 2
