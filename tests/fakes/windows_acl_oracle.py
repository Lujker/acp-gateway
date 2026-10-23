"""Frozen pre-ACL-backend-A PowerShell policy oracle; tests only."""

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
