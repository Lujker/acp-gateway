"""Per-user connector logon task; no shell, password or external agent process control."""

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from acp_gateway.powershell import encoded_script

# All dynamic values enter through JSON stdin, never PowerShell source/command text.
# COM avoids localized schtasks output and stores no account password.
SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Avoid loading PowerShell 7 modules inherited through PSModulePath.
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $r = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $name = 'ACP Gateway Connector-' + $sid
    $allowed = @($sid, 'S-1-5-18', 'S-1-5-32-544')
    function Assert-Private($path, $directory) {
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse points are not supported in service files'
        }
        $acl = Get-Acl -LiteralPath $path
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $allowed) { throw 'Service file has an untrusted owner' }
        # OWNER RIGHTS is safe only after validating this object's owner.
        # CPython creates private Windows directories with this SID.
        $readers = $allowed + @('S-1-3-4')
        $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
        $sddl = $acl.GetSecurityDescriptorSddlForm('Access')
        if ($sddl -match 'NO_ACCESS_CONTROL') { throw 'Null DACL is not supported' }
        foreach ($rule in $rules) {
            if ($rule.AccessControlType -ne 'Allow') { continue }
            if ($rule.IdentityReference.Value -in $readers) { continue }
            # For directories reject writes/deletes/ACL changes; files must be private.
            $rights = [int]$rule.FileSystemRights
            if ((-not $directory -and $rights -ne 0) -or
                ($directory -and ($rights -band 852310) -ne 0)) {
                throw 'ACL is not private; allow only this user, SYSTEM and Administrators'
            }
        }
    }
    $scheduler = New-Object -ComObject Schedule.Service
    $scheduler.Connect()
    $folder = $scheduler.GetFolder('\')
    $task = $null
    try { $task = $folder.GetTask($name) }
    catch {
        $exception = $_.Exception
        while ($null -ne $exception.InnerException) { $exception = $exception.InnerException }
        if ($exception.HResult -notin @(-2147024894, -2147024893)) { throw }
    }
    $ownedExecutables = @($r.executable)
    $interpreterName = [IO.Path]::GetFileName($r.executable)
    if ($interpreterName -in @('python.exe', 'pythonw.exe')) {
        $siblingName = if ($interpreterName -eq 'python.exe') {
            'pythonw.exe'
        } else { 'python.exe' }
        $sibling = [IO.Path]::Combine([IO.Path]::GetDirectoryName($r.executable), $siblingName)
        if (Test-Path -LiteralPath $sibling -PathType Leaf) { $ownedExecutables += $sibling }
    }
    if ($null -ne $task) {
        try { $meta = $task.Definition.RegistrationInfo.Description | ConvertFrom-Json }
        catch { throw 'Refusing an unmanaged Task Scheduler task' }
        $principal = $task.Definition.Principal
        $taskUser = $principal.UserId
        if ($taskUser -notmatch '^S-1-') {
            $account = New-Object Security.Principal.NTAccount($taskUser)
            $taskUser = $account.Translate([Security.Principal.SecurityIdentifier]).Value
        }
        if ($meta.marker -ne 'acpgw-connector-v1' -or $taskUser -ne $sid -or
            $principal.LogonType -ne 3 -or $principal.RunLevel -ne 0 -or
            $task.Definition.Actions.Count -ne 1 -or
            $task.Definition.Actions.Item(1).Path -notin $ownedExecutables) {
            throw 'Refusing an unmanaged task or a task owned by another installation'
        }
    }
    $stopMode = $null
    $normalized = $null
    if ($null -ne $task) {
        $copy = $scheduler.NewTask(0)
        $copy.XmlText = $task.Xml
        $copy.Settings.Enabled = $false
        $normalized = $copy.XmlText
    }
    if ($r.expectedDefinition -and $normalized -cne $r.expectedDefinition) {
        throw 'Connector task definition changed; maintenance refused'
    }
    if ($r.expectedControlId -and $r.action -ne 'restore' -and
        ($null -eq $task -or $meta.controlId -cne $r.expectedControlId)) {
        throw 'Connector task changed during setup; refusing to modify it'
    }
    function Stop-Connector {
        if ($task.GetInstances(0).Count -eq 0) { return 'idle' }
        if ($r.force) {
            $task.Stop(0)
            $mode = 'forced'
        } else {
            if (-not $meta.controlDirectory -or
                $meta.controlId -cnotmatch '^[a-f0-9]{32}$') {
                throw 'Legacy task: use stop --force, then reinstall to enable graceful stop'
            }
            Assert-Private $meta.controlDirectory $true
            $runtime = [IO.Path]::Combine($meta.controlDirectory, 'connector-control.json')
            $request = [IO.Path]::Combine($meta.controlDirectory, 'connector-stop.json')
            $mode = 'graceful'
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        $sent = $null
        while ($task.GetInstances(0).Count -gt 0) {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw 'Connector did not exit; inspect logs or explicitly retry with --force'
            }
            if (-not $r.force -and (Test-Path -LiteralPath $runtime)) {
                try {
                    Assert-Private $runtime $false
                    if ((Get-Item -LiteralPath $runtime).Length -gt 1024) {
                        throw 'Invalid connector control record'
                    }
                    $record = [IO.File]::ReadAllText($runtime) | ConvertFrom-Json
                } catch {
                    # The connector removes its record while exiting. Recheck the task
                    # within the same bounded deadline; never force-stop on missing IPC.
                    if (-not (Test-Path -LiteralPath $runtime)) {
                        Start-Sleep -Milliseconds 200
                        continue
                    }
                    throw
                }
                if ($record.version -ne 1 -or $record.instance -cnotmatch '^[a-f0-9]{32}$') {
                    throw 'Invalid connector control record'
                }
                # A foreground connector sharing the directory is not this task.
                if ($record.controlId -eq $meta.controlId -and $record.instance -ne $sent) {
                    if (Test-Path -LiteralPath $request) { Assert-Private $request $true }
                    $body = @{version=1; instance=$record.instance} | ConvertTo-Json -Compress
                    [IO.File]::WriteAllText($request, $body)
                    $sent = $record.instance
                }
            }
            Start-Sleep -Milliseconds 200
        }
        return $mode
    }
    if ($r.action -eq 'selection') {
        if ($null -eq $task) { throw 'Connector task is not installed' }
        @{installed=$true; arguments=$task.Definition.Actions.Item(1).Arguments} |
            ConvertTo-Json -Compress
        exit 0
    }
    if ($r.action -in @('snapshot', 'maintenance-state')) {
        if ($null -ne $task) {
            if ($r.action -eq 'snapshot' -and
                ($task.Enabled -or $task.State -in @(2, 4) -or $task.GetInstances(0).Count -gt 0)) {
                throw 'Disable the connector task before editing settings'
            }
            if ($r.action -eq 'maintenance-state' -and
                ($task.State -in @(0, 2) -or $task.GetInstances(0).Count -gt 1 -or
                 $meta.controlId -cnotmatch '^[a-f0-9]{32}$')) {
                throw 'Task state or control identity does not support managed maintenance'
            }
            if (-not $task.Definition.Actions.Item(1).Arguments.StartsWith(
                $r.argumentsPrefix + ' ', [StringComparison]::Ordinal)) {
                throw 'Task uses another configuration; rerun with its config/env paths'
            }
        }
    } elseif ($r.action -eq 'restore') {
        $unchanged = $r.snapshot.installed -and $null -ne $task -and
            $task.Xml -ceq $r.snapshot.xml -and -not $task.Enabled -and
            $task.State -notin @(2, 4) -and $task.GetInstances(0).Count -eq 0
        if (-not $unchanged) {
            if ($null -ne $task) {
                if ($meta.controlId -cne $r.expectedControlId) {
                    throw 'Connector task changed during setup; rollback refused'
                }
                $task.Enabled = $false
                $stopMode = Stop-Connector
                if ($task.State -in @(2, 4) -or $task.GetInstances(0).Count -gt 0) {
                    throw 'Connector task is still active; rollback refused'
                }
            }
            if ($r.snapshot.installed) {
                $definition = $scheduler.NewTask(0)
                $definition.XmlText = $r.snapshot.xml
                if ($definition.Settings.Enabled) { throw 'Rollback requires a disabled snapshot' }
                $sddl = 'D:P(A;;FA;;;' + $sid + ')(A;;FA;;;SY)(A;;FA;;;BA)'
                $task = $folder.RegisterTaskDefinition($name, $definition, 6, $sid, $null, 3, $sddl)
            } elseif ($null -ne $task) {
                $folder.DeleteTask($name, 0)
                $task = $null
            }
        }
    } elseif ($r.action -eq 'install') {
        if ($r.guarded -and (
            [bool]$r.snapshot.installed -ne ($null -ne $task) -or
            ($null -ne $task -and ($task.Xml -cne $r.snapshot.xml -or
             $task.Enabled -or $task.State -in @(2, 4))))) {
            throw 'Connector task changed during setup; installation refused'
        }
        foreach ($path in $r.privateFiles) {
            Assert-Private $path $false
            Assert-Private ([IO.Path]::GetDirectoryName($path)) $true
        }
        Assert-Private $r.logDirectory $true
        Assert-Private $r.controlDirectory $true
        if (Test-Path -LiteralPath $r.logFile) { Assert-Private $r.logFile $false }
        if ($null -ne $task -and $task.GetInstances(0).Count -gt 0) {
            throw 'Stop the existing connector task before reinstalling'
        }
        $definition = $scheduler.NewTask(0)
        $definition.RegistrationInfo.Description = $r.metadata
        $definition.Principal.UserId = $sid
        $definition.Principal.LogonType = 3
        $definition.Principal.RunLevel = 0
        $trigger = $definition.Triggers.Create(9)
        $trigger.UserId = $sid
        $trigger.Delay = 'PT10S'
        $settings = $definition.Settings
        $settings.Enabled = $false
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
    } elseif ($null -eq $task) {
        if ($r.action -notin @('status', 'uninstall')) { throw 'Connector task is not installed' }
    } else {
        switch ($r.action) {
            'maintenance-flags' {
                if ($r.enabled -isnot [bool]) { throw 'Invalid enabled state' }
                $task.Enabled = $r.enabled
            }
            'enable' { $task.Enabled = $true; $null = $task.Run($null) }
            'disable' { $task.Enabled = $false; $stopMode = Stop-Connector }
            'start' { $null = $task.Run($null) }
            'stop' { $stopMode = Stop-Connector }
            'restart' {
                $stopMode = Stop-Connector
                $null = $task.Run($null)
            }
            'uninstall' {
                $task.Enabled = $false
                $stopMode = Stop-Connector
                $folder.DeleteTask($name, 0)
                $task = $null
            }
            'status' { }
            'logs' { }
            default { throw 'Unknown connector task action' }
        }
    }
    if ($r.action -eq 'maintenance-state') {
        if ($null -eq $task) { throw 'Connector task is not installed' }
        $instance = $null
        if ($task.State -eq 4) {
            Assert-Private $meta.controlDirectory $true
            $runtime = [IO.Path]::Combine($meta.controlDirectory, 'connector-control.json')
            # Scheduler reports Running before Python has published its private IPC.
            # Wait only for absence, never for invalid/untrusted records to become valid.
            $deadline = [DateTime]::UtcNow.AddSeconds(20)
            $maintenanceXml = $task.Xml
            while ($null -eq $instance) {
                $task = $folder.GetTask($name)
                if ($task.Xml -cne $maintenanceXml -or $task.State -ne 4 -or
                    $task.GetInstances(0).Count -gt 1) {
                    throw 'Connector task changed during startup; maintenance refused; retry later'
                }
                if (Test-Path -LiteralPath $runtime) {
                    try {
                        Assert-Private $runtime $false
                        if ((Get-Item -LiteralPath $runtime).Length -gt 1024) {
                            throw 'Invalid control record'
                        }
                        $record = [IO.File]::ReadAllText($runtime) | ConvertFrom-Json
                        if ($null -eq $record) { throw 'Invalid control record' }
                    } catch {
                        # Publication/cleanup can race the existence check, as in stop.
                        if (Test-Path -LiteralPath $runtime) { throw }
                        $record = $null
                    }
                    if ($null -ne $record) {
                        if ($record.version -ne 1 -or $record.controlId -cne $meta.controlId -or
                            $record.instance -cnotmatch '^[a-f0-9]{32}$') {
                            throw 'Invalid task instance'
                        }
                        $instance = $record.instance
                        break
                    }
                }
                if ([DateTime]::UtcNow -ge $deadline) {
                    throw ('Connector startup did not publish connector-control.json; ' +
                        'inspect connector logs and retry maintenance')
                }
                Start-Sleep -Milliseconds 200
            }
            if ($task.Xml -cne $maintenanceXml -or $task.State -ne 4 -or
                $task.GetInstances(0).Count -ne 1) {
                throw 'Connector task changed during startup; maintenance refused; retry later'
            }
        }
        @{installed=$true; enabled=$task.Enabled; state=[int]$task.State;
          definition=$normalized; controlId=$meta.controlId;
          controlDirectory=$meta.controlDirectory; instance=$instance} | ConvertTo-Json -Compress
    } elseif ($r.action -eq 'snapshot') {
        if ($null -eq $task) {
            @{installed=$false; xml=$null} | ConvertTo-Json -Compress
        } else {
            @{installed=$true; xml=$task.Xml} | ConvertTo-Json -Compress
        }
    } elseif ($null -eq $task) {
        @{name=$name; installed=$false; stopMode=$stopMode} | ConvertTo-Json -Compress
    } else {
        $meta = $task.Definition.RegistrationInfo.Description | ConvertFrom-Json
        @{name=$name; installed=$true; enabled=$task.Enabled; state=[int]$task.State;
          lastResult=$task.LastTaskResult; logFile=$meta.logFile;
          stopMode=$stopMode} | ConvertTo-Json -Compress
    }
} catch {
    # Keep errors separate from PowerShell's host-generated CLIXML on stderr.
    @{error=$_.Exception.Message} | ConvertTo-Json -Compress
    exit 1
}
"""


def _request(action, **values):
    executable = shutil.which("powershell.exe")
    if executable is None:
        raise ValueError("Windows PowerShell is unavailable")
    script = encoded_script(SCRIPT)
    try:
        result = subprocess.run(  # noqa: S603 — fixed script, JSON stdin, no shell
            [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
            input=json.dumps({"action": action, "executable": sys.executable, **values}),
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            "Task Scheduler operation timed out; inspect the task before retrying"
        ) from None
    if result.returncode:
        # Host progress/error records may be serialized as CLIXML even when
        # the script catches the exception. Prefer our structured message; never
        # expose raw host records (which can include source and dynamic values).
        from acp_gateway.log import redact_text

        message = result.stderr.strip()
        if not message or "CLIXML" in message or "<Objs" in message:
            message = "operation failed; inspect connector logs and Task Scheduler before retrying"
        try:
            failure = json.loads(result.stdout)
            if isinstance(failure, dict) and isinstance(failure.get("error"), str):
                message = failure["error"] or message
        except (ValueError, TypeError):
            pass
        raise ValueError("Task Scheduler: " + redact_text(message))
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("installed"), bool):
            raise ValueError
        return data
    except (ValueError, TypeError):
        raise ValueError("Task Scheduler returned an invalid response") from None


def _split_arguments(command):
    """Use the Windows parser matching subprocess.list2cmdline."""
    import ctypes
    from ctypes import wintypes

    count = ctypes.c_int()
    parser = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
    parser.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parser.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = parser("python " + command, ctypes.byref(count))
    if not argv:
        raise ValueError("Cannot parse the installed connector task")
    release = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    release.argtypes = [ctypes.c_void_p]
    release.restype = ctypes.c_void_p
    try:
        return [argv[i] for i in range(1, count.value)]
    finally:
        release(ctypes.cast(argv, ctypes.c_void_p))


def installed_selection():
    """Read paths only from this installation's owned per-user connector task."""
    task = _request("selection")
    command = task.get("arguments")
    if not task["installed"] or not isinstance(command, str) or len(command) > 32768:
        raise ValueError("Invalid connector task selection")
    arguments = _split_arguments(command)
    if (
        len(arguments) < 7
        or arguments[:3] != ["-m", "acp_gateway", "--config"]
        or arguments[4] != "--env-file"
        or arguments[6] != "connector"
        or not all(Path(arguments[i]).is_absolute() for i in (3, 5))
    ):
        raise ValueError("Task has no supported config/env selection; specify paths explicitly")
    return Path(arguments[3]), Path(arguments[5])


