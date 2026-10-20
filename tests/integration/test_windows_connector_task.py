"""Explicit opt-in native Task Scheduler acceptance with a disposable task only."""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import uuid

import pytest

from acp_gateway import service, windows_service
from acp_gateway.config import load_config
from acp_gateway.connectors.launch import ConnectorLaunch

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("ACPGW_TEST_WINDOWS_TASK") != "1",
    reason="set ACPGW_TEST_WINDOWS_TASK=1 in a native Windows Gateway development environment",
)


@pytest.fixture
def disposable_connector(tmp_path, monkeypatch):
    # Production task identity, real Hermes and user config are never touched.
    prefix = "ACP Gateway Test-" + uuid.uuid4().hex + "-"
    monkeypatch.setattr(
        windows_service, "SCRIPT", windows_service.SCRIPT.replace("ACP Gateway Connector-", prefix)
    )
    config_path = tmp_path / "computer.yaml"
    env_path = tmp_path / "computer.env"
    key_path = tmp_path / "computer.key"
    import yaml

    config_path.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(tmp_path / "data"),
                "logging": {"file": str(tmp_path / "connector.log")},
                "agents": [
                    {"alias": "test", "url": "ws://127.0.0.1:1/acp", "default_cwd": str(tmp_path)}
                ],
            }
        )
    )
    env_path.write_text("")
    key_path.write_text("acpc_" + secrets.token_urlsafe(32))
    # Protect this disposable directory and its children; never alter user home ACLs.
    acl_script = r"""
    $ErrorActionPreference = 'Stop'
    # Avoid loading PowerShell 7 modules inherited through PSModulePath.
    $env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
    $path = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($existing in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($existing) }
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    ([IO.DirectoryInfo]$path).SetAccessControl($acl)
    foreach ($item in Get-ChildItem -LiteralPath $path -Force) {
        $child = Get-Acl -LiteralPath $item.FullName
        $child.SetAccessRuleProtection($false, $false)
        foreach ($rule in @($child.Access)) {
            if (-not $rule.IsInherited) { $child.RemoveAccessRuleSpecific($rule) }
        }
        $item.SetAccessControl($child)
    }
    """
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(acl_script.encode("utf-16-le")).decode("ascii"),
        ],
        input=json.dumps(str(tmp_path)),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    config = load_config(config_path, env_path)
    launch = ConnectorLaunch("ws://127.0.0.1:1/connect", "test", key_path)

    try:
        yield config, launch, prefix
    finally:
        windows_service._request("uninstall", force=True)


@pytest.mark.parametrize("existing", [False, True])
def test_native_setup_task_restores_disabled_definition(disposable_connector, existing):
    config, launch, _ = disposable_connector
    if existing:
        service.install(config, launch=launch)
    transaction = windows_service.SetupTask(config)
    snapshot = transaction.snapshot.copy()
    transaction.apply(launch)
    assert windows_service._request("status")["enabled"]
    transaction.restore()
    restored = windows_service.SetupTask(config).snapshot
    assert restored == snapshot


def test_native_setup_task_refuses_concurrent_replacement(disposable_connector):
    config, launch, _ = disposable_connector
    service.install(config, launch=launch)
    transaction = windows_service.SetupTask(config)
    service.install(config, launch=launch)
    replacement = windows_service.SetupTask(config).snapshot
    with pytest.raises(ValueError, match="changed during setup"):
        transaction.apply(launch)
    with pytest.raises(ValueError, match="rollback refused"):
        transaction.restore()
    assert windows_service.SetupTask(config).snapshot == replacement


def test_native_setup_task_refuses_another_config(disposable_connector, tmp_path):
    from dataclasses import replace

    config, launch, _ = disposable_connector
    service.install(config, launch=launch)
    other = replace(config, config_path=tmp_path / "other.yaml")
    with pytest.raises(ValueError, match="another configuration"):
        windows_service.SetupTask(other)


@pytest.mark.parametrize("existing", [False, True])
def test_native_wizard_start_failure_restores_files_and_task(
    disposable_connector, monkeypatch, existing
):
    from acp_gateway.cli import wizard
    from fakes.setup_prompts import save_answers

    config, launch, _ = disposable_connector
    if existing:
        service.install(config, launch=launch)
    snapshot = windows_service.SetupTask(config).snapshot
    files = [config.config_path, config.env_file, config.config_path.parent / wizard.STATE]
    originals = {path: path.read_bytes() if path.exists() else None for path in files}
    request = windows_service._request

    def fail_after_enable(action, **values):
        result = request(action, **values)
        if action == "enable":
            raise ValueError("injected failure after task start")
        return result

    monkeypatch.setattr(windows_service, "_request", fail_after_enable)
    with pytest.raises(ValueError, match="injected failure"):
        wizard.wizard(
            config_file=config.config_path,
            env_file=config.env_file,
            role="connector",
            prompts=save_answers(
                **{
                    "Server connection URL": launch.dispatcher_url,
                    "I accept unencrypted WS credentials and conversations?": True,
                    "Connection method": "advanced credential file",
                    "Enrollment key file copied privately from the server": str(launch.token_file),
                    "Install and start the Windows connector logon task?": True,
                }
            ),
        )
    assert {path: path.read_bytes() if path.exists() else None for path in files} == originals
    assert windows_service.SetupTask(config).snapshot == snapshot


