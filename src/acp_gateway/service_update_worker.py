"""Stdlib coordinator outside uv's replaced environment; Linux/systemd only."""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def invoke(command, *, capture=False, timeout=300, pass_fds=()):
    result = subprocess.run(  # noqa: S603 — fixed executables and explicit argument lists
        command,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=timeout,
        check=False,
        pass_fds=pass_fds,
    )
    if result.returncode:
        raise ValueError(f"Service update subprocess failed (exit {result.returncode})")
    return result.stdout if capture else ""


def state(service):
    output = invoke(
        [
            service["systemctl"],
            "--user",
            "show",
            Path(service["unit"]).name,
            "--property=ActiveState,UnitFileState,MainPID",
        ],
        capture=True,
        timeout=20,
    )
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def check_definition(service):
    path = Path(service["unit"])
    if path.is_symlink() or path.read_text(encoding="utf-8") != service["definition"]:
        raise ValueError("Service definition changed; automatic maintenance refused")


def control(service, action):
    check_definition(service)
    invoke([service["systemctl"], "--user", action, Path(service["unit"]).name], timeout=30)


def cli(plan, service, *arguments, capture=False):
    return invoke(
        [
            plan["entry"],
            "--config",
            service["config"],
            "--env-file",
            service["env_file"],
            *arguments,
        ],
        capture=capture,
        timeout=30,
    )


def admission(plan, service, action):
    arguments = ["maintenance"]
    if service["role"] == "connector":
        arguments += ["--role", "connector"]
    arguments += [action, "--reservation-file", plan["reservation"]]
    cli(plan, service, *arguments)


def restore_state(plan, service):
    check_definition(service)
    current = state(service)
    active = current.get("ActiveState") == "active"
    if current.get("ActiveState") not in {"active", "inactive", "failed"}:
        raise ValueError("Service did not settle; inspect status and the reservation file")
    if active:
        if not service["running"] or current.get("MainPID") != service["pid"]:
            raise ValueError("A different process started during maintenance; recovery refused")
        if Path(plan["reservation"]).exists():
            admission(plan, service, "resume")
    else:
        # The old process has exited; its process-local admission token is stale.
        Path(plan["reservation"]).unlink(missing_ok=True)
    control(service, "enable" if service["enabled"] else "disable")
    if service["running"] and not active:
        control(service, "start")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = state(service)
            if current.get("ActiveState") == "active":
                try:
                    if service["role"] == "gateway":
                        cli(plan, service, "status", capture=True)
                    else:
                        status = json.loads(
                            cli(
                                plan,
                                service,
                                "maintenance",
                                "--role",
                                "connector",
                                "status",
                                capture=True,
                            )
                        )
                        if status.get("state") != "running" or status.get("reserved"):
                            raise ValueError("Connector has not become ready")
                    return
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            time.sleep(0.2)
        raise ValueError("Updated service did not become healthy; inspect status and logs")


def record(plan, status, phase, code=None):
    result = Path(plan["result"])
    temporary = result.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"operation": "service-update", "status": status, "phase": phase, "returncode": code}
        ),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(result)


def execute(plan):
    probe = (
        "import json,sys; from acp_gateway.service_update import inspect_service; "
        "print(json.dumps(inspect_service(*json.loads(sys.argv[1]))))"
    )
    arguments = [plan["python"], "-c", probe, json.dumps(plan["selection"])]
    if "windows_script" in plan:
        probe = (
            "import json,sys; from acp_gateway import windows_service; "
            "windows_service.SCRIPT=json.loads(sys.argv[2]); "
            "from acp_gateway.service_update import inspect_service; "
            "print(json.dumps(inspect_service(*json.loads(sys.argv[1]))))"
        )
        arguments = [
            plan["python"],
            "-c",
            probe,
            json.dumps(plan["selection"]),
            json.dumps(plan["windows_script"]),
        ]
    service = json.loads(invoke(arguments, capture=True, timeout=30))
    if service.get("backend") == "windows":
        return execute_windows(plan, service)
    record(plan, "running", "reserve")
    touched = False
    reserved = False
    try:
        check_definition(service)
        if service["running"]:
            admission(plan, service, "prepare")
            reserved = True
        current = state(service)
        if current.get("MainPID", "0") != service["pid"]:
            raise ValueError("Service process changed before stop; maintenance refused")
        record(plan, "running", "stop")
        touched = True
        control(service, "disable")
        control(service, "stop")
        stopped = state(service)
        if (
            stopped.get("ActiveState") not in {"inactive", "failed"}
            or stopped.get("MainPID", "0") != "0"
        ):
            raise ValueError("Service did not stop; package update refused")
        check_definition(service)
        record(plan, "running", "update")
        invoke(
            [
                plan["entry"],
                "--config",
                service["config"],
                "--env-file",
                service["env_file"],
                *plan["arguments"],
            ],
            pass_fds=(plan["coordinator_fd"],),
        )
    finally:
        if touched:
            record(plan, "running", "restore-services")
            restore_state(plan, service)
        elif reserved:
            admission(plan, service, "resume")


