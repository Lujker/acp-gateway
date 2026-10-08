"""Linux/systemd user-service control, independent of the daemon's HTTP API."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

from acp_gateway.config import AppConfig
from acp_gateway.daemon import owner_token

UNIT = "acp-gateway.service"
MARKER = "# Managed by acpgw service install.\n"


def unit_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not root.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be an absolute path")
    return root / "systemd" / "user" / UNIT


def _quote(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("service paths cannot contain control characters")
    # systemd specifiers and environment expansion apply even inside double quotes.
    value = value.replace("$", "$$")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_unit(config: Path, env_file: Path | None) -> str:
    config = config.resolve()
    directory = str(config.parent)
    _quote(directory)  # reject control characters before writing a unit
    if directory.endswith(" "):
        raise ValueError("service configuration directory cannot end in whitespace")
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command += ["-m", "acp_gateway"]
    command += ["--config", str(config)]
    if env_file is not None:
        command += ["--env-file", str(env_file.resolve())]
    command += ["serve"]
    return (
        MARKER
        + "[Unit]\nDescription=ACP Gateway\n\n"
        + "[Service]\nType=simple\n"
        + "ExecStart="
        + " ".join(_quote(value) for value in command)
        + "\n"
        + "WorkingDirectory="
        + directory.replace("%", "%%")
        + "\n"
        + "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=20\nUMask=0077\n\n"
        + "[Install]\nWantedBy=default.target\n"
    )


def _systemctl(*args: str, allow_not_found=False) -> str:
    if sys.platform != "linux":
        raise ValueError("service control currently supports Linux/WSL with systemd only")
    executable = shutil.which("systemctl")
    if executable is None:
        raise ValueError("systemctl is unavailable; enable systemd or use acpgw serve")
    try:
        result = subprocess.run(  # noqa: S603 (fixed executable and argument list, no shell)
            [executable, "--user", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("systemctl timed out; check your systemd user manager") from exc
    if result.returncode and not (allow_not_found and "LoadState=not-found" in result.stdout):
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(
            "systemd user service operation failed: "
            + (detail or "unknown error")
            + "; check systemctl --user status and docs/setup/service.md"
        )
    return result.stdout


def _owned(path: Path) -> None:
    if path.is_symlink() or not path.read_text(encoding="utf-8").startswith(MARKER):
        raise ValueError(f"refusing to replace an unmanaged unit: {path}")


def install(config: AppConfig) -> int:
    if sys.platform != "linux":
        raise ValueError("service install currently supports Linux/WSL with systemd only")
    if config.config_path is None:
        raise ValueError("a persistent config file is required; run acpgw setup first")
    if config.env_file is None:
        raise ValueError("a persistent .env file is required for unattended startup")
    values = dotenv_values(config.env_file, interpolate=False)
    required = [config.settings.gateway.api_token_env]
    if config.secrets.get(config.settings.gateway.mcp_token_env) is not None:
        required.append(config.settings.gateway.mcp_token_env)
    required += [p.secret_env for p in config.settings.agents if p.secret_env]
    if config.settings.telegram.enabled:
        required.append(config.settings.telegram.token_env)
    for name in required:
        secret = config.secrets.get(name)
        if not values.get(name) or secret is None or secret.get_secret_value() != values[name]:
            raise ValueError(f"persist {name} in the selected .env file before service install")
    if config.settings.data_dir is not None and not config.settings.data_dir.is_absolute():
        raise ValueError("service installation requires an absolute data_dir in config.yaml")
    if config.settings.logging.file is not None and not config.settings.logging.file.is_absolute():
        raise ValueError("service installation requires an absolute logging.file in config.yaml")
    owner_token(config)
    missing = config.missing_agent_secrets()
    if missing:
        raise ValueError("missing agent secrets: " + ", ".join(missing))
    # Resolve before writing: start must work outside the current shell/repository.
    content = render_unit(config.config_path, config.env_file)
    path = unit_path()
    if path.exists() or path.is_symlink():
        _owned(path)
    # Ensure a working user manager before making persistent changes.
    _systemctl("show-environment")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temp = Path(stream.name)
            stream.write(content)
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    print(f"Installed: {path}")
    print("Enable and start: acpgw service enable")
    return 0


def control(action: str) -> int:
    if sys.platform != "linux":
        raise ValueError("service control currently supports Linux/WSL with systemd only")
    if action == "status":
        print(
            _systemctl(
                "show",
                UNIT,
                "--no-pager",
                "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID",
                allow_not_found=True,
            ).strip()
        )
        print(f"Unit file: {unit_path()}")
        return 0
    if action == "uninstall":
        path = unit_path()
        if not path.exists() and not path.is_symlink():
            print("Service is not installed.")
            return 0
        _owned(path)
        _systemctl("disable", "--now", UNIT)
        path.unlink()
        _systemctl("daemon-reload")
        print("Service uninstalled; configuration, secrets and data were kept.")
        return 0
    commands = {
        "enable": ("enable", "--now", UNIT),
        "disable": ("disable", "--now", UNIT),
        "start": ("start", UNIT),
        "stop": ("stop", UNIT),
        "restart": ("restart", UNIT),
    }
    _systemctl(*commands[action])
    print(f"Service: {action} completed. Inspect with acpgw service status.")
    return 0
