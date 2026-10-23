"""Stdlib-only ACL support for the independent copied updater runtime.

Explicit compatibility exception: update_runner copies this as windows_acl.py
for the base interpreter, which must outlive the replaced application venv and
has no pywin32. Preserve the previous bounded PowerShell verifier and policy.
Application/IPC callers use acp_gateway.windows_acl's native child instead;
this module is neither a fallback nor part of that hot path.
"""

import base64
import json
import shutil
import subprocess
from pathlib import Path

# Same conservative ACL policy as the native-tested connector service preflight.
# Inputs are paths only. Token bytes never enter PowerShell or its diagnostics.
SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $r = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowed = @($sid, 'S-1-5-18', 'S-1-5-32-544')
    function Assert-Private($path, $directory) {
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse point'
        }
        $acl = Get-Acl -LiteralPath $path
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $allowed) { throw 'Untrusted owner' }
        # OWNER RIGHTS applies to this object's owner, already checked above.
        # CPython's Windows mkdir(mode=0o700) uses this SID in its private DACL.
        $readers = $allowed + @('S-1-3-4')
        if ($acl.GetSecurityDescriptorSddlForm('Access') -match 'NO_ACCESS_CONTROL') {
            throw 'Null DACL'
        }
        $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
        foreach ($rule in $rules) {
            if ($rule.AccessControlType -ne 'Allow') { continue }
            if ($rule.IdentityReference.Value -in $readers) { continue }
            $rights = [int]$rule.FileSystemRights
            # Write | DeleteSubdirectoriesAndFiles | Delete | ChangePermissions | TakeOwnership
            if ((-not $directory -and $rights -ne 0) -or
                ($directory -and ($rights -band 852310) -ne 0)) { throw 'Public ACL' }
        }
    }
    foreach ($path in $r.files) { Assert-Private $path $false }
    foreach ($path in $r.directories) { Assert-Private $path $true }
    @{private=$true} | ConvertTo-Json -Compress
} catch {
    # Discard input-dependent .NET messages, which may contain paths or record values.
    [Console]::Error.WriteLine('Private Windows ACL verification failed')
    exit 1
}
"""


def check_private_paths(files, *, directories=(), purpose="Hermes discovery"):
    executable = shutil.which("powershell.exe")
    if executable is None:
        raise ValueError("Windows PowerShell is required to verify private discovery ACLs")
    payload = {
        "files": [str(path) for path in files],
        "directories": [str(path) for path in directories],
    }
    try:
        result = subprocess.run(  # noqa: S603 — fixed script and JSON stdin; no shell
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii"),
            ],
            input=json.dumps(payload),
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Windows discovery ACL verification timed out") from None
    except OSError:
        raise ValueError("Windows discovery ACL verifier could not be started") from None
    if result.returncode:
        raise ValueError(f"{purpose} files must have private Windows ACLs")
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or response.get("private") is not True:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(
            "Windows discovery ACL verification returned an invalid response"
        ) from None


def ensure_private_directory(directory: Path) -> None:
    """Create only missing directories; validate existing ACLs without repairing them."""
    if not directory.is_absolute():
        raise ValueError("Private Windows directories require absolute paths")
    missing = []
    current = directory
    while not current.exists():
        if current.is_symlink():
            raise ValueError("Private Windows directories cannot be links")
        missing.append(current)
        current = current.parent
    check_private_paths([], directories=[current], purpose="Gateway setup")
    if not missing:
        # Owned runtime/config directories also contain future secret files:
        # reject public read inheritance, not just public write access.
        check_private_paths([current], purpose="Gateway setup")
    for path in reversed(missing):
        path.mkdir(mode=0o700)
        check_private_paths([path], purpose="Gateway setup")
