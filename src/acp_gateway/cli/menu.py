"""Terminal management console using the same commands as scripted operations."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from acp_gateway.cli.wizard import (
    Prompts,
    _identifier,
    _required,
    _snapshot,
    _state,
    check_connections,
)
from acp_gateway.config import find_config_file, load_config
from acp_gateway.log import redact_text


def menu(config_file=None, env_file=None, *, prompts=None, execute=None):
    if prompts is None and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ValueError("interactive menu requires a terminal; use acpgw --help for commands")
    from acp_gateway.cli.main import main

    prompts = prompts or Prompts()
    execute = execute or main
    config_file = find_config_file(config_file)
    if config_file is not None:
        config_file = config_file.absolute()
    if env_file is None and config_file is not None:
        env_file = config_file.parent / ".env"

    def run(arguments):
        prefix = []
        if config_file is not None:
            prefix += ["--config", str(config_file)]
        if env_file is not None:
            prefix += ["--env-file", str(env_file)]
        result = execute(prefix + arguments)
        if result:
            print(f"Command finished with code {result}; you can correct settings and retry.")
        return result

    while True:
        print("\nACP Gateway · " + (str(config_file) if config_file else "not configured"))
        action = prompts.choice(
            "Management",
            (
                "setup",
                "settings",
                "doctor",
                "agents",
                "connect",
                "check",
                "connections",
                "status",
                "service",
                "computers",
                "conversation",
                "approvals",
                "updates",
                "exit",
            ),
            "exit",
        )
        if action == "exit":
            return 0
        try:
            if action == "setup":
                run(["setup", "--interactive"])
                config_file = find_config_file(config_file)
                if config_file is not None:
                    config_file = config_file.absolute()
                    env_file = env_file or config_file.parent / ".env"
            elif action == "check":
                run(["config", "check"])
            elif action == "settings":
                section = prompts.choice(
                    "Settings section",
                    (
                        "base",
                        "agents",
                        "gateway",
                        "telegram",
                        "hermes",
                        "connection",
                        "policy",
                        "rate-limits",
                        "retention",
                        "logging",
                        "all",
                    ),
                    "agents",
                )
                run(["config", "edit", "--section", section])
            elif action == "doctor":
                run(
                    ["doctor", "--live"]
                    if prompts.yes("Probe live ACP/API/TLS endpoints?", True)
                    else ["doctor"]
                )
            elif action == "connect":
                run(["connect"])
            elif action == "agents":
                agents_menu(prompts, run, config_file, env_file)
            elif action == "connections":
                cfg = load_config(config_file, env_file)
                state = (
                    _state(_snapshot(cfg.config_path.parent / "setup.json"))
                    if (cfg.config_path is not None)
                    else {}
                )
                check_connections(cfg, state or {"role": "local"})
            elif action == "status":
                run(["status"])
            elif action == "service":
                service_menu(prompts, run, config_file)
            elif action == "computers":
                computers_menu(prompts, run, config_file)
            elif action == "conversation":
                cfg = load_config(config_file, env_file)
                addresses = tuple(a.address for a in cfg.settings.agents)
                if not addresses:
                    raise ValueError("Configure an agent through setup first")
                agent = prompts.choice("Agent", addresses, addresses[0])
                thread = prompts.ask("Conversation", "default", _required)
                operation = prompts.choice(
                    "Conversation action", ("sessions", "new", "ask", "stop"), "sessions"
                )
                args = [operation, "--agent", agent, "--thread", thread]
                if operation == "ask":
                    args.append(prompts.ask("Message to agent", validate=_required))
                elif operation == "stop" and not prompts.yes("Cancel jobs in this conversation?"):
                    continue
                run(args)
            elif action == "approvals":
                operation = prompts.choice("Approvals", ("list", "watch"), "list")
                run(["approvals", operation])
            elif action == "updates":
                run(["update", "--check"])
        except (OSError, ValueError, httpx.HTTPError) as exc:
            print("error: " + redact_text(str(exc)), file=sys.stderr)
        except KeyboardInterrupt:
            print("\nAction cancelled; returning to menu.")


def service_menu(prompts, run, config_file):
    state = _state(_snapshot(config_file.parent / "setup.json")) if config_file else {}
    role = "connector" if state.get("role") == "connector" else "gateway"
    operation = prompts.choice(
        "Service",
        ("status", "install", "start", "stop", "restart", "enable", "disable", "logs", "uninstall"),
        "status",
    )
    if operation in {"stop", "restart", "disable", "uninstall"} and not prompts.yes(
        f"{operation} the {role} service? Active work may be interrupted."
    ):
        return 0
    run(["service", "--role", role, operation])


def computers_menu(prompts, run, config_file):
    operation = prompts.choice(
        "Computers", ("list", "status", "enroll", "rotate", "revoke"), "list"
    )
    args = ["computers", operation]
    if operation in {"enroll", "rotate", "revoke"}:
        identity = prompts.ask("Computer ID", validate=_identifier)
        args.append(identity)
        if operation != "enroll" and not prompts.yes(
            f"{operation} access for {identity}? Its previous key will stop working."
        ):
            return 0
        if operation == "enroll":
            args += ["--name", prompts.ask("Display name", identity, _required)]
        if operation != "revoke":
            from acp_gateway import paths

            directory = config_file.parent if config_file else paths.config_dir()
            default = (
                directory
                / "enrollment"
                / (identity + ("-next" if operation == "rotate" else "") + ".key")
            )
            path = (
                Path(prompts.ask("New private key file (must not exist)", str(default), _required))
                .expanduser()
                .absolute()
            )
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            args += ["--token-file", str(path)]
    result = run(args)
    if operation == "enroll" and not result:
        print(
            "Copy the key privately to the computer and run acpgw setup there. "
            "Add its agent route through setup on this server."
        )


def agents_menu(prompts, run, config_file, env_file):
    operation = prompts.choice(
        "Executor agents",
        ("list", "configure", "install", "provider", "serve", "service"),
        "list",
    )
    args = ["agents", operation]
    if operation == "install":
        args.append("goose")
    elif operation in {"serve", "service"}:
        cfg = load_config(config_file, env_file)
        aliases = tuple(
            a.alias for a in cfg.settings.agents if a.kind == "goose" and a.backend == "remote"
        )
        if not aliases:
            raise ValueError("Prepare a Goose profile with agents configure first")
        alias = prompts.choice("Local Goose", aliases, aliases[0])
        args.append(alias)
        if operation == "service":
            command = prompts.choice(
                "Goose service",
                (
                    "install",
                    "enable",
                    "status",
                    "logs",
                    "start",
                    "stop",
                    "restart",
                    "disable",
                    "uninstall",
                ),
                "status",
            )
            if command in {
                "stop",
                "restart",
                "disable",
                "uninstall",
            } and not prompts.yes(f"{command} Goose {alias}? Active work may be interrupted."):
                return 0
            args.append(command)
    run(args)
