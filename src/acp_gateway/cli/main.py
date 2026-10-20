"""``acpgw`` command-line entry point."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from acp_gateway import __version__, paths
from acp_gateway.cli.setup_sections import SECTIONS as SETUP_SECTIONS
from acp_gateway.log import redact_text

if TYPE_CHECKING:
    from acp_gateway.config import AppConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acpgw",
        description="ACP Gateway: route conversations to ACP agents with human approvals.",
        epilog="Getting started: acpgw setup (interactive) → acpgw start. "
        "Use acpgw setup --non-interactive to create templates for automation. "
        "Use acpgw <command> --help for details.",
    )
    parser.add_argument("--version", action="version", version=f"acpgw {__version__}")
    parser.add_argument("--config", type=Path, help="path to config.yaml")
    parser.add_argument("--env-file", type=Path, help="path to the .env file with secrets")
    commands = parser.add_subparsers(dest="command", required=True)

    version = commands.add_parser("version", help="show version and installation manager")
    version.add_argument("--json", action="store_true")
    update = commands.add_parser("update", help="check releases or update a stopped uv tool")
    target = update.add_mutually_exclusive_group()
    target.add_argument("--check", action="store_true", help="check stable PyPI releases only")
    target.add_argument("--status", action="store_true", help="show the latest maintenance result")
    target.add_argument("--version", help="install an exact PyPI version")
    target.add_argument("--stable", action="store_true", help="install a newer stable PyPI release")
    target.add_argument(
        "--rollback",
        action="store_true",
        help="restore the previous environment and selected database",
    )
    target.add_argument("--from", dest="source", help="install a wheel path/URL or Git URL/ref")
    update.add_argument("--dry-run", action="store_true", help="inspect without changing files")
    update.add_argument(
        "--with-services",
        action="store_true",
        help="reserve and restore a selected systemd service or Windows connector task",
    )
    update.add_argument("--service-role", choices=("gateway", "connector"))
    update.add_argument(
        "--restart-uncertain",
        action="store_true",
        help="owner confirms no agent turn is running; restart an uncertain-only connector",
    )
    update.add_argument("--service-coordinator-fd", type=int, help=argparse.SUPPRESS)
    update.add_argument("--service-coordinator-file", type=Path, help=argparse.SUPPRESS)
    update.add_argument("--handoff-file", type=Path, help=argparse.SUPPRESS)
    update.add_argument("--operation-id", help=argparse.SUPPRESS)
    update.add_argument(
        "--constraints", type=Path, help="release requirements.lock.txt (with --from)"
    )
    uninstall = commands.add_parser(
        "uninstall", help="remove a stopped uv tool and its managed units; keep all user data"
    )
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument("--handoff-file", type=Path, help=argparse.SUPPRESS)

    config_cmd = commands.add_parser("config", help="inspect configuration")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("check", help="validate configuration and show a summary")
    edit = config_sub.add_parser("edit", help="edit settings with the interactive wizard")
    edit.add_argument(
        "--section",
        choices=SETUP_SECTIONS,
        default="all",
    )

    doctor = commands.add_parser("doctor", help="diagnose configuration and suggest repairs")
    doctor.add_argument(
        "--live", action="store_true", help="also probe ACP, API and dispatcher TLS"
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable report")
    doctor.add_argument(
        "--timeout", type=float, default=5, help="per-probe deadline in seconds (0-60)"
    )
    commands.add_parser("connect", help="configure this computer's outgoing dispatcher connection")
    agents = commands.add_parser("agents", help="prepare and manage local executor agents")
    agent_sub = agents.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("list", help="show configured agents without secrets")
    probe = agent_sub.add_parser("probe", help="probe an existing local agent without a model call")
    probe.add_argument("kind", choices=("hermes", "goose"))
    probe.add_argument("--url", help="Goose: explicit numeric-loopback ACP URL")
    probe.add_argument("--secret-env", help="Goose: environment variable holding the backend key")
    probe.add_argument("--tls-fingerprint", help="Goose: required SHA-256 pin for TLS")
    probe.add_argument(
        "--lock-dir", type=Path, help="Hermes rendezvous directory in this OS environment"
    )
    probe.add_argument("--role", choices=("desktop-serve", "serve"))
    probe.add_argument(
        "--timeout", type=float, default=5, help="per-request deadline, at most 60 seconds"
    )
    probe.add_argument("--json", action="store_true")
    agent_sub.add_parser("configure", help="edit agent profiles interactively")
    install_agent = agent_sub.add_parser(
        "install", help="install Goose using its official installer"
    )
    install_agent.add_argument("kind", choices=("goose",))
    install_agent.add_argument("--version", help="pin the Goose release")
    install_agent.add_argument("--dry-run", action="store_true", help="show installation plan")
    install_agent.add_argument(
        "--yes", action="store_true", help="accept installation without prompting"
    )
    agent_sub.add_parser("provider", help="run Goose provider/model configuration")
    agent_serve = agent_sub.add_parser("serve", help="run a configured local Goose listener")
    agent_serve.add_argument("alias")
    agent_service = agent_sub.add_parser(
        "service", help="manage a local Goose systemd user service"
    )
    agent_service.add_argument("alias")
    agent_service.add_argument(
        "action",
        choices=(
            "install",
            "start",
            "stop",
            "restart",
            "enable",
            "disable",
            "status",
            "logs",
            "uninstall",
        ),
    )

    commands.add_parser("paths", help="show platform directories used by the gateway")
    commands.add_parser("serve", help="run the gateway daemon on loopback")
    commands.add_parser("start", help="run the role saved by interactive setup")
    updater = commands.add_parser("updater", help="run an independent, locally configured updater")
    updater.add_argument("--policy", type=Path, required=True)
    updater.add_argument(
        "--init", action="store_true", help="write a uv companion policy without starting it"
    )
    updater.add_argument("--role", choices=("dispatcher", "connector"), default="connector")
    updater.add_argument(
        "updater_action",
        nargs="?",
        default="run",
        choices=(
            "run",
            "install",
            "enable",
            "disable",
            "start",
            "stop",
            "restart",
            "status",
            "uninstall",
            "diagnose",
            "resolve",
            "allow-image",
            "stage-runtime",
        ),
        help="manage independent updater autostart; install does not start it",
    )
    updater.add_argument("--operation", help="unknown operation ID to resolve locally")
    updater.add_argument("--outcome", choices=("succeeded", "failed"))
    updater.add_argument("--note", help="local recovery audit note")
    updater.add_argument(
        "--acknowledge-idle",
        action="store_true",
        help="confirm installers/rollback have finished after local inspection",
    )
    updater.add_argument("--version", help="reviewed stable version for allow-image")
    updater.add_argument("--image", help="reviewed immutable local Docker image")
    commands.add_parser("menu", help="interactive configuration and management console")
    commands.add_parser("status", help="show gateway, agents and channels")
    local_sessions = commands.add_parser(
        "connector-sessions", help="inspect or retire local Hermes attachments without VPS access"
    )
    local_sub = local_sessions.add_subparsers(dest="session_command", required=True)
    for action in ("list", "journal", "retire"):
        cmd = local_sub.add_parser(action)
        cmd.add_argument("--agent", required=True, help="local connector agent alias")
        if action != "list":
            cmd.add_argument("handle", help="opaque local handle from connector-sessions list")
    maintenance_mode = commands.add_parser(
        "maintenance", help="reserve an idle dispatcher before planned maintenance"
    )
    maintenance_mode.add_argument("--role", choices=("gateway", "connector"), default="gateway")
    maintenance_sub = maintenance_mode.add_subparsers(dest="maintenance_command", required=True)
    maintenance_sub.add_parser("status", help="show admission and active work counts")
    for action in ("prepare", "resume"):
        maintenance_action = maintenance_sub.add_parser(action)
        maintenance_action.add_argument("--reservation-file", type=Path)
        if action == "prepare":
            maintenance_action.add_argument(
                "--restart-uncertain",
                action="store_true",
                help="owner acknowledges uncertainty after verifying no agent turn is running",
            )
        if action == "resume":
            maintenance_action.add_argument(
                "--force", action="store_true", help="owner recovery for a lost reservation token"
            )
    setup_cmd = commands.add_parser(
        "setup", help="configure interactively in a terminal; create templates otherwise"
    )
    setup_cmd.add_argument(
        "--config-dir", type=Path, help="destination (default: platform config dir)"
    )
    setup_cmd.add_argument(
        "--role",
        choices=("local", "server", "connector"),
        help="preselect role in the interactive wizard",
    )
    setup_cmd.add_argument(
        "--section",
        choices=SETUP_SECTIONS,
        help="configure one section directly; otherwise open the setup menu",
    )
    setup_mode = setup_cmd.add_mutually_exclusive_group()
    setup_mode.add_argument("--interactive", action="store_true", help="run the setup wizard")
    setup_mode.add_argument(
        "--non-interactive", action="store_true", help="create templates without questions"
    )
    service = commands.add_parser(
        "service",
        help="manage autostart (systemd user service or Windows connector task)",
        description="Control the user service independently of the gateway API. "
        "enable/disable also start/stop it; start/stop leave autostart unchanged.",
    )
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service.add_argument("--role", choices=("gateway", "connector"), default="gateway")
    for action, help_text in (
        ("install", "install/update the unit using the selected config; do not start it"),
        ("enable", "enable autostart and start now"),
        ("disable", "disable autostart and stop now"),
        ("start", "start now without changing autostart"),
        ("stop", "stop now without changing autostart"),
        ("restart", "restart the running service"),
        ("status", "show installed, enabled and running states; works while stopped"),
        ("logs", "show the last 50 journal entries for the selected service"),
        ("uninstall", "stop and remove the managed unit; keep configuration and data"),
    ):
        sub = service_sub.add_parser(action, help=help_text)
        if action in {"stop", "disable", "restart", "uninstall"}:
            sub.add_argument(
                "--force",
                action="store_true",
                help="Windows connector only: terminate the task instead of graceful stop",
            )
        if action == "install":
            sub.add_argument(
                "--dispatcher-url", help="connector dispatcher ws[s]://host[:port][/path]"
            )
            sub.add_argument("--computer-id", help="connector computer identity")
            sub.add_argument("--token-file", type=Path, help="private connector enrollment file")
            sub.add_argument("--tls-fingerprint", help="optional connector WSS certificate pin")

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
    connector.add_argument("--control-id", help=argparse.SUPPRESS)
    connector.add_argument("--dispatcher-url", required=True, help="ws[s]://host[:port][/path]")
    connector.add_argument("--computer-id", required=True)
    connector.add_argument(
        "--token-file", type=Path, required=True, help="private enrollment credential"
    )
    connector.add_argument(
        "--tls-fingerprint", help="optional WSS certificate pin; otherwise use CA/name validation"
    )

    owner = commands.add_parser("owner", help="trusted local single-owner profile")
    owner_sub = owner.add_subparsers(dest="owner_command", required=True)
    owner_sub.add_parser("show")
    bootstrap = owner_sub.add_parser(
        "bootstrap", help="explicitly select Telegram approvers; empty means local only"
    )
    bootstrap.add_argument("--telegram-id", type=int, action="append", default=[])
    pairing = commands.add_parser(
        "pairing", help="compare and resolve connector enrollment locally"
    )
    pair_sub = pairing.add_subparsers(dest="pairing_command", required=True)
    pair_sub.add_parser("list")
    for action in ("approve", "deny"):
        decision = pair_sub.add_parser(action)
        decision.add_argument("request_id")
        decision.add_argument(
            "--verification", required=True, help="exact comparison value displayed on applicant PC"
        )
    activate = pair_sub.add_parser(
        "activate-legacy-rebind",
        help="one-time forward-only credential fencing after local upgrades",
    )
    activate.add_argument(
        "--confirm",
        action="store_true",
        help="confirm backup and local upgrade path; old software/DB rollback prohibited",
    )

    computers = commands.add_parser(
        "computers", help="local enrollment and credential control (connector foundation)"
    )
    computer_sub = computers.add_subparsers(dest="computer_command", required=True)
    computer_sub.add_parser("list", help="list computers without credentials")
    computer_sub.add_parser("status", help="live computer and route diagnostics from owner API")
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

    models = commands.add_parser("model", help="inspect or select the active dialogue model")
    model_sub = models.add_subparsers(dest="model_command", required=True)
    for action in ("show", "list", "state"):
        cmd = model_sub.add_parser(action)
        conversation_options(cmd)
        cmd.add_argument("--session", type=int, required=True, help="owned gateway dialogue row ID")
    cmd = model_sub.add_parser("reconcile", help="inspect the exact retained outcome without retry")
    conversation_options(cmd)
    cmd.add_argument("--session", type=int, required=True)
    cmd.add_argument("--revision", type=int, required=True)
    cmd = model_sub.add_parser("set", help="explicit session-scoped provider/model selection")
    conversation_options(cmd)
    cmd.add_argument("provider")
    cmd.add_argument("model")
    cmd.add_argument("--session", type=int, required=True)
    cmd.add_argument("--revision", type=int, required=True, help="active_revision from sessions")
    cmd.add_argument("--confirmation-token", help="single-use token from confirmation_required")
    defaults = model_sub.add_parser("default", help="default for new dispatcher dialogues only")
    default_sub = defaults.add_subparsers(dest="default_command", required=True)
    for action in ("show", "clear", "set"):
        cmd = default_sub.add_parser(action)
        conversation_options(cmd)
        if action == "set":
            cmd.add_argument("provider")
            cmd.add_argument("model")

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

    print(f"agents:      {len(s.agents)} configured")
    if s.connector.enabled:
        print("remote agents: discovered from enrolled connectors; acpgw computers status")
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
        elif agent.backend == "hermes":
            print(f"    attach: local Hermes/{agent.hermes.profile}; dedicated conversations")
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
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        argv = ["menu"]
    args = _build_parser().parse_args(argv)
    if args.command == "updater":
        from acp_gateway.update_runner import init_policy, start_updater

        try:
            if (
                any((args.operation, args.outcome, args.note, args.acknowledge_idle))
                and args.updater_action != "resolve"
            ):
                raise ValueError("Recovery options require updater resolve")
            if any((args.version, args.image)) and args.updater_action != "allow-image":
                raise ValueError("Image options require updater allow-image")
            if args.init:
                if args.updater_action != "run":
                    raise ValueError("--init cannot be combined with an updater service action")
                return init_policy(args.policy, args.config, args.env_file, role=args.role)
            if args.updater_action == "stage-runtime":
                # Called by the independent worker itself: no config, services or
                # package installation may be touched on this path.
                from acp_gateway.update_bootstrap import stage_runtime

                stage_runtime(args.policy)
                return 0
            if args.updater_action in {"diagnose", "resolve", "allow-image"}:
                from acp_gateway.updater_recovery import allow_image, diagnose, resolve

                if args.updater_action == "diagnose":
                    return diagnose(args.policy)
                if args.updater_action == "allow-image":
                    return allow_image(args.policy, args.version, args.image)
                if args.operation is None:
                    raise ValueError("resolve requires --operation")
                return resolve(
                    args.policy,
                    args.operation,
                    args.outcome,
                    args.note,
                    acknowledge_idle=args.acknowledge_idle,
                )
            if args.updater_action != "run":
                from acp_gateway.updater_service import manage

                return manage(args.updater_action, args.policy)
            return start_updater(args.policy)
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
            print(f"error: {redact_text(str(exc))}", file=sys.stderr)
            return 1
    if args.command in {"version", "update", "uninstall"}:
        try:
            from acp_gateway.installation import maintenance

            return maintenance(args)
        except (OSError, ValueError, httpx.HTTPError) as exc:
            print(f"error: {redact_text(str(exc))}", file=sys.stderr)
            return 1
    if args.command in {"serve", "connector", "dispatcher"} or (
        args.command == "agents" and args.agent_command == "serve"
    ):
        try:
            from acp_gateway.installation import runtime_lease

            with runtime_lease():
                return _run_configured(args)
        except (OSError, ValueError) as exc:
            print(f"error: {redact_text(str(exc))}", file=sys.stderr)
            return 78 if args.command == "connector" else 1
    return _run_configured(args)


def _run_configured(args) -> int:
    from pydantic import ValidationError

    from acp_gateway.config import load_config

    if args.command == "agents" and args.agent_command == "probe":
        from acp_gateway.cli.probe import run

        try:
            return run(args)
        except KeyboardInterrupt:
            return 130
    if args.command == "doctor":
        from acp_gateway.cli.doctor import doctor

        return doctor(
            args.config, args.env_file, live=args.live, as_json=args.json, timeout=args.timeout
        )
    if args.command == "agents" and args.agent_command in {"install", "provider"}:
        from acp_gateway.cli.executors import run_bootstrap

        try:
            return run_bootstrap(args)
        except (OSError, ValueError, httpx.HTTPError) as exc:
            print(f"error: {redact_text(str(exc))}", file=sys.stderr)
            return 1
        except (KeyboardInterrupt, EOFError):
            return 130

    if args.command == "paths":
        return _cmd_paths()
    if (
        args.command in {"setup", "start", "menu", "connect"}
        or (args.command == "agents" and args.agent_command == "configure")
        or (
            args.command == "agents"
            and args.agent_command == "service"
            and args.action != "install"
        )
        or (args.command == "config" and args.config_command == "edit")
        or (args.command == "service" and args.service_command != "install")
    ):
        try:
            if args.command == "agents" and args.agent_command == "service":
                from acp_gateway.cli.executors import service

                return service(None, args.alias, args.action, config_file=args.config)
            if args.command == "connect" or (
                args.command == "agents" and args.agent_command == "configure"
            ):
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise ValueError("interactive configuration requires a terminal")
                from acp_gateway.cli.wizard import wizard
                from acp_gateway.config import find_config_file

                selected = find_config_file(args.config)
                fresh = args.command == "connect" and (selected is None or not selected.is_file())
                return wizard(
                    config_file=selected,
                    env_file=args.env_file,
                    section="all"
                    if fresh
                    else "connection"
                    if args.command == "connect"
                    else "agents",
                    **({"role": "connector"} if fresh else {}),
                )
            if args.command == "menu":
                from acp_gateway.cli.menu import menu

                return menu(args.config, args.env_file)
            if args.command == "config":
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise ValueError("interactive configuration requires a terminal")
                from acp_gateway.cli.wizard import wizard
                from acp_gateway.config import find_config_file

                return wizard(
                    config_file=find_config_file(args.config),
                    env_file=args.env_file,
                    section=args.section,
                )
            if args.command == "start":
                from acp_gateway.cli.wizard import start

                return start(args.config, args.env_file)
            if args.command == "setup":
                interactive = args.interactive or (
                    not args.non_interactive and sys.stdin.isatty() and sys.stdout.isatty()
                )
                if interactive:
                    if not sys.stdin.isatty() or not sys.stdout.isatty():
                        raise ValueError("interactive setup requires a terminal")
                    if args.section:
                        if args.role and args.section not in {"base", "all"}:
                            raise ValueError("--role belongs to --section base")
                        from acp_gateway.cli.wizard import wizard

                        return wizard(
                            args.config_dir,
                            config_file=args.config,
                            env_file=args.env_file,
                            role=args.role,
                            section=args.section,
                        )
                    from acp_gateway.cli.setup_menu import setup_menu

                    return setup_menu(
                        args.config_dir,
                        config_file=args.config,
                        env_file=args.env_file,
                        role=args.role,
                    )
                if args.role or args.section:
                    raise ValueError("--role/--section requires interactive setup")
                from acp_gateway.setup import setup

                return setup(args.config_dir)
            from acp_gateway.service import control

            options = {"force": True} if getattr(args, "force", False) else {}
            return control(args.service_command, role=args.role, **options)
        except (OSError, ValueError) as exc:
            print(f"error: {redact_text(str(exc))}", file=sys.stderr)
            return 1
        except (KeyboardInterrupt, EOFError):
            print("\nInteractive command cancelled.", file=sys.stderr)
            return 130

    try:
        if args.command == "service" and args.env_file is None:
            from acp_gateway.config import ENV_FILE_ENV, find_config_file

            selected_config = find_config_file(args.config)
            if not os.environ.get(ENV_FILE_ENV) and selected_config is not None:
                from acp_gateway.setup_state import read

                saved = read(selected_config)
                saved_env = Path(saved.get("env_file", selected_config.parent / ".env"))
                if "env_file" in saved or saved_env.is_file():
                    args.env_file = saved_env
        cfg = load_config(args.config, args.env_file)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"error: invalid configuration\n{exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    if args.command == "config" and args.config_command == "check":
        return _cmd_config_check(cfg)
    try:
        if args.command in {"owner", "pairing"}:
            from acp_gateway.cli.pairing import run

            return run(cfg, args)
        if args.command == "connector-sessions":
            from acp_gateway.cli.connector_maintenance import sessions

            return sessions(cfg, args)
        if args.command == "maintenance" and args.role == "connector":
            from acp_gateway.cli.connector_maintenance import run

            return run(cfg, args)
        if args.command == "agents":
            from acp_gateway.cli.executors import run

            return run(cfg, args)
        if args.command in {"connector", "dispatcher"}:
            from acp_gateway.connectors.commands import run as run_connector_command

            return run_connector_command(cfg, args)
        if args.command == "computers":
            if args.computer_command == "status":
                from acp_gateway.cli.client import run

                return run(cfg, args)
            from acp_gateway.cli.computers import run_computers

            return run_computers(cfg, args)
        if args.command == "service":
            from acp_gateway.service import install

            launch = None
            if args.role == "connector":
                from acp_gateway.connectors.launch import ConnectorLaunch

                explicit = any(
                    (args.dispatcher_url, args.computer_id, args.token_file, args.tls_fingerprint)
                )
                launch = (
                    ConnectorLaunch.from_args(args, cfg)
                    if explicit
                    else ConnectorLaunch.from_saved(cfg)
                )
            elif any(
                (args.dispatcher_url, args.computer_id, args.token_file, args.tls_fingerprint)
            ):
                raise ValueError("connector launch options require service --role connector")
            return install(cfg, launch=launch)
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
        return 78 if args.command == "connector" else 1
    except httpx.HTTPError:
        print("error: the gateway API is unavailable; start acpgw serve", file=sys.stderr)
        return 1
