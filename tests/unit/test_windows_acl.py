import asyncio
import base64
import json
import secrets
import subprocess
import threading
from types import SimpleNamespace

import pytest

from acp_gateway import windows_acl


def test_private_directory_creation_checks_parent_and_each_child(tmp_path, monkeypatch):
    calls = []

    def check(files, **kwargs):
        assert all(path.exists() for path in [*files, *kwargs.get("directories", [])])
        calls.append((files, kwargs))

    monkeypatch.setattr(windows_acl, "check_private_paths", check)
    target = tmp_path / "new parent" / "data"
    windows_acl.ensure_private_directory(target)
    assert calls[0][1]["directories"] == [tmp_path]
    assert [files for files, _ in calls[1:]] == [[target.parent], [target]]


def test_existing_directory_is_checked_without_changing_acl(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        windows_acl, "check_private_paths", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    windows_acl.ensure_private_directory(tmp_path)
    assert calls[-1][0] == ([tmp_path],)


def test_untrusted_parent_prevents_creation(tmp_path, monkeypatch):
    def reject(*args, **kwargs):
        raise ValueError("private ACL required")

    monkeypatch.setattr(windows_acl, "check_private_paths", reject)
    with pytest.raises(ValueError, match="private ACL"):
        windows_acl.ensure_private_directory(tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_fixed_script_checks_batched_paths_without_shell(monkeypatch, tmp_path):
    captured = {}

    def run(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout='{"private":true}')

    monkeypatch.setattr(windows_acl.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(windows_acl.subprocess, "run", run)
    paths = [tmp_path / 'token & $(throw "injection")', tmp_path / "record.json"]
    windows_acl.check_private_paths(paths, directories=[tmp_path])
    assert json.loads(captured["input"]) == {
        "files": list(map(str, paths)),
        "directories": [str(tmp_path)],
    }
    assert base64.b64decode(captured["argv"][-1]).decode("utf-16-le") == windows_acl.SCRIPT
    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "shell" not in captured
    assert captured["timeout"] == 10


@pytest.mark.parametrize("failure", ["missing", "start", "timeout", "denied", "json", "shape"])
def test_acl_verification_fails_closed_without_echoing_input(monkeypatch, failure):
    value = secrets.token_urlsafe(32)
    monkeypatch.setattr(
        windows_acl.shutil, "which", lambda name: None if failure == "missing" else name
    )

    def run(argv, **kwargs):
        if failure == "start":
            raise OSError(value)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10, output=value)
        return SimpleNamespace(
            returncode=1 if failure == "denied" else 0,
            stdout=value if failure == "json" else '{"private":false}',
            stderr=value,
        )

    monkeypatch.setattr(windows_acl.subprocess, "run", run)
    with pytest.raises(ValueError) as exc:
        windows_acl.check_private_paths([value])
    assert value not in str(exc.value)


async def test_discovery_preflight_does_not_block_event_loop(monkeypatch, tmp_path):
    from acp_gateway.cli import probe

    released = threading.Event()

    def read(*args):
        assert released.wait(timeout=2), "ACL preflight blocked the event loop"
        raise FileNotFoundError

    monkeypatch.setattr(probe, "read_endpoint", read)
    timer = asyncio.get_running_loop().call_later(0.02, released.set)
    try:
        rows = await probe.probe_hermes(tmp_path, role="desktop-serve")
    finally:
        timer.cancel()
        released.set()
    assert rows[0]["status"] == "missing"
