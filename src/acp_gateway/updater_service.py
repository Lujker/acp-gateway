"""Independent updater autostart; installed helpers never import the replaced package."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from acp_gateway.maintenance_worker import lease
from acp_gateway.remote_update_worker import load_policy
from acp_gateway.service import _quote, _systemctl
from acp_gateway.update_journal import Journal
from acp_gateway.update_runner import copy_runtime

MARKER = "# Managed by acpgw updater install.\n"


def identity(policy):
    return hashlib.sha256(os.fsencode(str(policy))).hexdigest()[:16]


def runtime_path(policy):
    return policy.parent / ("updater-runtime-" + identity(policy))


def unit_path(policy):
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not root.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be absolute")
    return root / "systemd/user" / ("acp-gateway-updater-" + identity(policy) + ".service")


def command(policy):
    executable = Path(sys._base_executable).absolute()
    if sys.platform == "win32" and executable.with_name("pythonw.exe").is_file():
        executable = executable.with_name("pythonw.exe")
    return [str(executable), str(runtime_path(policy) / "worker.py"), str(policy)]


def render_unit(policy):
    return (
        MARKER
        + "[Unit]\nDescription=ACP Gateway independent updater\n\n"
        + "[Service]\nType=simple\nExecStart="
        + " ".join(_quote(value) for value in command(policy))
        + "\nRestart=on-failure\nRestartSec=10\nUMask=0077\n"
        # A service manager stop must not kill an installer or a rollback child.
        + "KillMode=process\nTimeoutStopSec=30\nSendSIGKILL=no\n\n"
        + "[Install]\nWantedBy=default.target\n"
    )


def _private_directory(path):
    if sys.platform == "win32":
        from acp_gateway.windows_acl import ensure_private_directory

        ensure_private_directory(path)
        return
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("Updater runtime and spool directories must be private to this user")


def _owned_unit(path):
    if path.is_symlink() or not path.read_text(encoding="utf-8").startswith(MARKER):
        raise ValueError("Refusing to modify an unmanaged updater unit")


def _atomic(path, contents):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _idle(root):
    try:
        with lease(root / "worker.lock", exclusive=True):
            return True
    except ValueError:
        return False


def stop_worker(policy, *, timeout=30):
    """Instance-bound cooperative stop. Never terminate a running installation."""
    root = Path(policy["spool_dir"])
    if not root.exists():
        return
    _private_directory(root)
    deadline = time.monotonic() + timeout
    instance = None
    while not _idle(root):
        if time.monotonic() >= deadline:
            raise ValueError(
                "Updater has not stopped; it may be finishing an update. "
                "Inspect status and retry later; no process was force-stopped"
            )
        journal = Journal(root / "operations.db")
        try:
            worker = journal.worker()
        finally:
            journal.close()
        if isinstance(worker, dict) and worker.get("instance") != instance:
            candidate = worker.get("instance")
            if isinstance(candidate, str) and len(candidate) == 32:
                instance = candidate
                _atomic(root / "worker-stop.json", json.dumps({"instance": instance}))
        time.sleep(0.1)


def manage(action, policy):
    if sys.platform not in {"linux", "win32"}:
        raise ValueError("Updater autostart supports Linux and Windows only")
    policy = policy.expanduser().absolute()
    settings = load_policy(policy)
    runtime = runtime_path(policy)
    root = Path(settings["spool_dir"])
    if action == "install":
        _private_directory(root)
        _private_directory(runtime)
        if os.name == "posix" and policy.parent.stat().st_mode & 0o022:
            raise ValueError("Updater policy directory must not be writable by other users")
        # Hold the same lock while replacing files: running helpers are never overwritten.
        with lease(root / "worker.lock", exclusive=True):
            if sys.platform == "linux":
                path = unit_path(policy)
                if path.exists() or path.is_symlink():
                    _owned_unit(path)
                _systemctl("show-environment")
            copy_runtime(runtime)
            if sys.platform == "linux":
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic(path, render_unit(policy))
                _systemctl("daemon-reload")
            else:
                _windows("install", policy)
        print("Updater installed, disabled on first install. Enable with:")
        print(f'acpgw updater --policy "{policy}" enable')
        return 0
    if action in {"stop", "disable", "restart", "uninstall"}:
        # Disable autostart first so no new task instance races an uninstall/disable.
        if action in {"disable", "uninstall"}:
            _control("disable", policy)
        stop_worker(settings)
        _control("stop", policy)
    _control("start" if action == "restart" else action, policy)
    if action == "status":
        path = root / "operations.db"
        if path.is_file():
            journal = Journal(path)
            try:
                print("Worker: " + json.dumps(journal.worker(), ensure_ascii=True))
                print("Unfinished: " + json.dumps(journal.unfinished(), ensure_ascii=True))
            finally:
                journal.close()
        else:
            print("Worker has not created a mailbox yet.")
    elif action == "uninstall":
        # Keep runtime, policy, mailbox and recovery evidence for diagnosis/reinstallation.
        print("Updater autostart removed; policy, helpers and operation history kept.")
    else:
        print(f"Updater: {action} completed.")
    return 0


def _control(action, policy):
    if sys.platform == "win32":
        return _windows(action, policy)
    path = unit_path(policy)
    if action == "status":
        print(
            _systemctl(
                "show",
                path.name,
                "--no-pager",
                "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID,Result",
                allow_not_found=True,
            ).strip()
        )
        return
    if not path.exists() and not path.is_symlink():
        if action in {"disable", "uninstall", "stop"}:
            return
        raise ValueError("Updater is not installed; run updater install first")
    _owned_unit(path)
    if action == "uninstall":
        _systemctl("stop", path.name)
        path.unlink()
        _systemctl("daemon-reload")
    else:
        args = ("enable", "--now") if action == "enable" else (action,)
        _systemctl(*args, path.name)


WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $r = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $name = 'ACP Gateway Updater-' + $sid + '-' + $r.identity
    $scheduler = New-Object -ComObject Schedule.Service
    $scheduler.Connect()
    $folder = $scheduler.GetFolder('\')
    $task = $null
    try { $task = $folder.GetTask($name) } catch {
        $e = $_.Exception
        while ($null -ne $e.InnerException) { $e = $e.InnerException }
        if ($e.HResult -notin @(-2147024894, -2147024893)) { throw }
    }
    if ($null -ne $task) {
        $meta = $task.Definition.RegistrationInfo.Description | ConvertFrom-Json
        $principal = $task.Definition.Principal
        $user = $principal.UserId
        if ($user -notmatch '^S-1-') {
            $user = (New-Object Security.Principal.NTAccount($user)).Translate(
                [Security.Principal.SecurityIdentifier]).Value
        }
        if ($meta.marker -cne 'acpgw-updater-v1' -or $meta.policy -cne $r.policy -or
            $user -ne $sid -or $principal.LogonType -ne 3 -or $principal.RunLevel -ne 0 -or
            $task.Definition.Actions.Count -ne 1 -or
            $task.Definition.Actions.Item(1).Path -cne $r.executable -or
            $task.Definition.Actions.Item(1).Arguments -cne $r.arguments) {
            throw 'Refusing an unmanaged updater task or another installation'
        }
    }
    if ($r.action -eq 'install') {
        if ($null -ne $task -and $task.GetInstances(0).Count -gt 0) {
            throw 'Stop updater before reinstalling'
        }
        $enabled = $null -ne $task -and $task.Enabled
        $definition = $scheduler.NewTask(0)
        $definition.RegistrationInfo.Description = @{
            marker='acpgw-updater-v1'; policy=$r.policy
        } | ConvertTo-Json -Compress
        $definition.Principal.UserId = $sid
        $definition.Principal.LogonType = 3
        $definition.Principal.RunLevel = 0
        $trigger = $definition.Triggers.Create(9)
        $trigger.UserId = $sid
        $settings = $definition.Settings
        $settings.Enabled = $enabled
        $settings.MultipleInstances = 2
        $settings.DisallowStartIfOnBatteries = $false
        $settings.StopIfGoingOnBatteries = $false
        $settings.StartWhenAvailable = $true
        $settings.AllowDemandStart = $true
        $settings.ExecutionTimeLimit = 'PT0S'
        $settings.RestartInterval = 'PT1M'
        $settings.RestartCount = 3
        $action = $definition.Actions.Create(0)
        $action.Path = $r.executable
        $action.Arguments = $r.arguments
        $action.WorkingDirectory = $r.directory
        $sddl = 'D:P(A;;FA;;;' + $sid + ')(A;;FA;;;SY)(A;;FA;;;BA)'
        $task = $folder.RegisterTaskDefinition($name, $definition, 6, $sid, $null, 3, $sddl)
    } elseif ($null -ne $task) {
        switch ($r.action) {
            'enable' { $task.Enabled = $true; $null = $task.Run($null) }
            'start' {
                $wasEnabled = $task.Enabled
                try { $task.Enabled = $true; $null = $task.Run($null) }
                finally { $task.Enabled = $wasEnabled }
            }
            'disable' { $task.Enabled = $false }
            'stop' {
                $deadline = [DateTime]::UtcNow.AddSeconds(20)
                while ($task.GetInstances(0).Count -gt 0) {
                    if ([DateTime]::UtcNow -ge $deadline) { throw 'Updater has not exited yet' }
                    Start-Sleep -Milliseconds 100
                }
            }
            'uninstall' {
                if ($task.GetInstances(0).Count -gt 0) { throw 'Updater has not exited yet' }
                $folder.DeleteTask($name, 0); $task = $null
            }
            'status' { }
            default { throw 'Unknown updater action' }
        }
    } elseif ($r.action -notin @('status','stop','disable','uninstall')) {
        throw 'Updater is not installed'
    }
    if ($null -eq $task) { @{installed=$false} | ConvertTo-Json -Compress }
    else {
        @{installed=$true; enabled=$task.Enabled; state=[int]$task.State;
          lastResult=$task.LastTaskResult; name=$name} | ConvertTo-Json -Compress
    }
} catch {
    [Console]::Error.WriteLine('Updater Task Scheduler operation failed; inspect the task locally')
    exit 1
}
"""


def _windows(action, policy):
    from acp_gateway.powershell import encoded_script

    executable = shutil.which("powershell.exe")
    if executable is None:
        raise ValueError("Windows PowerShell is required for updater autostart")
    argv = command(policy)
    result = subprocess.run(  # noqa: S603 — fixed script and JSON stdin, no shell
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded_script(WINDOWS_SCRIPT),
        ],
        input=json.dumps(
            {
                "action": action,
                "policy": str(policy),
                "identity": identity(policy),
                "executable": argv[0],
                "arguments": subprocess.list2cmdline(argv[1:]),
                "directory": str(runtime_path(policy)),
            }
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ValueError("Updater Task Scheduler operation failed; inspect the task locally")
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or type(value.get("installed")) is not bool:
        raise ValueError("Invalid updater task response")
    print(json.dumps(value))
    return value