@pytest.mark.parametrize(
    "enabled,running", [(True, True), (True, False), (False, True), (False, False)]
)
def test_native_maintenance_snapshot_flags_and_definition_fence(
    disposable_connector, enabled, running
):
    config, launch, _ = disposable_connector
    from acp_gateway import service_update
    from acp_gateway.service_update_worker import windows_request

    service.install(config, launch=launch)
    windows_service._request("maintenance-flags", enabled=True)
    if running:
        windows_service._request("start")
        deadline = time.monotonic() + 15
        while True:
            try:
                snapshot = service_update.inspect_service(
                    str(config.config_path), str(config.env_file), "connector"
                )
                if snapshot["running"]:
                    break
            except ValueError:
                pass
            assert time.monotonic() < deadline
            time.sleep(0.2)
    windows_service._request("maintenance-flags", enabled=enabled)
    snapshot = service_update.inspect_service(
        str(config.config_path), str(config.env_file), "connector"
    )
    assert snapshot["enabled"] is enabled and snapshot["running"] is running
    plan = {"python": sys.executable, "windows_script": windows_service.SCRIPT}
    windows_request(plan, snapshot, "maintenance-flags", enabled=not enabled)
    current = windows_request(plan, snapshot, "maintenance-state")
    assert current["definition"] == snapshot["definition"]
    assert (current["state"] == 4) is running
    wrong = snapshot | {"definition": "unrelated XML"}
    with pytest.raises(ValueError, match="Task Scheduler"):
        windows_request(plan, wrong, "maintenance-flags", enabled=enabled)
    windows_request(plan, snapshot, "maintenance-flags", enabled=enabled)


def test_disposable_connector_logon_task_lifecycle(disposable_connector):
    config, launch, prefix = disposable_connector
    config_path, env_path, key_path = config.config_path, config.env_file, launch.token_file

    def wait_state(expected):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = windows_service._request("status")
            if result.get("state") == expected:
                return result
            time.sleep(0.2)
        pytest.fail(f"task did not reach state {expected}")

    try:
        assert service.install(config, launch=launch) == 0
        status = windows_service._request("status")
        assert status["installed"] and not status["enabled"]
        assert status["name"].startswith(prefix)
        service.control("enable", role="connector")
        assert wait_state(4)["enabled"]
        # Reinstallation must not modify a running task.
        with pytest.raises(ValueError, match="Stop the existing"):
            service.install(config, launch=launch)
        assert windows_service._request("restart")["stopMode"] == "graceful"
        wait_state(4)
        assert windows_service._request("disable")["stopMode"] == "graceful"
        assert not wait_state(1)["enabled"]
        service.control("logs", role="connector")
        service.control("uninstall", role="connector")
        assert not windows_service._request("status")["installed"]
        assert config_path.exists() and env_path.exists() and key_path.exists()
    finally:
        windows_service._request("uninstall")


@pytest.mark.parametrize("target", ["config", "env", "key", "log", "directory"])
def test_public_acl_refused_without_task_changes(disposable_connector, target):
    import ntsecuritycon
    import win32security

    config, launch, _ = disposable_connector
    log = config.settings.logging.file
    log.write_text("disposable test log\n")
    path = {
        "config": config.config_path,
        "env": config.env_file,
        "key": launch.token_file,
        "log": log,
        "directory": config.config_path.parent,
    }[target]
    security = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    original = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    public = security.GetSecurityDescriptorDacl()
    public.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        ntsecuritycon.FILE_GENERIC_READ
        | (ntsecuritycon.FILE_GENERIC_WRITE if target == "directory" else 0),
        win32security.CreateWellKnownSid(win32security.WinWorldSid),
    )
    security.SetSecurityDescriptorDacl(1, public, 0)
    try:
        win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, security)
        with pytest.raises(
            ValueError,
            match=r"ACL is not private|Computer credential files must have private Windows ACLs",
        ):
            service.install(config, launch=launch)
        assert not windows_service._request("status")["installed"]
    finally:
        win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, original)


