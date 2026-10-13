"""Checkout scripts must use uv ownership and wait for verified maintenance."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import checkout


@pytest.fixture
def harness(tmp_path, monkeypatch):
    entry = tmp_path / "tool bin" / "acpgw"
    entry.parent.mkdir()
    calls = []
    state = {"installed": False, "manager": "uv", "fail": None}
    monkeypatch.setattr(checkout.shutil, "which", lambda name: "/fake/uv")
    monkeypatch.setattr(checkout, "WINDOWS", False)

    def call(argv, *, capture=False):
        args = [str(arg) for arg in argv]
        calls.append(args)
        if args[1:] == ["tool", "dir", "--bin"]:
            return str(entry.parent)
        if args[1:] == ["version", "--json"]:
            return json.dumps({"manager": state["manager"]})
        if args[1:3] == ["tool", "install"]:
            entry.touch()
            state["installed"] = True
        if "--output-file" in args:
            Path(args[-1]).write_text("locked\n")
        if "update" in args:
            assert Path(args[args.index("--constraints") + 1]).read_text() == "locked\n"
            if state["fail"] is not None:
                raise checkout.CommandFailed(state["fail"])
        return ""

    monkeypatch.setattr(checkout, "call", call)
    return entry, calls, state


def test_install_then_update_never_reconfigures(harness, tmp_path):
    entry, calls, _ = harness
    config = tmp_path / "private config & data"
    assert checkout.main(["install", "--config-dir", str(config), "--role", "connector"]) == 0
    assert [str(entry), "setup", "--config-dir", str(config), "--role", "connector"] in calls
    installation = next(args for args in calls if args[1:3] == ["tool", "install"])
    assert installation[-2:] == ["--refresh-package", "acp-gateway"]
    calls.clear()
    assert checkout.main(["install", "--config-dir", str(config)]) == 0
    assert not any("setup" in args for args in calls)
    mutation = next(args for args in calls if "update" in args)
    assert mutation[:5] == [
        str(entry),
        "--config",
        str(config / "config.yaml"),
        "--env-file",
        str(config / ".env"),
    ]
    assert mutation[mutation.index("--from") + 1] == str(checkout.ROOT)
    assert not Path(mutation[-1]).exists()


@pytest.mark.parametrize("action", ["install", "update", "run"])
def test_refuse_foreign_installation(harness, action):
    entry, calls, state = harness
    entry.touch()
    state["manager"] = "pip"
    assert checkout.main([action]) == 1
    assert len(calls) == 2


def test_missing_update_does_not_install(harness):
    _, calls, _ = harness
    assert checkout.main(["update"]) == 1
    assert len(calls) == 1


def test_update_forwards_service_selection(harness):
    entry, calls, _ = harness
    entry.touch()
    assert checkout.main(["update", "--with-services", "--service-role", "connector"]) == 0
    mutation = next(args for args in calls if "update" in args)
    assert mutation[-3:] == ["--with-services", "--service-role", "connector"]


def test_failure_propagates_without_setup(harness):
    entry, calls, state = harness
    entry.touch()
    state["fail"] = 19
    assert checkout.main(["update"]) == 19
    assert not any("setup" in args for args in calls)


def test_existing_install_rejects_setup_arguments(harness):
    entry, calls, _ = harness
    entry.touch()
    assert checkout.main(["install", "--role", "connector"]) == 1
    assert len(calls) == 2


def test_windows_waits_for_worker_before_constraints_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(checkout, "WINDOWS", True)
    constraints = tmp_path / "requirements.txt"
    constraints.write_text("locked")
    calls = []
    plan = {
        "python": str(tmp_path / "base python"),
        "worker": str(tmp_path / "worker.py"),
        "manifest": str(tmp_path / "plan.json"),
    }

    def call(argv, **kwargs):
        args = [str(arg) for arg in argv]
        calls.append(args)
        assert constraints.exists()
        if "--handoff-file" in args:
            Path(args[-1]).write_text(json.dumps(plan))
        else:
            raise checkout.CommandFailed(23)
        return ""

    monkeypatch.setattr(checkout, "call", call)
    with pytest.raises(checkout.CommandFailed) as error:
        checkout.update(tmp_path / "acpgw.exe", constraints, None, tmp_path)
    assert error.value.code == 23
    assert calls[-1] == list(plan.values())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell wrappers")
def test_shell_wrappers_preserve_arguments_and_ignore_path_acpgw(tmp_path):
    repo = tmp_path / "checkout & space $literal"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in ("checkout.py", "install.sh", "run.sh", "update.sh"):
        shutil.copyfile(checkout.ROOT / "scripts" / name, scripts / name)
    bin_dir = tmp_path / "uv bin"
    bin_dir.mkdir()
    tool_bin = tmp_path / "owned tool"
    tool_bin.mkdir()
    log = tmp_path / "calls.jsonl"
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == 'run':
    index = next(i for i, value in enumerate(args) if value.endswith('checkout.py'))
    sys.exit(subprocess.call([sys.executable, *args[index:]]))
if args == ['tool', 'dir', '--bin']:
    print(os.environ['TEST_TOOL_BIN'])
elif args[0] == 'export':
    Path(args[-1]).write_text('locked')
elif args[:2] == ['tool', 'install']:
    import shutil
    shutil.copyfile(os.environ['TEST_ENTRY_TEMPLATE'], Path(os.environ['TEST_TOOL_BIN']) / 'acpgw')
    (Path(os.environ['TEST_TOOL_BIN']) / 'acpgw').chmod(0o700)
else:
    sys.exit(88)
"""
    )
    uv.chmod(0o700)
    template = tmp_path / "entry.py"
    template.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys
if sys.argv[1:] == ['version', '--json']:
    print(json.dumps({'manager': 'uv'}))
else:
    with open(os.environ['TEST_LOG'], 'a') as stream:
        stream.write(json.dumps(sys.argv[1:]) + '\\n')
"""
    )
    foreign = bin_dir / "acpgw"
    foreign.write_text("#!/bin/sh\nexit 99\n")
    foreign.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "TEST_TOOL_BIN": str(tool_bin),
        "TEST_LOG": str(log),
        "TEST_ENTRY_TEMPLATE": str(template),
    }
    literal = "$(touch SHOULD_NOT_EXIST); & `echo unsafe`"
    for script, args in (
        ("install", []),
        ("run", []),
        ("run", ["--config", literal, "status"]),
        ("update", []),
        ("install", []),
    ):
        result = subprocess.run(
            ["sh", str(scripts / f"{script}.sh"), *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[:3] == [["setup"], ["start"], ["--config", literal, "status"]]
    assert len(records) == 5
    assert all(args[args.index("--from") + 1] == str(repo) for args in records[3:])
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
