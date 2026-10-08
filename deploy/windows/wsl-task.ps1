<#
Manage the Windows logon task that keeps a systemd-enabled WSL distro alive.
Gateway autostart itself is controlled by acpgw service enable/disable in WSL.
#>
param(
    [ValidateSet('install', 'enable', 'disable', 'status', 'uninstall')]
    [string]$Action = 'status',
    [string]$Distro,
    [string]$LinuxUser,
    [string]$TaskName = 'ACP Gateway WSL'
)
$ErrorActionPreference = 'Stop'
$marker = 'Managed by ACP Gateway: WSL keepalive at Windows logon.'
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -ne $marker) {
    throw "Refusing to change an unmanaged scheduled task: $TaskName"
}
switch ($Action) {
    'install' {
        if (-not $Distro -or -not $LinuxUser) {
            throw 'install requires -Distro and -LinuxUser.'
        }
        if ($Distro -notmatch '^[\p{L}\p{N}][\p{L}\p{N}._-]*$' -or $LinuxUser -notmatch '^[a-z_][a-z0-9_-]*\$?$') {
            throw 'Distro must contain only letters, digits, dots, underscores or hyphens; LinuxUser must be a valid Linux username.'
        }
        $windowsUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        # Some WSL versions treat quotes in the scheduled action's argument
        # string as part of the distro name. Restrict names to single safe
        # tokens and pass them without quotes; no shell is used.
        $exe = Join-Path $env:SystemRoot 'System32\wsl.exe'
        $arguments = "--distribution $Distro --user $LinuxUser --exec /bin/sleep infinity"
        $taskAction = New-ScheduledTaskAction -Execute $exe -Argument $arguments
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $windowsUser
        $principal = New-ScheduledTaskPrincipal -UserId $windowsUser -LogonType Interactive
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -Disable
        Register-ScheduledTask -TaskName $TaskName -Description $marker -Action $taskAction `
            -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Write-Output "Installed logon task: $TaskName. Run this script with -Action enable to start now."
    }
    'enable' {
        if (-not $existing) { throw 'Install the task first.' }
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
    }
    'disable' {
        if (-not $existing) { throw 'The task is not installed.' }
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Stop-ScheduledTask -TaskName $TaskName
    }
    'status' {
        if ($existing) {
            $existing | Select-Object TaskName, State, Description
            Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime, LastTaskResult
        } else { Write-Output 'WSL logon task is not installed.' }
    }
    'uninstall' {
        if ($existing) {
            Stop-ScheduledTask -TaskName $TaskName
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        Write-Output 'WSL logon task removed. Gateway configuration and data were kept.'
    }
}
