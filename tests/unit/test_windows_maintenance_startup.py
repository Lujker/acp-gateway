"""Execute the maintenance script in native PowerShell, never a real scheduler."""

import json
import secrets
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from acp_gateway import windows_service as windows


@pytest.fixture
def native_request(monkeypatch):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        candidate = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        if candidate.is_file():
            powershell = str(candidate)
    if powershell is None:
        pytest.skip("native Windows PowerShell unavailable")
    original_run = subprocess.run
    monkeypatch.setattr(windows.shutil, "which", lambda name: powershell)

    def request(scenario="delayed", **values):
        # Replace only the scheduler connection: all production ownership, ACL,
        # control-record validation and response handling still execute.
        source = windows.SCRIPT.replace(
            "    $scheduler = New-Object -ComObject Schedule.Service\n"
            "    $scheduler.Connect()\n"
            "    $folder = $scheduler.GetFolder('\\')",
            HARNESS,
        )
        assert source != windows.SCRIPT
        # Keep failure tests fast while exercising the real deadline condition.
        source = source.replace(".AddSeconds(20)", ".AddMilliseconds(250)")
        source += (
            "\nfinally { if ($testDirectory) {"
            " Remove-Item -LiteralPath $testDirectory -Recurse -Force } }\n"
        )
        monkeypatch.setattr(windows, "SCRIPT", source)
        return windows._request(
            "maintenance-state", scenario=scenario, argumentsPrefix="fixture", **values
        )

    # Explicitly call the real subprocess implementation, not a scheduler mock.
    monkeypatch.setattr(windows.subprocess, "run", original_run)
    return request


HARNESS = r"""
    $testDirectory = [IO.Path]::Combine(
        $env:LOCALAPPDATA, 'hermes\cache\scratch', ('maintenance-test-' + [guid]::NewGuid()))
    $null = [IO.Directory]::CreateDirectory($testDirectory)
    $acl = Get-Acl -LiteralPath $testDirectory
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($existing in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($existing) }
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        ([Security.Principal.WindowsIdentity]::GetCurrent().User),
        'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    ([IO.DirectoryInfo]$testDirectory).SetAccessControl($acl)
    $script:testRuntime = [IO.Path]::Combine($testDirectory, 'connector-control.json')
    $script:testMeta = @{marker='acpgw-connector-v1'; controlDirectory=$testDirectory;
        controlId=('a' * 32)}
    $script:testAction = [pscustomobject]@{Path=$r.executable; Arguments='fixture connector'}
    $actions = [pscustomobject]@{Count=1}
    $actions | Add-Member ScriptMethod Item { param($index) $script:testAction }
    $script:testTask = [pscustomobject]@{
        State=4; Enabled=$true; Xml='original';
        Definition=[pscustomobject]@{
            RegistrationInfo=[pscustomobject]@{Description=($script:testMeta | ConvertTo-Json)};
            Principal=[pscustomobject]@{UserId=$sid; LogonType=3; RunLevel=0}; Actions=$actions}}
    $script:testInstances = if ($r.scenario -in @('starting-instance', 'no-task-instance')) {
        0
    } else {1}
    $script:testTask | Add-Member ScriptMethod GetInstances { param($flags)
        [pscustomobject]@{Count=$(if ($script:testTask.State -eq 4) {
            $script:testInstances
        } else {0})} }
    $folder = [pscustomobject]@{}
    $folder | Add-Member ScriptMethod GetTask { param($name) $script:testTask }
    $scheduler = [pscustomobject]@{}
    $scheduler | Add-Member ScriptMethod NewTask { param($flags)
        [pscustomobject]@{XmlText='normalized'; Settings=[pscustomobject]@{Enabled=$false}} }
    function Publish-TestRecord {
        $body = @{version=1; controlId=('a' * 32); instance=('b' * 32)}
        if ($r.scenario -eq 'foreign') { $body.controlId = 'c' * 32 }
        if ($r.scenario -eq 'invalid-instance') { $body.instance = 'bad' }
        $text = $body | ConvertTo-Json -Compress
        if ($r.scenario -eq 'malformed') { $text = '{' }
        if ($r.scenario -eq 'null') { $text = 'null' }
        if ($r.scenario -eq 'oversized') { $text = 'x' * 1025 }
        [IO.File]::WriteAllText($script:testRuntime, $text)
        if ($r.scenario -eq 'public') {
            $acl = Get-Acl -LiteralPath $script:testRuntime
            $world = New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
                $world, 'Read', 'Allow')))
            Set-Acl -LiteralPath $script:testRuntime -AclObject $acl
        }
    }
    if ($r.scenario -notin @(
        'delayed', 'starting-instance', 'missing', 'exited', 'multiple', 'changed-definition')) {
        Publish-TestRecord
    }
    function Start-Sleep { param($Milliseconds)
        if ($r.scenario -eq 'changed-definition') { $script:testTask.Xml = 'replacement' }
        elseif ($r.scenario -in @('delayed', 'starting-instance')) {
            $script:testInstances = 1
            Publish-TestRecord
        }
        elseif ($r.scenario -eq 'exited') { $script:testTask.State = 3 }
        elseif ($r.scenario -eq 'multiple') {
            $script:testTask | Add-Member ScriptMethod GetInstances {
                param($flags) [pscustomobject]@{Count=2} } -Force
        }
        else { [Threading.Thread]::Sleep(20) }
    }
"""


