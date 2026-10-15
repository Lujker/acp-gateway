"""Read-only systemd update plan, resolved in a short-lived installed interpreter."""

import shutil
import sys
from pathlib import Path


def resolve_selection(config, env_file, role=None):
    """Explicit CLI/environment paths win; otherwise use the owned Windows task."""
    import os

    if (
        sys.platform == "win32"
        and config is None
        and env_file is None
        and not os.environ.get("ACPGW_CONFIG")
        and not os.environ.get("ACPGW_ENV_FILE")
    ):
        from acp_gateway.windows_service import installed_selection

        config, env_file = installed_selection()
        role = role or "connector"
    return config, env_file, role


def preflight_activity(service, *, restart_uncertain=False):
    """Observe before handoff; live prepare still reserves admission atomically."""
    if restart_uncertain and service["role"] != "connector":
        raise ValueError("--restart-uncertain is only for connector updates")
    if service["role"] != "connector" or not service["running"]:
        return
    from acp_gateway.connectors.maintenance import can_reserve, read_status

    activity = read_status(Path(service["data_dir"]))
    if activity["state"] != "running":
        raise ValueError(
            "Cannot verify connector activity; inspect maintenance --role connector status"
        )
    if not can_reserve(activity, restart_uncertain=restart_uncertain):
        counts = ", ".join(
            f"{key}={activity.get(key, 0)}"
            for key in (
                "pending_requests",
                "pending_approvals",
                "running_sessions",
                "uncertain_sessions",
                "opening",
                "unsupported_agents",
            )
        )
        raise ValueError(
            "Connector maintenance refused: " + counts + ". "
            "Finish active work first. For uncertain-only state, after verifying no agent turn "
            "is running, explicitly select update --with-services --restart-uncertain."
        )


def inspect_service(config, env_file, role=None):
    from acp_gateway import service
    from acp_gateway.config import load_config
    from acp_gateway.setup_state import STATE, read

    cfg = load_config(Path(config) if config else None, Path(env_file) if env_file else None)
    if cfg.config_path is None or cfg.env_file is None:
        raise ValueError("Service updates require persistent config and environment files")
    if sys.platform == "win32":
        from acp_gateway import windows_service

        if role not in {None, "connector"}:
            raise ValueError("Windows managed update supports the connector task only")
        prefix = windows_service.subprocess.list2cmdline(windows_service._arguments_prefix(cfg))
        task = windows_service._request("maintenance-state", argumentsPrefix=prefix)
        if Path(task["controlDirectory"]).resolve() != cfg.settings.resolved_data_dir().resolve():
            raise ValueError("Connector task uses another data directory")
        return {
            "role": "connector",
            "backend": "windows",
            "config": str(cfg.config_path.resolve()),
            "env_file": str(cfg.env_file.resolve()),
            "arguments_prefix": prefix,
            "definition": task["definition"],
            "control_id": task["controlId"],
            "data_dir": str(cfg.settings.resolved_data_dir()),
            "enabled": task["enabled"],
            "running": task["state"] == 4,
            "instance": task["instance"],
        }
    if role is None:
        saved = read(cfg.config_path) if (cfg.config_path.parent / STATE).exists() else {}
        role = "connector" if saved.get("role") == "connector" else "gateway"
    path = service.unit_path(role)
    if not path.is_file():
        raise ValueError("Selected service is not installed")
    service._owned(path, role)
    text = path.read_text(encoding="utf-8")
    prefix = (
        "ExecStart="
        + " ".join(
            service._quote(value)
            for value in (
                sys.executable,
                "-m",
                "acp_gateway",
                "--config",
                str(cfg.config_path.resolve()),
                "--env-file",
                str(cfg.env_file.resolve()),
            )
        )
        + " "
    )
    if not any(line.startswith(prefix) for line in text.splitlines()):
        raise ValueError("Service belongs to another installation or config/env selection")
    state = dict(
        line.split("=", 1)
        for line in service._systemctl(
            "show", path.name, "--property=ActiveState,UnitFileState,MainPID"
        ).splitlines()
        if "=" in line
    )
    if state.get("ActiveState") not in {"active", "inactive", "failed"} or state.get(
        "UnitFileState"
    ) not in {"enabled", "disabled"}:
        raise ValueError("Service state is transitional or unsupported; inspect it before updating")
    executable = shutil.which("systemctl")
    if executable is None:
        raise ValueError("systemctl is unavailable")
    return {
        "role": role,
        "unit": str(path),
        "definition": text,
        "systemctl": executable,
        "running": state["ActiveState"] == "active",
        "enabled": state["UnitFileState"] == "enabled",
        "pid": state.get("MainPID", "0"),
        "config": str(cfg.config_path.resolve()),
        "env_file": str(cfg.env_file.resolve()),
        "data_dir": str(cfg.settings.resolved_data_dir()),
    }
