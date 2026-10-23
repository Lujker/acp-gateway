"""Harmless final-boundary canaries: never connect to an OS manager."""

import os
import subprocess
import sys

import pytest

import external_effect_guard as guard
from external_effect_guard import ExternalEffectDenied

pytestmark = pytest.mark.skipif(not guard.INSTALLED, reason="strict isolation runner is not active")


class CanaryReached(Exception):
    pass


@pytest.fixture
def process_canary(monkeypatch):
    calls = []

    def canary(*args, **kwargs):
        calls.append(True)
        raise CanaryReached("harmless OS process boundary canary")

    if sys.platform == "win32":
        import _winapi

        monkeypatch.setattr(_winapi, "CreateProcess", canary)
    else:
        monkeypatch.setattr(subprocess, "_fork_exec", canary)
        monkeypatch.setattr(subprocess, "_USE_POSIX_SPAWN", False)
    return calls


def test_encoded_scheduler_stops_before_os_boundary(monkeypatch, process_canary):
    from acp_gateway import windows_service

    monkeypatch.setattr(windows_service.shutil, "which", lambda name: "powershell.exe")
    try:
        windows_service._request("status")
    except BaseException as exc:
        assert type(exc).__name__ == "ExternalEffectDenied"
    else:
        pytest.fail("scheduler request was not denied")
    assert process_canary == []


@pytest.mark.parametrize(
    "event",
    [
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "os.startfile",
        "os.startfile/2",
        "os.kill",
        "os.killpg",
        "_winapi.CreateProcess",
        "ctypes.dlopen",
        "ctypes.dlsym",
    ],
)
def test_alternate_os_boundaries_are_denied(event):
    # Emit only the audit notification: no OS API is called, even in RED.
    with pytest.raises(ExternalEffectDenied):
        sys.audit(event, "synthetic-not-an-operation")


@pytest.mark.parametrize(
    "argv",
    [
        ["schtasks.exe", "/Query"],
        ["sc.exe", "query"],
        ["net.exe", "start"],
        ["systemctl", "--user", "status"],
        ["launchctl", "list"],
        ["powershell.exe", "-EncodedCommand", "synthetic-encoded-payload"],
        ["uv", "tool", "upgrade", "acp-gateway"],
    ],
)
def test_all_unapproved_processes_stop_before_canary(argv, process_canary):
    with pytest.raises(ExternalEffectDenied):
        subprocess.run(argv, check=False)
    assert process_canary == []


@pytest.mark.parametrize(
    "name", ["win32com.client", "pythoncom", "comtypes.client", "win32service"]
)
def test_direct_native_manager_imports_are_denied(name):
    import importlib

    with pytest.raises(ExternalEffectDenied):
        importlib.import_module(name)


def test_explicit_disposable_probe_runs_only_fixed_helper(tmp_path):
    import external_effect_guard as guard

    assert callable(getattr(guard, "ScopedEffects", None)), "scoped disposable fixture API missing"
    scope = guard.ScopedEffects(tmp_path)
    result = scope.probe("scheduler", input='{"action":"status"}')
    assert result.returncode == 0
    assert result.stdout.strip() == '{"installed": false}'
    with pytest.raises(ExternalEffectDenied):
        subprocess.run([sys.executable, "-c", "raise SystemExit(0)"], check=False)


def test_readonly_acl_optin_is_fixed_and_fixture_scoped(tmp_path):
    import external_effect_guard as guard

    scope = guard.ScopedEffects(tmp_path)
    assert callable(getattr(scope, "check_private_paths", None)), "read-only ACL opt-in missing"
    with pytest.raises(ExternalEffectDenied):
        scope.check_private_paths([tmp_path.parent])
    if sys.platform == "win32":
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        path = private / "empty-fixture"
        path.write_text("", encoding="utf-8")
        scope.check_private_paths([path])


def test_scope_rejects_unowned_roots_and_arbitrary_processes(tmp_path, process_canary):
    import external_effect_guard as guard

    with pytest.raises(ExternalEffectDenied):
        guard.ScopedEffects(tmp_path.parent)
    scope = guard.ScopedEffects(tmp_path)
    with pytest.raises(ExternalEffectDenied):
        scope._run(["schtasks.exe", "/Query"], input="")
    assert process_canary == []


def test_environment_paths_are_fixture_owned(tmp_path):
    from pathlib import Path

    for name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
        "TMP",
        "TEMP",
    ):
        assert Path(os.environ[name]).is_relative_to(tmp_path), name