def test_foreign_installation_and_unmanaged_task_refused(disposable_connector):
    import win32com.client

    config, launch, _ = disposable_connector
    service.install(config, launch=launch)
    status = windows_service._request("status")
    for action in ("status", "enable", "disable", "uninstall"):
        with pytest.raises(ValueError, match="another installation"):
            windows_service._request(action, executable=sys.executable + ".foreign")
    scheduler = win32com.client.Dispatch("Schedule.Service")
    scheduler.Connect()
    folder = scheduler.GetFolder("\\")
    definition = folder.GetTask(status["name"]).Definition
    original = definition.RegistrationInfo.Description
    definition.RegistrationInfo.Description = "unmanaged disposable acceptance task"
    sid = definition.Principal.UserId
    folder.RegisterTaskDefinition(status["name"], definition, 6, sid, None, 3)
    try:
        for action in ("status", "enable", "disable", "uninstall"):
            with pytest.raises(ValueError, match="unmanaged"):
                windows_service._request(action)
    finally:
        definition.RegistrationInfo.Description = original
        folder.RegisterTaskDefinition(status["name"], definition, 6, sid, None, 3)


def test_native_task_blocks_maintenance_setup_and_foreground(disposable_connector, tmp_path):
    from filelock import FileLock, Timeout

    from acp_gateway import installation
    from acp_gateway.cli import wizard
    from acp_gateway.cli.main import main

    config, launch, _ = disposable_connector
    service.install(config, launch=launch)
    service.control("enable", role="connector")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        lock = FileLock(config.settings.data_dir / "connector.lock", timeout=0)
        try:
            lock.acquire()
            lock.release()
        except Timeout:
            break
        time.sleep(0.2)
    else:
        pytest.fail("scheduled connector did not acquire its data directory lock")
    assert (
        main(
            [
                "--config",
                str(config.config_path),
                "--env-file",
                str(config.env_file),
                *launch.arguments(),
            ]
        )
        == 78
    )
    with pytest.raises(ValueError, match="disable"):
        installation._service_preflight(uninstall=True)
    with pytest.raises(ValueError, match="Disable the connector task"):
        wizard.wizard(tmp_path / "unused-setup")
    assert not (tmp_path / "unused-setup").exists()
    service.control("disable", role="connector")
    assert installation._service_preflight(uninstall=False) == []


def test_native_explicit_force_and_restart_instance_fencing(disposable_connector):
    from acp_gateway.connectors.lifecycle import RUNTIME_FILE, STOP_FILE

    config, launch, _ = disposable_connector
    service.install(config, launch=launch)
    directory = config.settings.resolved_data_dir()

    def wait_record(previous=None):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                record = json.loads((directory / RUNTIME_FILE).read_text())
                if record["instance"] != previous:
                    return record
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        pytest.fail("connector did not publish a new instance")

    service.control("enable", role="connector")
    old = wait_record()
    assert windows_service._request("disable", force=True)["stopMode"] == "forced"
    # A request from the forcibly stopped process must not stop its replacement.
    (directory / STOP_FILE).write_text(json.dumps({"version": 1, "instance": old["instance"]}))
    service.control("enable", role="connector")
    current = wait_record(old["instance"])
    assert current["instance"] != old["instance"]
    time.sleep(0.5)
    assert windows_service._request("status")["state"] == 4
    assert windows_service._request("disable")["stopMode"] == "graceful"
    assert not (directory / RUNTIME_FILE).exists()


@pytest.mark.parametrize("target", [None, "record", "token", "directory"])
def test_native_hermes_discovery_acl_before_token_read(disposable_connector, target, monkeypatch):
    import ntsecuritycon
    import win32security

    from acp_gateway.discovery import hermes

    config, _, _ = disposable_connector
    root = config.config_path.parent
    if target is None:
        # Windows CPython uses OWNER RIGHTS for mode 0700; descendants inherit it.
        root = root / "python-private-rendezvous"
        root.mkdir(mode=0o700)
    record_path = root / "host-desktop-serve.json"
    token_path = root / "host-desktop-serve.token"
    token = secrets.token_urlsafe(32)
    token_path.write_text(token)
    record_path.write_text(
        json.dumps(
            {
                "role": "desktop-serve",
                "pid": 1234,
                "host": "127.0.0.1",
                "port": 4549,
                "protocolVersion": 1,
                "profiles": ["default"],
                "tokenFingerprint": hashlib.sha256(token.encode()).hexdigest()[:16],
            }
        )
    )
    if target is None:
        assert hermes.read_endpoint(root, "desktop-serve").token == token
        return
    path = {"record": record_path, "token": token_path, "directory": root}[target]
    original = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    security = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    dacl = security.GetSecurityDescriptorDacl()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        ntsecuritycon.FILE_GENERIC_READ
        | (ntsecuritycon.FILE_GENERIC_WRITE if target == "directory" else 0),
        win32security.CreateWellKnownSid(win32security.WinWorldSid),
    )
    security.SetSecurityDescriptorDacl(1, dacl, 0)
    monkeypatch.setattr(
        hermes, "_private_read", lambda *args: pytest.fail("read before ACL denial")
    )
    try:
        win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, security)
        with pytest.raises(hermes.HermesDiscoveryError, match="private Windows ACLs") as exc:
            hermes.read_endpoint(root, "desktop-serve")
        assert exc.value.status == "auth-required"
        assert token not in str(exc.value)
    finally:
        win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, original)


