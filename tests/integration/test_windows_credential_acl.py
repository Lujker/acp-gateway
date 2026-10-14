"""Disposable native Windows credential boundaries; no real enrollment/services."""

import base64
import json
import os
import shutil
import subprocess
from contextlib import closing

import pytest

from acp_gateway.connectors.control import read_credential
from acp_gateway.storage import Store

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows ACL semantics")

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
$r = [Console]::In.ReadToEnd() | ConvertFrom-Json
if ($r.action -eq 'read') {
    $acl = Get-Acl -LiteralPath $r.path
    $sid = New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'Read', 'Allow')
    $acl.AddAccessRule($rule)
    (Get-Item -LiteralPath $r.path -Force).SetAccessControl($acl)
} elseif ($r.action -eq 'junction') {
    New-Item -ItemType Junction -Path $r.path -Target $r.target | Out-Null
}
"""


def change(action, path, target=None):
    result = subprocess.run(
        [
            shutil.which("powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii"),
        ],
        input=json.dumps({"action": action, "path": str(path), "target": str(target)}),
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def private(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return root


def test_native_private_export_and_read(private):
    with closing(Store.open(":memory:")) as store:
        key = private / "home.key"
        store.computers.issue("home", key, display_name="Home")
        assert store.computers.authenticate("home", read_credential(key))


def test_native_public_file_is_rejected_by_foreground_reader(private):
    with closing(Store.open(":memory:")) as store:
        key = private / "home.key"
        store.computers.issue("home", key, display_name="Home")
        change("read", key)
        with pytest.raises(ValueError, match="private Windows ACLs"):
            read_credential(key)


def test_native_public_parent_refuses_rotation_before_export(private):
    with closing(Store.open(":memory:")) as store:
        original = private / "home.key"
        store.computers.issue("home", original, display_name="Home")
        token = read_credential(original)
        public = private / "public"
        public.mkdir(mode=0o700)
        change("read", public)
        target = public / "rejected.key"
        with pytest.raises(ValueError, match="private Windows ACLs"):
            store.computers.issue("home", target)
        assert not target.exists()
        assert store.computers.authenticate("home", token)
        assert store.computers.list()[0]["generation"] == 1


def test_native_junction_parent_is_rejected(private):
    with closing(Store.open(":memory:")) as store:
        target = private / "real"
        target.mkdir(mode=0o700)
        key = target / "home.key"
        store.computers.issue("home", key, display_name="Home")
        junction = private / "junction"
        change("junction", junction, target)
        with pytest.raises(ValueError, match="private Windows ACLs"):
            read_credential(junction / "home.key")
        with pytest.raises(ValueError, match="private Windows ACLs"):
            store.computers.issue("other", junction / "other.key", display_name="Other")
        assert not (target / "other.key").exists()