@pytest.mark.parametrize("scenario", ["delayed", "starting-instance"])
def test_maintenance_waits_for_private_matching_startup_record(native_request, scenario):
    result = native_request(scenario)
    assert result["state"] == 4
    assert result["instance"] == "b" * 32
    assert result["controlId"] == "a" * 32
    assert result["definition"] == "original"
    assert result["enabled"] is True


@pytest.mark.parametrize(
    "scenario,message",
    [
        ("missing", "inspect connector logs and retry"),
        ("exited", "changed during startup"),
        ("multiple", "changed during startup"),
        ("no-task-instance", "changed during startup"),
        ("changed-definition", "changed during startup"),
        ("foreign", "Invalid task instance"),
        ("invalid-instance", "Invalid task instance"),
        ("malformed", None),
        ("null", "Invalid control record"),
        ("oversized", "Invalid control record"),
        ("public", "ACL is not private"),
    ],
)
def test_maintenance_refuses_unsafe_or_unready_runtime(native_request, scenario, message):
    with pytest.raises(ValueError, match=message) as exc:
        native_request(scenario)
    assert "CLIXML" not in str(exc.value)
    assert "<Objs" not in str(exc.value)


@pytest.mark.parametrize(
    "values,message",
    [
        ({"expectedControlId": "c" * 32}, "changed during setup"),
        ({"expectedDefinition": "different"}, "definition changed"),
    ],
)
def test_maintenance_preserves_initial_generation_and_definition_guards(
    native_request, values, message
):
    with pytest.raises(ValueError, match=message):
        native_request("delayed", **values)


@pytest.mark.parametrize("structured", [True, False])
def test_request_never_displays_clixml(monkeypatch, structured):
    from acp_gateway.log import register_secret

    secret = secrets.token_urlsafe(32)
    register_secret(secret)
    monkeypatch.setattr(windows.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(
        windows.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"error": "Startup unavailable: " + secret}) if structured else "",
            stderr='#< CLIXML\n<Objs><S S="Error">raw private source</S></Objs>',
        ),
    )
    with pytest.raises(ValueError) as exc:
        windows._request("maintenance-state")
    message = str(exc.value)
    assert "CLIXML" not in message and "<Objs" not in message
    assert "raw private source" not in message and secret not in message
    assert ("Startup unavailable" if structured else "inspect connector logs") in message