def test_native_graceful_stop_tolerates_record_removed_during_acl_check(
    disposable_connector, monkeypatch
):
    config, launch, _ = disposable_connector
    directory = config.settings.resolved_data_dir()
    runtime = directory / "connector-control.json"
    marker = directory / "race-observed"
    service.install(config, launch=launch)
    windows_service._request("enable")
    deadline = time.monotonic() + 20
    while not runtime.exists():
        assert time.monotonic() < deadline, "connector did not publish its activity record"
        time.sleep(0.1)
    # Deliver a genuine instance-fenced stop between the existence and ACL checks.
    # This forces the real connector's cleanup to remove the record in that window.
    hook = r"""
                    $testRecord = [IO.File]::ReadAllText($runtime) | ConvertFrom-Json
                    $testBody = @{version=1; instance=$testRecord.instance} | ConvertTo-Json
                    [IO.File]::WriteAllText($request, $testBody)
                    $testDeadline = [DateTime]::UtcNow.AddSeconds(10)
                    while ((Test-Path -LiteralPath $runtime) -and
                           [DateTime]::UtcNow -lt $testDeadline) {
                        Start-Sleep -Milliseconds 50
                    }
                    if (-not (Test-Path -LiteralPath $runtime)) {
                        [IO.File]::WriteAllText(
                            [IO.Path]::Combine($meta.controlDirectory, 'race-observed'), 'observed')
                    }
                    Assert-Private $runtime $false
"""
    monkeypatch.setattr(
        windows_service,
        "SCRIPT",
        windows_service.SCRIPT.replace("Assert-Private $runtime $false", hook, 1),
    )
    windows_service._request("disable")
    assert marker.exists() and not runtime.exists()
    status = windows_service._request("status")
    assert not status["enabled"] and status["state"] != 4


def test_native_task_runs_windowless_and_remains_cli_managed(disposable_connector):
    import xml.etree.ElementTree as ET
    from pathlib import Path

    config, launch, _ = disposable_connector
    service.install(config, launch=launch)
    snapshot = windows_service.SetupTask(config).snapshot
    # Only the task just created by this disposable fixture is parsed.
    xml = ET.fromstring(snapshot["xml"])  # noqa: S314
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    command = xml.find("t:Actions/t:Exec/t:Command", ns).text
    assert Path(command).name.lower() == "pythonw.exe"
    windows_service._request("enable")
    runtime = config.settings.resolved_data_dir() / "connector-control.json"
    deadline = time.monotonic() + 20
    while not runtime.exists():
        assert time.monotonic() < deadline, "windowless connector did not start"
        time.sleep(0.1)
    pid = json.loads(runtime.read_text())["pid"]
    script = (
        f"$p=Get-Process -Id {int(pid)} -ErrorAction Stop; "
        "@{name=$p.ProcessName; window=[Int64]$p.MainWindowHandle} | ConvertTo-Json -Compress"
    )
    from acp_gateway.powershell import encoded_script

    probe = subprocess.run(
        ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded_script(script)],
        capture_output=True,
        text=True,
        check=True,
    )
    process = json.loads(probe.stdout)
    # uv may delegate its GUI launcher to a process named python.exe.
    assert process["name"] in {"python", "pythonw"} and process["window"] == 0
    records = [json.loads(line) for line in config.settings.logging.file.read_text().splitlines()]
    assert any(
        row.get("event") == "connector runtime" and row.get("console_attached") is False
        for row in records
    )
    assert windows_service._request("disable")["stopMode"] == "graceful"
    assert not runtime.exists()


def test_native_owned_task_selection_reads_existing_config_paths(disposable_connector):
    config, launch, _ = disposable_connector
    service.install(config, launch=launch)
    selected = windows_service.installed_selection()
    assert selected == (config.config_path.resolve(), config.env_file.resolve())
    # A different interpreter cannot use automatic discovery to claim this task.
    with pytest.raises(ValueError, match="another installation"):
        windows_service._request(
            "selection", executable=str(config.config_path.parent / "other.exe")
        )
