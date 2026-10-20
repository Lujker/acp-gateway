"""Refresh connection arguments with the existing maintenance and service adapters.

No package installation, agent setup or new service is hidden in a connection edit.
"""

import sys
from pathlib import Path
from types import SimpleNamespace


def apply_connection_settings(cfg, launch, files, originals, directory, prompts):
    from acp_gateway import service, service_update
    from acp_gateway.cli.connector_maintenance import run as maintenance
    from acp_gateway.cli.wizard import _atomic, _snapshot, save_files

    windows = sys.platform == "win32"
    if windows:
        from acp_gateway import windows_service

        installed = windows_service._request("status")["installed"]
    else:
        installed = sys.platform == "linux" and service.unit_path("connector").is_file()
    if not installed:
        return save_files(files, originals, directory), False
    if launch is None:
        raise ValueError("configure a local agent before refreshing the connector service")
    selected = service_update.inspect_service(cfg.config_path, cfg.env_file, role="connector")
    service_update.preflight_activity(
        selected
    )  # Busy/uncertain is refused; never an implicit cancel.
    print(
        "The owned connector service arguments will be refreshed. Active attachments may be "
        "lost; history/results are kept. Running/enabled state will be restored."
    )
    if not prompts.yes("Refresh the existing connector service with these settings?", True):
        raise ValueError("Connection settings not saved; existing service kept unchanged")
    reservation = cfg.settings.resolved_data_dir() / "pairing-maintenance.json"
    reserved = stopped = False
    task = None
    backup = None

    def admission(action):
        return maintenance(
            cfg,
            SimpleNamespace(
                maintenance_command=action,
                role="connector",
                reservation_file=reservation,
                force=False,
                restart_uncertain=False,
            ),
        )

    def restore_runtime():
        if windows:
            expected = task.control_id if task else selected["control_id"]
            if selected["running"]:
                windows_service._request(
                    "maintenance-flags", expectedControlId=expected, enabled=True
                )
                windows_service._request("start", expectedControlId=expected)
            windows_service._request(
                "maintenance-flags", expectedControlId=expected, enabled=selected["enabled"]
            )
        elif selected["running"]:
            service.control("start", role="connector")

    try:
        if selected["running"]:
            admission("prepare")
            reserved = True
        current = service_update.inspect_service(cfg.config_path, cfg.env_file, role="connector")
        if current != selected:
            raise ValueError("connector service changed during setup; refresh refused")
        if windows:
            windows_service._request(
                "disable",
                expectedControlId=selected["control_id"],
                expectedDefinition=selected["definition"],
                argumentsPrefix=selected["arguments_prefix"],
            )
            stopped = True
            task = windows_service.SetupTask(cfg)
        else:
            service.control("stop", role="connector")
            stopped = True
        backup = save_files(files, originals, directory)
        if windows:
            service.install(cfg, launch=launch, windows_setup=task)
        else:
            service.install(cfg, launch=launch)
        restore_runtime()
        reservation.unlink(missing_ok=True)
        return backup, True
    except BaseException:
        # Restore selected settings before any restart; never overwrite a concurrent edit.
        try:
            if stopped:
                for path, contents in files.items():
                    current = _snapshot(path)
                    if current not in (contents, originals[path]):
                        raise ValueError(
                            "settings changed concurrently; automatic rollback refused"
                        )
                if windows and task is not None:
                    task.restore()
                    task = None
                elif not windows:
                    _atomic(Path(selected["unit"]), selected["definition"].encode())
                    service._systemctl("daemon-reload")
                for path, original in originals.items():
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic(path, original)
                restore_runtime()
                reservation.unlink(missing_ok=True)
            elif reserved:
                admission("resume")
        except BaseException as recovery:
            raise ValueError(
                "connection/service rollback incomplete; inspect selected service and setup "
                "backups before retrying"
            ) from recovery
        raise