def windows_request(plan, service, action, **values):
    if __package__:
        from acp_gateway.powershell import encoded_script
    else:
        from ps_support import encoded_script
    executable = shutil.which("powershell.exe")
    if not executable:
        raise ValueError("Windows PowerShell unavailable")
    payload = {
        "action": action,
        "executable": plan["python"],
        "expectedControlId": service["control_id"],
        "expectedDefinition": service["definition"],
        "argumentsPrefix": service["arguments_prefix"],
        **values,
    }
    result = subprocess.run(  # noqa: S603 — fixed script, dynamic values only in JSON stdin
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded_script(plan["windows_script"]),
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError("Task Scheduler service update operation failed; inspect task status")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or data.get("installed") is not True:
        raise ValueError("Task Scheduler did not return an owned task")
    return data


def restore_windows(plan, service):
    current = windows_request(plan, service, "maintenance-state")
    if current["state"] not in {1, 3, 4}:
        raise ValueError("Task did not settle; recovery refused")
    active = current["state"] == 4
    if active:
        if not service["running"] or current["instance"] != service["instance"]:
            raise ValueError("Another connector instance started; recovery refused")
        if Path(plan["reservation"]).exists():
            admission(plan, service, "resume")
    else:
        Path(plan["reservation"]).unlink(missing_ok=True)
    if service["running"] and not active:
        try:
            windows_request(plan, service, "maintenance-flags", enabled=True)
            windows_request(plan, service, "start")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    current = windows_request(plan, service, "maintenance-state")
                    status = json.loads(
                        cli(
                            plan,
                            service,
                            "maintenance",
                            "--role",
                            "connector",
                            "status",
                            capture=True,
                        )
                    )
                    if (
                        current["state"] == 4
                        and status.get("state") == "running"
                        and not status.get("reserved")
                        and current["instance"] == status.get("instance")
                    ):
                        return
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                time.sleep(0.2)
            raise ValueError("Updated connector did not become ready")
        finally:
            windows_request(plan, service, "maintenance-flags", enabled=service["enabled"])
    else:
        windows_request(plan, service, "maintenance-flags", enabled=service["enabled"])


def execute_windows(plan, service):
    record(plan, "running", "reserve")
    touched = reserved = False
    try:
        if service["running"]:
            admission(plan, service, "prepare")
            reserved = True
        current = windows_request(plan, service, "maintenance-state")
        if current["instance"] != service["instance"]:
            raise ValueError("Connector instance changed before stop")
        record(plan, "running", "stop")
        touched = True
        windows_request(plan, service, "disable")
        stopped = windows_request(plan, service, "maintenance-state")
        if stopped["state"] not in {1, 3} or stopped["enabled"]:
            raise ValueError("Task did not stop; update refused")
        record(plan, "running", "update")
        handoff = str(Path(plan["reservation"]).parent / "package-handoff.json")
        invoke(
            [
                plan["entry"],
                "--config",
                service["config"],
                "--env-file",
                service["env_file"],
                *plan["arguments"],
                "--handoff-file",
                handoff,
            ]
        )
        details = json.loads(Path(handoff).read_text(encoding="utf-8"))
        if set(details) != {"python", "worker", "manifest"} or any(
            not isinstance(value, str) or not Path(value).is_absolute()
            for value in details.values()
        ):
            raise ValueError("Invalid package helper handoff")
        invoke([details["python"], details["worker"], details["manifest"]])
    finally:
        if touched:
            record(plan, "running", "restore-services")
            restore_windows(plan, service)
        elif reserved:
            admission(plan, service, "resume")


def wait_parent(pid):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if handle:
        try:
            if kernel.WaitForSingleObject(handle, 30000) != 0:
                raise ValueError("Original service-update CLI did not exit")
        finally:
            kernel.CloseHandle(handle)


def main():

    manifest = Path(sys.argv[1])
    plan = json.loads(manifest.read_text(encoding="utf-8"))
    descriptor = plan["coordinator_fd"]
    code = 1
    try:
        if sys.platform == "win32":
            from support_worker import lease

            wait_parent(plan["pid"])
            with lease(Path(plan["coordinator_lock"]), exclusive=True):
                execute(plan)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            execute(plan)
        code = 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "Service update failed; inspect update --status, service logs and recovery files.",
            file=sys.stderr,
        )
        print(f"Service update recovery plan: {manifest}", file=sys.stderr)
    finally:
        record(plan, "succeeded" if code == 0 else "failed", "complete", code)
        # Inner update has its own result; expose the entire service cycle last.
        pointer = Path(plan["pointer"])
        temporary = pointer.with_suffix(".service-tmp")
        temporary.write_text(json.dumps({"result": plan["result"]}), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(pointer)
        with contextlib.suppress(OSError):
            Path(plan["pending_pointer"]).unlink()
        if sys.platform != "win32":
            os.close(descriptor)
        if code == 0:
            shutil.rmtree(manifest.parent)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
