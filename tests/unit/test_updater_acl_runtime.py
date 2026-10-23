"""Copied-runtime checks only; no worker loop, services or installations."""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from acp_gateway import update_runner


def test_runtime_current_tracks_independent_acl_source(tmp_path):
    update_runner.copy_runtime(tmp_path)
    assert update_runner.runtime_current(tmp_path)
    sources = {name: source for source, name in update_runner.runtime_sources()}
    assert sources["windows_acl.py"].__name__ == "acp_gateway.updater_windows_acl"
    target = tmp_path / "windows_acl.py"
    target.write_bytes(target.read_bytes() + b"\n# stale\n")
    assert not update_runner.runtime_current(tmp_path)
    update_runner.copy_runtime(tmp_path)
    assert update_runner.runtime_current(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows base interpreter and real ACLs")
def test_policy_load_under_isolated_base_interpreter(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    update_runner.copy_runtime(runtime)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    spool = private / "spool"
    spool.mkdir(mode=0o700)
    policy = private / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "version": 1,
                "backend": "uv",
                "role": "connector",
                "target": "owned",
                **{
                    key: str(private / key) for key in ("entry", "config", "env_file", "result_dir")
                },
                "spool_dir": str(spool),
            }
        ),
        encoding="utf-8",
    )
    # -I ignores the poisoned application path. Only copied helpers are admitted;
    # importing worker does not execute its main/update loop.
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "absent-application")}
    code = (
        "import sys, importlib.util; assert sys.flags.isolated; "
        "assert importlib.util.find_spec('win32api') is None; "
        "assert importlib.util.find_spec('acp_gateway') is None; "
        "sys.path.insert(0, sys.argv[1]); import worker; "
        "assert worker.load_policy(sys.argv[2])['target'] == 'owned'; "
        "print('isolated base policy ACL accepted')"
    )
    command = [sys._base_executable, "-I", "-c", code, str(runtime), str(policy)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40, env=env)
    assert result.returncode == 0, result.stderr
    assert "isolated base policy ACL accepted" in result.stdout

    # Mutation is restricted to this test-owned file, never an installation.
    import win32security

    descriptor = win32security.GetFileSecurity(str(policy), win32security.DACL_SECURITY_INFORMATION)
    dacl = descriptor.GetSecurityDescriptorDacl()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION, 1, win32security.ConvertStringSidToSid("S-1-1-0")
    )
    win32security.SetNamedSecurityInfo(
        str(policy),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
    denied = subprocess.run(command, capture_output=True, text=True, timeout=40, env=env)
    assert denied.returncode != 0
    assert "Updater policy files must have private Windows ACLs" in denied.stderr


def test_copied_acl_timeout_is_fail_closed(tmp_path, monkeypatch):
    update_runner.copy_runtime(tmp_path)
    spec = importlib.util.spec_from_file_location("copied_updater_acl", tmp_path / "windows_acl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr("shutil.which", lambda _name: "powershell.exe")

    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="Windows discovery ACL verification timed out"):
        module.check_private_paths([tmp_path / "owned"])
