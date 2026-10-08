<# Exercise the real Windows Task Scheduler with a temporary managed task. #>
param(
    [Parameter(Mandatory = $true)][string]$Distro,
    [Parameter(Mandatory = $true)][string]$LinuxUser
)
$ErrorActionPreference = 'Stop'
$helper = Join-Path $PSScriptRoot '..\deploy\windows\wsl-task.ps1'
$taskName = 'ACP Gateway WSL smoke ' + [Guid]::NewGuid().ToString('N')

function Assert-State([string]$Expected) {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        if ([string]$task.State -eq $Expected) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    throw "Expected $Expected, got $($task.State); LastTaskResult=$($info.LastTaskResult)."
}

try {
    & $helper -Action install -Distro $Distro -LinuxUser $LinuxUser -TaskName $taskName
    Assert-State 'Disabled'
    $task = Get-ScheduledTask -TaskName $taskName
    if ($task.Settings.ExecutionTimeLimit -ne 'PT0S') { throw 'Task has a time limit.' }
    if ($task.Principal.LogonType -ne 'Interactive') { throw 'Expected interactive logon.' }
    if (@($task.Triggers).Count -ne 1 -or $task.Triggers[0].CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger') {
        throw 'Expected a single logon trigger.'
    }
    & $helper -Action enable -TaskName $taskName
    Start-Sleep -Seconds 3
    Assert-State 'Running'
    & $helper -Action status -TaskName $taskName
    & $helper -Action disable -TaskName $taskName
    Assert-State 'Disabled'
    & $helper -Action enable -TaskName $taskName
    Start-Sleep -Seconds 3
    Assert-State 'Running'
    & $helper -Action uninstall -TaskName $taskName
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        throw 'Task survived uninstall.'
    }
    Write-Output 'WSL task smoke passed: install, enable/start, status, disable/stop, re-enable, uninstall.'
} finally {
    # Only remove the uniquely named task if it was successfully registered.
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        & $helper -Action uninstall -TaskName $taskName
    }
}