def test_production_python_request_with_disposable_fake(monkeypatch, audited_external_effects):
    from types import SimpleNamespace

    from acp_gateway import windows_service
    from acp_gateway.powershell import encoded_script

    def fake_manager(argv, **kwargs):
        assert argv == [
            "disposable-manager",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded_script(windows_service.SCRIPT),
        ]
        return audited_external_effects.probe("scheduler", input=kwargs["input"])

    monkeypatch.setattr(
        windows_service, "shutil", SimpleNamespace(which=lambda _: "disposable-manager")
    )
    monkeypatch.setattr(windows_service, "subprocess", SimpleNamespace(run=fake_manager))
    assert windows_service._request("status") == {"installed": False}


def test_denial_is_not_swallowed_by_ordinary_exception_handlers():
    assert not issubclass(ExternalEffectDenied, Exception)


def test_collection_and_thread_boundaries_are_fail_closed(process_canary):
    import concurrent.futures

    import external_effect_guard as guard

    token = guard._ROOT.set(None)
    try:
        with pytest.raises(ExternalEffectDenied):
            subprocess.run(["schtasks.exe", "/Query"], check=False)
    finally:
        guard._ROOT.reset(token)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(subprocess.run, ["sc.exe", "query"], check=False)
        with pytest.raises(ExternalEffectDenied):
            future.result()
    assert process_canary == []


def test_scope_revocation_and_exception_cleanup(tmp_path, audited_external_effects, monkeypatch):
    import external_effect_guard as guard

    with guard.fixture_root(tmp_path / "home"), pytest.raises(ExternalEffectDenied):
        audited_external_effects.probe("scheduler", input='{"action":"status"}')
    with monkeypatch.context() as patch:
        patch.setattr(
            guard.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(ValueError("synthetic"))
        )
        with pytest.raises(ValueError, match="synthetic"):
            audited_external_effects.probe("scheduler", input='{"action":"status"}')
    assert guard._PERMIT.get() is None


def test_updater_encoded_script_is_denied_at_final_boundary(tmp_path, process_canary):
    # Compile the exact updater boundary without importing its unrelated CLI/Click
    # native-console dependencies. No production checks or script are rewritten.
    import ast
    import hashlib
    import json
    from pathlib import Path
    from types import SimpleNamespace

    import acp_gateway

    source = Path(acp_gateway.__file__).parent / "updater_service.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"_windows", "command", "identity", "runtime_path"}
    nodes = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "WINDOWS_SCRIPT"
                for target in node.targets
            )
        )
    ]
    namespace = {
        "os": os,
        "sys": sys,
        "subprocess": subprocess,
        "hashlib": hashlib,
        "json": json,
        "Path": Path,
        "shutil": SimpleNamespace(which=lambda _: "powershell.exe"),
    }
    exec(  # noqa: S102 - exact local production AST, never user script/input
        compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace
    )
    with pytest.raises(ExternalEffectDenied):
        namespace["_windows"]("status", tmp_path / "disposable-policy.json")
    assert process_canary == []


async def test_async_subprocess_is_denied_before_os_boundary(process_canary):
    import asyncio

    with pytest.raises(ExternalEffectDenied):
        await asyncio.create_subprocess_exec("schtasks.exe", "/Query")
    assert process_canary == []


def test_direct_posix_process_boundary_is_denied():
    if os.name != "nt":
        import _posixsubprocess

        # Zero arguments cannot create a process even in the unguarded RED case.
        with pytest.raises(ExternalEffectDenied):
            _posixsubprocess.fork_exec()


def test_direct_windows_termination_is_denied():
    if os.name == "nt":
        import _winapi

        # Invalid arity is harmless with the original implementation in RED.
        with pytest.raises(ExternalEffectDenied):
            _winapi.TerminateProcess()


def test_native_console_import_does_not_grant_manager_symbols():
    if os.name == "nt":
        import ctypes

        import click

        assert callable(click.echo)
        with pytest.raises(ExternalEffectDenied):
            _ = ctypes.windll.kernel32.CreateProcessW
        with pytest.raises(ExternalEffectDenied):
            ctypes.WinDLL("ole32")


def test_disposable_child_disables_site_initialization(monkeypatch, audited_external_effects):
    import external_effect_guard as guard

    original = subprocess.run

    def checked(argv, **kwargs):
        assert argv[1:3] == ["-I", "-S"], "helper must not import sitecustomize/plugins"
        return original(argv, **kwargs)

    monkeypatch.setattr(guard.subprocess, "run", checked)
    assert audited_external_effects.probe("scheduler", input='{"action":"status"}').returncode == 0


