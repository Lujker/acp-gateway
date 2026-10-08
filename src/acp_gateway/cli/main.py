"""``acpgw`` command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
from pydantic import ValidationError

from acp_gateway import __version__, paths
from acp_gateway.config import AppConfig, load_config
from acp_gateway.log import redact_text


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acpgw",
        description="ACP Gateway: route conversations to ACP agents with human approvals.",
        epilog="Getting started: acpgw setup → edit configuration → acpgw config check → "
        "acpgw serve (foreground) or acpgw service install / enable (autostart). "
        "Use acpgw <command> --help for details.",
    )
    parser.add_argument("--version", action="version", version=f"acpgw {__version__}")
    parser.add_argument("--config", type=Path, help="path to config.yaml")
    parser.add_argument("--env-file", type=Path, help="path to the .env file with secrets")
    commands = parser.add_subparsers(dest="command", required=True)

    config_cmd = commands.add_parser("config", help="inspect configuration")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("check", help="validate configuration and show a summary")

    commands.add_parser("paths", help="show platform directories used by the gateway")
    commands.add_parser("serve", help="run the gateway daemon on loopback")
    commands.add_parser("status", help="show gateway, agents and channels")
    setup_cmd = commands.add_parser(
        "setup", help="create initial config and private tokens; keep existing files"
    )
    setup_cmd.add_argument(
        "--config-dir", type=Path, help="destination (default: platform config dir)"
    )
    service = commands.add_parser(
        "service",
        help="install and manage autostart (Linux/WSL systemd user service)",
        description="Control the systemd user service independently of the gateway API. "
        "enable/disable also start/stop it; start/stop leave autostart unchanged.",
    )
    service_sub = service.add_subparsers(dest="service_command", required=True)
    for action, help_text in (
        ("install", "install/update the unit using the selected config; do not start it"),
        ("enable", "enable autostart and start now"),
        ("disable", "disable autostart and stop now"),
        ("start", "start now without changing autostart"),
        ("stop", "stop now without changing autostart"),
        ("restart", "restart the running service"),
        ("status", "show installed, enabled and running states; works while stopped"),
        ("uninstall", "stop and remove the managed unit; keep configuration and data"),
    ):
        service_sub.add_parser(action, help=help_text)

    dispatcher = commands.add_parser("dispatcher", help="standalone WS ingress diagnostic listener")
    dispatcher.add_argument(
        "--host", default="127.0.0.1", help="bind address; explicit for public ingress"
    )
    dispatcher.add_argument("--port", type=int, default=8766)
    dispatcher.add_argument("--connect-path", default="/connect", help="root or proxy subpath")
    dispatcher.add_argument("--tls-cert", type=Path, help="optional certificate to enable TLS")
    dispatcher.add_argument("--tls-key", type=Path, help="private key paired with --tls-cert")
    connector = commands.add_parser(
        "connector", help="outgoing WS/WSS computer connection and local ACP relay"
    )
    connector.add_argument("--dispatcher-url", required=True, help="ws[s]://host[:port][/path]")
    connector.add_argument("--computer-id", required=True)
    connector.add_argument(
        "--token-file", type=Path, required=True, help="private enrollment credential"
    )
    connector.add_argument(
        "--tls-fingerprint", help="optional WSS certificate pin; otherwise use CA/name validation"
    )

    computers = commands.add_parser(
        "computers", help="local enrollment and credential control (connector foundation)"
    )
    computer_sub = computers.add_subparsers(dest="computer_command", required=True)
    computer_sub.add_parser("list", help="list computers without credentials")
    for action in ("enroll", "rotate", "revoke"):
        cmd = computer_sub.add_parser(action, help=f"{action} a computer credential locally")
        cmd.add_argument("computer_id", help="stable lowercase computer identifier")
        if action == "enroll":
            cmd.add_argument("--name", required=True, help="human-readable computer name")
        if action != "revoke":
            cmd.add_argument(
                "--token-file",
                type=Path,
                required=True,
                help="new private credential file; existing files are refused",
            )

    def conversation_options(cmd):
        cmd.add_argument("--agent", help="agent alias (optional with one configured agent)")
        cmd.add_argument("--thread", default="default", help="conversation name")

    conversation_options(commands.add_parser("sessions", help="list conversation sessions"))
    new = commands.add_parser("new", help="create and activate a session")
    conversation_options(new)
    new.add_argument("--cwd", help="working directory on the agent machine")
    switch = commands.add_parser("switch", help="activate one of the conversation's sessions")
    conversation_options(switch)
    switch.add_argument("session", type=int, help="gateway session row id")
    ask = commands.add_parser("ask", help="send a prompt and stream the agent's answer")
    conversation_options(ask)
    ask.add_argument("text", help="prompt text, or - to read stdin")
    ask.add_argument("--session", type=int, help="prompt a specific gateway session row id")
    ask.add_argument("--no-stream", action="store_true", help="return the running job immediately")
    ask.add_argument("--json", action="store_true", help="print the final job as JSON")
    conversation_options(commands.add_parser("stop", help="cancel conversation jobs"))
    result = commands.add_parser("result", help="retrieve a job after reconnect or restart")
    result.add_argument("job_id")
    result.add_argument("--wait", type=float, default=0, help="wait up to 300 seconds")
    result.add_argument("--json", action="store_true")
    approvals = commands.add_parser("approvals", help="list, watch and decide human approvals")
    approval_sub = approvals.add_subparsers(dest="approval_command")
    approval_sub.add_parser("list", help="list pending approvals")
    approval_sub.add_parser("watch", help="connect a human and decide actions interactively")
    for action in ("approve", "reject"):
        approval_sub.add_parser(action, help=f"{action} a pending request once").add_argument(
            "approval_id"
        )
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
        print(f"  - {agent.address} [{agent.kind}/{agent.backend}] {agent.display_name}")
        if agent.backend == "connector":
            print(f"    route: {agent.address} (computer credentials remain on the computer)")
        else:
            print(f"    url: {agent.url}  tls: {tls}")
        print(f"    secret: {secret}  cwd: {agent.default_cwd}")
        if agent.backend == "remote":
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
    if args.command == "setup" or (args.command == "service" and args.service_command != "install"):
        try:
            if args.command == "setup":
                from acp_gateway.setup import setup

                return setup(args.config_dir)
            from acp_gateway.service import control

            return control(args.service_command)
        except (OSError, ValueError) as exc:
            print(f"error: {redact_text(str(exc))}", file=sys.stderr)
            return 1

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
    try:
        if args.command in {"connector", "dispatcher"}:
            from acp_gateway.connectors.commands import run as run_connector_command

            return run_connector_command(cfg, args)
        if args.command == "computers":
            from acp_gateway.cli.computers import run_computers

            return run_computers(cfg, args)
        if args.command == "service":
            from acp_gateway.service import install

            return install(cfg)
        if args.command == "serve":
            from acp_gateway.daemon import serve

            serve(cfg)
            return 0
        from acp_gateway.cli.client import run

        return run(cfg, args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, EOFError) as exc:
        print(f"error: {redact_text(str(exc))}", file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print("error: the gateway API is unavailable; start acpgw serve", file=sys.stderr)
        return 1
