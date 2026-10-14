"""Section-oriented setup: opening the menu never writes configuration."""

import sys
from pathlib import Path

import httpx

from acp_gateway import paths
from acp_gateway.cli.wizard import Prompts, _snapshot, _state, check_connections, wizard
from acp_gateway.config import find_config_file, load_config
from acp_gateway.log import redact_text


def setup_menu(
    directory=None, *, config_file=None, env_file=None, role=None, prompts=None, execute=None
):
    if prompts is None and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ValueError("interactive setup requires a terminal")
    from acp_gateway.cli.main import main
    from acp_gateway.cli.menu import agents_menu, computers_menu, service_menu

    prompts = prompts or Prompts()
    execute = execute or main
    if config_file is None and directory is None:
        config_file = find_config_file()
    directory = (
        Path(directory or (config_file.parent if config_file else paths.config_dir()))
        .expanduser()
        .absolute()
    )
    config_file = (config_file or directory / "config.yaml").expanduser().absolute()
    env_file = (env_file or config_file.parent / ".env").expanduser().absolute()

    def run(arguments):
        result = execute(["--config", str(config_file), "--env-file", str(env_file), *arguments])
        if result:
            print(f"Command finished with code {result}; settings can be corrected here.")
        return result

    while True:
        state = _state(_snapshot(config_file.parent / "setup.json"))
        print(f"\nACP Gateway setup · {config_file}")
        print("Role: " + state.get("role", "not configured") + "; each section is independent.")
        print(
            "base: role and network; agents: local profiles or server routes; "
            "connection: outgoing connector; telegram: bot and access; "
            "hermes: gateway MCP integration; computers: private enrollment keys; "
            "service: autostart; policy/rate-limits/retention/logging: runtime settings."
        )
        action = prompts.choice(
            "Setup section",
            (
                "base",
                "gateway",
                "agents",
                "connection",
                "telegram",
                "hermes",
                "computers",
                "agent-tools",
                "service",
                "policy",
                "rate-limits",
                "retention",
                "logging",
                "check",
                "exit",
            ),
            "exit" if config_file.exists() else "base",
        )
        if action == "exit":
            return 0
        try:
            if action != "base" and not config_file.is_file():
                raise ValueError(
                    "Complete base setup first; other sections can be configured later"
                )
            saved_role = state.get("role", role or "local")
            if action == "computers":
                if saved_role != "server":
                    raise ValueError("Computer enrollment belongs on the dispatcher (server role)")
                computers_menu(prompts, run, config_file)
            elif action == "agent-tools":
                agents_menu(prompts, run, config_file, env_file)
            elif action == "service":
                service_menu(prompts, run, config_file)
            elif action == "check":
                run(["config", "check"])
                if prompts.yes("Check connections (no model prompts or messages)?"):
                    check_connections(
                        load_config(config_file, env_file), state or {"role": saved_role}
                    )
            else:
                wizard(
                    directory,
                    config_file=config_file,
                    env_file=env_file,
                    role=role if action == "base" else None,
                    section=action,
                    prompts=prompts,
                )
        except (OSError, ValueError, httpx.HTTPError) as exc:
            print("error: " + redact_text(str(exc)), file=sys.stderr)
        except (KeyboardInterrupt, EOFError):
            print("\nSection cancelled; returning to setup menu.")