def test_disposable_interpreter_is_pinned_before_feature_monkeypatches(
    monkeypatch, audited_external_effects
):
    import external_effect_guard as guard

    original_executable = sys.executable
    original_run = subprocess.run

    def checked(argv, **kwargs):
        assert argv[0] == original_executable, (
            "feature monkeypatch must not authorize an executable"
        )
        return original_run(argv, **kwargs)

    monkeypatch.setattr(guard.sys, "executable", "schtasks.exe")
    monkeypatch.setattr(guard.subprocess, "run", checked)
    assert audited_external_effects.probe("scheduler", input='{"action":"status"}').returncode == 0


def test_acl_system_executable_is_pinned_before_environment_changes(
    tmp_path, monkeypatch, audited_external_effects, process_canary
):
    if os.name == "nt":
        import external_effect_guard as guard

        fake_root = tmp_path / "fake-windows"
        executable = fake_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"")
        target = tmp_path / "empty-fixture"
        target.write_bytes(b"")
        monkeypatch.setenv("SYSTEMROOT", str(fake_root))
        monkeypatch.setattr(guard.shutil, "which", lambda _: str(executable))
        with pytest.raises(ExternalEffectDenied):
            audited_external_effects.check_private_paths([target])
        assert process_canary == []


def test_installed_file_lock_dependency_works_under_the_guard(tmp_path):
    """The gate guards external effects, not installed locking dependencies."""
    import filelock

    lock = filelock.FileLock(tmp_path / "owned.lock")
    with lock:
        assert lock.is_locked


def test_production_file_lease_on_fixture_owned_path_works(tmp_path):
    from acp_gateway import maintenance_worker

    with maintenance_worker.lease(tmp_path / "owned.lease", exclusive=True) as descriptor:
        assert descriptor >= 0


def test_unlisted_native_entry_points_stay_denied():
    if os.name == "nt":
        import ctypes

        with pytest.raises(ExternalEffectDenied):
            _ = ctypes.WinDLL("kernel32").CreateProcessW


def test_unowned_process_signal_stays_denied(tmp_path, monkeypatch):
    import external_effect_guard as guard

    def stub(argv, **kwargs):
        # Synthetic audit notification only: never a real signal.
        sys.audit("os.kill", os.getpid(), 0)

    monkeypatch.setattr(guard.subprocess, "run", stub)
    scope = guard.ScopedEffects(tmp_path)
    with pytest.raises(ExternalEffectDenied):
        scope.probe("scheduler", input='{"action":"status"}')


def test_approved_helper_stall_is_bounded_and_cleaned_up(tmp_path):
    import time

    import external_effect_guard as guard

    scope = guard.ScopedEffects(tmp_path)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        scope.probe("stall", input='{"action":"status"}')
    assert time.monotonic() - started < 12


def test_mcp_sdk_native_loader_imports_under_the_guard():
    """pywintypes/_win32sysloader are SDK loaders, not OS-manager capabilities."""
    import mcp.server.fastmcp

    assert mcp.server.fastmcp.FastMCP is not None


def test_importable_pywin32_modules_lose_process_entry_points():
    if os.name != "nt":
        pytest.skip("pywin32 is Windows-only")
    import win32api
    import win32process

    with pytest.raises(ExternalEffectDenied):
        win32api.WinExec("cmd /c exit")
    with pytest.raises(ExternalEffectDenied):
        win32process.CreateProcess(None, "cmd /c exit", None, None, False, 0, None, None, None)


def test_copied_context_cannot_reuse_revoked_fixture_capability(tmp_path, monkeypatch):
    import contextvars

    calls = []
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **kw: calls.append(True))
    with guard.fixture_root(tmp_path):
        scope = guard.ScopedEffects(tmp_path)
        copied = contextvars.copy_context()
    with pytest.raises(ExternalEffectDenied, match="fixture"):
        copied.run(scope.probe, "scheduler", input="{}")
    assert calls == []


@pytest.mark.parametrize(
    "relative,override",
    [
        ("tests/not_test_windows_acl.py", ""),
        ("elsewhere/test_windows_acl.py", ""),
        ("tests/test_unreviewed.py", "test_unreviewed.py"),
    ],
)
def test_strict_selection_uses_fixed_canonical_paths(monkeypatch, relative, override):
    from pathlib import Path
    from types import SimpleNamespace

    import conftest

    repository = Path(guard.__file__).resolve().parents[1]
    monkeypatch.setenv("ACPGW_STRICT_TEST_MODULES", override)
    item = SimpleNamespace(path=repository / relative, nodeid=relative)
    with pytest.raises(pytest.UsageError, match="unaudited"):
        conftest.pytest_collection_modifyitems(None, [item])