def _arguments_prefix(config):
    command = [] if getattr(sys, "frozen", False) else ["-m", "acp_gateway"]
    return [
        *command,
        "--config",
        str(config.config_path.resolve()),
        "--env-file",
        str(config.env_file.resolve()),
    ]


class SetupTask:
    """Snapshot only an idle owned task; fence setup and rollback by its control ID."""

    def __init__(self, config):
        self.config = config
        self.control_id = uuid.uuid4().hex
        snapshot = _request(
            "snapshot", argumentsPrefix=subprocess.list2cmdline(_arguments_prefix(config))
        )
        xml = snapshot.get("xml")
        if snapshot["installed"]:
            if not isinstance(xml, str) or not xml or len(xml.encode("utf-8")) > 131072:
                raise ValueError("Invalid connector task snapshot")
        elif xml is not None:
            raise ValueError("Invalid connector task snapshot")
        self.snapshot = {"installed": snapshot["installed"], "xml": xml}

    def apply(self, launch):
        from acp_gateway.service import install as install_service

        install_service(self.config, launch=launch, windows_setup=self)
        _request("enable", expectedControlId=self.control_id)

    def restore(self):
        _request("restore", expectedControlId=self.control_id, snapshot=self.snapshot)


def _background_executable():
    if sys.platform == "win32" and not getattr(sys, "frozen", False):
        windowless = Path(sys.executable).with_name("pythonw.exe")
        if not windowless.is_file():
            raise ValueError(
                "Windowless Python is missing; reinstall the uv tool with managed Python"
            )
        return str(windowless)
    return sys.executable


