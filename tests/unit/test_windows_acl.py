import asyncio
import json
import secrets
import subprocess
import sys
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


def test_fixed_native_child_checks_batched_paths_without_shell(monkeypatch, tmp_path):
    import multiprocessing

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    captured = {}

    class Connection:
        def send_bytes(self, data):
            captured["reply"] = data

        def poll(self, timeout):
            captured["timeout"] = timeout
            return True

        def recv_bytes(self, maxlength):
            assert maxlength == 7
            return b"private"

        def close(self):
            pass

    class Process:
        exitcode = 0

        def __init__(self, *, target, args, daemon):
            captured.update(target=target, args=args, daemon=daemon)

        def start(self):
            pass

        def join(self, timeout):
            assert timeout <= 1

        def is_alive(self):
            return False

        def close(self):
            pass

    def context(method):
        assert method == "spawn"
        return SimpleNamespace(Pipe=lambda duplex: (Connection(), Connection()), Process=Process)

    def forbidden(*args, **kwargs):
        pytest.fail("PowerShell must not be started")

    monkeypatch.setattr(multiprocessing, "get_context", context)
    monkeypatch.setattr(subprocess, "run", forbidden)
    paths = [tmp_path / 'token & $(throw "injection")', tmp_path / "record.json"]
    windows_acl.check_private_paths(paths, directories=[tmp_path])
    assert captured["target"] is windows_acl._verify_child
    assert captured["args"][0] == {"files": list(map(str, paths)), "directories": [str(tmp_path)]}
    assert captured["daemon"] is True
    assert captured["timeout"] <= 10


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


def test_interpreter_child_has_fixed_entry_and_timeout(monkeypatch, tmp_path):
    captured = {}

    def run(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout='{"private":true}')

    monkeypatch.setattr(subprocess, "run", run)
    windows_acl.check_private_paths([tmp_path / 'a & $(throw "injection")'], directories=[tmp_path])
    assert captured["argv"] == [
        sys.executable,
        "-I",
        str(windows_acl.Path(windows_acl.__file__).absolute()),
    ]
    assert captured["timeout"] == 10
    assert "shell" not in captured
    assert json.loads(captured["input"])["directories"] == [str(tmp_path)]


@pytest.mark.parametrize("failure", ["start", "timeout", "denied", "json", "shape"])
def test_native_verification_fails_closed_without_echoing_input(monkeypatch, failure):
    value = secrets.token_urlsafe(32)

    def run(argv, **kwargs):
        if failure == "start":
            raise OSError(value)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10, output=value)
        return SimpleNamespace(
            returncode=1 if failure == "denied" else 0,
            stdout=value if failure == "json" else '{"private":false}',
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError) as exc:
        windows_acl.check_private_paths([value])
    assert value not in str(exc.value)


@pytest.mark.parametrize("denied", [False, True])
def test_standalone_verifier_response_is_sanitized(monkeypatch, capsys, denied):
    import io

    value = secrets.token_urlsafe(32)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"files": [value], "directories": []}))
    )

    def verify(payload):
        assert payload["files"] == [value]
        if denied:
            raise OSError(value)

    monkeypatch.setattr(windows_acl, "_verify_paths", verify)
    assert windows_acl._verifier_main() == int(denied)
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == ("" if denied else '{"private":true}\n')


def test_interpreter_child_disables_busy_cursor_feedback(monkeypatch, tmp_path):
    captured = {}

    class StartupInfo:
        def __init__(self):
            self.dwFlags = 0

    def run(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"private":true}')

    monkeypatch.setattr(subprocess, "STARTUPINFO", StartupInfo, raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    windows_acl.check_private_paths([], directories=[tmp_path])
    # STARTF_FORCEOFFFEEDBACK: no "app starting" cursor per pythonw child.
    assert captured["startupinfo"].dwFlags & 0x80