def test_job_capability_exports_are_disarmed_for_mcp_imports():
    if os.name != "nt":
        pytest.skip("pywin32 is Windows-only")
    import win32job

    for name in (
        "TerminateJobObject",
        "AssignProcessToJobObject",
        "CreateJobObject",
        "OpenJobObject",
        "SetInformationJobObject",
    ):
        function = getattr(win32job, name)
        assert getattr(function, "__module__", None) == guard.__name__, name
        assert function.__name__ == "denied", name


@pytest.mark.parametrize(
    "name",
    [
        "SuspendThread",
        "ResumeThread",
        "SetPriorityClass",
        "SetProcessAffinityMask",
        "SetProcessPriorityBoost",
        "SetProcessShutdownParameters",
        "SetProcessWorkingSetSize",
        "SetThreadAffinityMask",
        "SetThreadIdealProcessor",
        "SetThreadPriority",
        "SetThreadPriorityBoost",
        "AttachThreadInput",
        "VirtualAllocEx",
        "VirtualFreeEx",
        "beginthreadex",
        "ExitProcess",
    ],
)
def test_conventional_pywin32_process_mutations_are_disarmed(name):
    if os.name != "nt":
        pytest.skip("pywin32 is Windows-only")
    import win32process

    # Inspect exports only: neither handles nor process mutation calls, even in RED.
    function = getattr(win32process, name)
    assert getattr(function, "__module__", None) == guard.__name__, name
    assert function.__name__ == "denied", name


@pytest.mark.parametrize(
    "name",
    [
        "GenerateConsoleCtrlEvent",
        "LoadLibrary",
        "LoadLibraryEx",
        "GetProcAddress",
        "ExitWindows",
        "InitiateSystemShutdown",
    ],
)
def test_pywin32_alternate_process_controls_are_disarmed(name):
    if os.name != "nt":
        pytest.skip("pywin32 is Windows-only")
    import win32api

    function = getattr(win32api, name)
    assert getattr(function, "__module__", None) == guard.__name__, name
    assert function.__name__ == "denied", name


@pytest.mark.parametrize("mode", ["collection-direct", "collection-nodeid", "collection-recursive"])
def test_fixed_startup_harness_refuses_import_markers(mode, audited_external_effects):
    import json

    result = audited_external_effects.probe(mode, input="")
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.splitlines()[-1])
    assert not observed["marker_created"]
    if mode == "collection-recursive":
        assert observed["exit_code"] == 0
        assert observed["pytest_imports_guarded"]
        assert "1 test collected" in result.stdout
    else:
        assert observed["exit_code"] != 0
        assert not observed["pytest_imports_guarded"]


def test_fixed_prebound_harness_fails_closed(audited_external_effects):
    import json

    result = audited_external_effects.probe("startup-prebound", input="")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"prebound_denied": True}


def test_copied_pending_process_permit_is_revoked_with_fixture(tmp_path, monkeypatch):
    import contextvars

    copied = []
    monkeypatch.setattr(
        guard.subprocess, "run", lambda *a, **kw: copied.append(contextvars.copy_context())
    )
    with guard.fixture_root(tmp_path):
        guard.ScopedEffects(tmp_path).probe("scheduler", input="{}")
    context = copied[0]
    permit = context.run(guard._PERMIT.get)
    argv = subprocess.list2cmdline(permit["argv"]) if os.name == "nt" else permit["argv"]
    with pytest.raises(ExternalEffectDenied):
        context.run(
            guard._audit,
            "subprocess.Popen",
            (permit["argv"][0], argv, permit["cwd"], permit["env"]),
        )


@pytest.mark.parametrize(
    "kind", ["ancestor-conftest", "package-init", "config-plugin", "conftest-plugin"]
)
@pytest.mark.parametrize("location", ["root", "unit"])
@pytest.mark.parametrize("selection", ["direct", "nodeid", "recursive"])
def test_support_import_chain_is_confined(kind, location, selection, audited_external_effects):
    import json

    result = audited_external_effects.probe(f"{kind}-{location}-{selection}", input="")
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.splitlines()[-1])
    assert not observed["marker_created"], result.stdout
    if kind == "config-plugin":
        assert observed["exit_code"] == 0, result.stdout + result.stderr
        assert observed["pytest_imports_guarded"]
        expected = "2 tests collected" if selection == "recursive" else "1 test collected"
        assert expected in result.stdout
    else:
        assert observed["exit_code"] != 0
        assert not observed["pytest_imports_guarded"]