def install(config, launch, *, setup=None):
    log_file = config.settings.logging.file
    if log_file is None or not log_file.is_absolute() or not log_file.parent.is_dir():
        raise ValueError(
            "Windows connector service requires an absolute logging.file in an existing directory"
        )
    command = _arguments_prefix(config) + launch.arguments()
    control_id = setup.control_id if setup else uuid.uuid4().hex
    command += ["--control-id", control_id]
    executable = _background_executable()
    paths = [executable, *command, str(log_file)]
    if any(ord(char) < 32 or ord(char) == 127 for value in paths for char in value):
        raise ValueError("service paths cannot contain control characters")
    directory = config.settings.resolved_data_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = json.dumps(
        {
            "marker": "acpgw-connector-v1",
            "logFile": str(log_file),
            "controlDirectory": str(directory),
            "controlId": control_id,
        }
    )
    result = _request(
        "install",
        executable=executable,
        arguments=subprocess.list2cmdline(command),
        directory=str(config.config_path.resolve().parent),
        privateFiles=[str(config.config_path), str(config.env_file), str(launch.token_file)],
        logFile=str(log_file),
        logDirectory=str(log_file.parent),
        metadata=metadata,
        controlDirectory=str(directory),
        **({"guarded": True, "snapshot": setup.snapshot} if setup else {}),
    )
    print(f"Installed disabled logon task: {result['name']}")
    print("Enable and start: acpgw service --role connector enable")
    return 0


def control(action, *, role="gateway", force=False):
    if role != "connector":
        raise ValueError("Windows service control currently requires --role connector")
    actions = {"status", "logs", "enable", "disable", "start", "stop", "restart", "uninstall"}
    if action not in actions:
        raise ValueError("unknown connector task action")
    if force and action not in {"stop", "disable", "restart", "uninstall"}:
        raise ValueError("--force requires a connector task stop operation")
    result = _request(action, force=True) if force else _request(action)
    if action == "logs":
        from pathlib import Path

        from acp_gateway.log import redact_text

        path = Path(result["logFile"])
        if not path.is_file():
            print("No connector log file yet.")
        else:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 65536))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()[-50:]
            print(redact_text("\n".join(lines)))
    elif action == "status":
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Connector task: {action} completed; configuration, keys and data were kept.")
        if result.get("stopMode"):
            print(f"Stop mode: {result['stopMode']}")
    return 0
