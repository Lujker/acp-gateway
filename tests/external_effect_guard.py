"""Test-only deny-by-default accident guard for explicitly audited modules.

Installed before pytest/plugins by ``python -I -S tests/strict_isolation_runner.py``.
Strict conftest startup refuses every other entry route; ordinary pytest is
unchanged. The fixed audited paths may only be narrowed, never extended by env.
Direct targets are validated before pytest import, and recursive collection
excludes unaudited files/directories before importing them. The gate denies the
final process, service, scheduler, updater and native-manager boundaries before
an effect can happen, so an audited test that accidentally reaches one fails
closed instead of touching the machine.

Honest limits -- this is an accident guard, not a sandbox:

* startup fails closed if ctypes/_ctypes or pywin32 capability modules were
  imported before installation; prior callables are NOT revoked;
* binding of unaudited native libraries and symbols is denied, but CPython never
  reports a ctypes invocation through an audit event (verified on 3.12), so a
  test that deliberately walks a loaded module's memory and calls an address is
  outside this guard;
* filesystem, socket, registry, account and environment writes are not
  comprehensively intercepted (for example win32net account APIs are out of scope);
* pywin32 modules that dependencies import keep working, with their process
  entry points disarmed; a direct ``ExtensionFileLoader`` re-exec can restore
  them.

Use it to run a named, audited module set -- never to declare the whole suite
safe. The controlled runner supports audited CPython 3.12 on native Windows and
Linux only; the POSIX fork_exec wrapper relies on that runtime's 23-argument ABI.
The pinned helper alone may start synthetic collection/prebound probes, model-free
scheduler stubs and a timeout canary. No arbitrary argv/script opt-in exists.
"""

import base64
import contextvars
import hashlib
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

_PYTHON_EXECUTABLE = sys.executable
_SYSTEM_POWERSHELL = (
    (Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe").resolve()
    if os.name == "nt"
    else None
)


class _FixtureCapability:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.active = True


_ROOT = contextvars.ContextVar("test_fixture_root", default=None)


@contextmanager
def fixture_root(root):
    """Called only by global autouse fixture; grants no external effects itself."""
    capability = _FixtureCapability(root)
    token = _ROOT.set(capability)
    try:
        yield
    finally:
        capability.active = False
        _ROOT.reset(token)


_NATIVE_CALL = contextvars.ContextVar("test_native_create_process", default=False)
_PERMIT = contextvars.ContextVar("test_external_process_permit", default=None)
_PROBE_MODES = frozenset(
    {
        "scheduler",
        "stall",
        "collection-direct",
        "collection-nodeid",
        "collection-recursive",
        "startup-prebound",
        "ancestor-conftest-root-direct",
        "ancestor-conftest-root-nodeid",
        "ancestor-conftest-root-recursive",
        "ancestor-conftest-unit-direct",
        "ancestor-conftest-unit-nodeid",
        "ancestor-conftest-unit-recursive",
        "package-init-root-direct",
        "package-init-root-nodeid",
        "package-init-root-recursive",
        "package-init-unit-direct",
        "package-init-unit-nodeid",
        "package-init-unit-recursive",
        "config-plugin-root-direct",
        "config-plugin-root-nodeid",
        "config-plugin-root-recursive",
        "config-plugin-unit-direct",
        "config-plugin-unit-nodeid",
        "config-plugin-unit-recursive",
        "conftest-plugin-root-direct",
        "conftest-plugin-root-nodeid",
        "conftest-plugin-root-recursive",
        "conftest-plugin-unit-direct",
        "conftest-plugin-unit-nodeid",
        "conftest-plugin-unit-recursive",
    }
)
_PROBE_SHA256 = "cf4a65fe96a83b3f1b0ab8cd5caf0969b79d6f72383a877006e265bc4db838a7"


class ScopedEffects:
    """Only fixed audited operations; deliberately no arbitrary argv/script API.

    Use through the audited_external_effects fixture. This is an accident guard,
    not a sandbox against a malicious test editing Python's own process memory.
    """

    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self._capability = _ROOT.get()
        self._validate_root()

    def _validate_root(self):
        if (
            self._capability is None
            or self._capability is not _ROOT.get()
            or not self._capability.active
            or self.root != self._capability.root
        ):
            raise ExternalEffectDenied("external opt-in root is not the current fixture")

    def _run(self, argv, *, input):
        self._validate_root()
        helper = Path(__file__).parent / "fakes" / "isolation_probe.py"
        if (
            len(argv) != 6
            or argv[:4] != [_PYTHON_EXECUTABLE, "-I", "-S", str(helper)]
            or argv[4] not in _PROBE_MODES
            or argv[5] != str(self.root)
        ):
            raise ExternalEffectDenied("arbitrary subprocess opt-ins are forbidden")
        if hashlib.sha256(helper.read_bytes()).hexdigest() != _PROBE_SHA256:
            raise ExternalEffectDenied("disposable probe source changed; audit required")
        env = {
            name: os.environ[name]
            for name in ("SystemRoot", "SystemDrive", "WINDIR")
            if name in os.environ
        }
        env.update(
            {
                name: str(self.root)
                for name in (
                    "HOME",
                    "USERPROFILE",
                    "APPDATA",
                    "LOCALAPPDATA",
                    "TMP",
                    "TEMP",
                    "TMPDIR",
                )
            }
        )
        permit = {
            "argv": argv,
            "cwd": str(self.root),
            "env": env,
            "started": False,
            "capability": self._capability,
        }
        token = _PERMIT.set(permit)
        try:
            return subprocess.run(
                argv,
                executable=argv[0],
                input=input,
                text=True,
                capture_output=True,
                cwd=str(self.root),
                env=env,
                timeout=10,
                check=False,
            )
        finally:
            _PERMIT.reset(token)

    def probe(self, mode, *, input):
        if mode not in _PROBE_MODES:
            raise ExternalEffectDenied("unknown disposable probe")
        helper = Path(__file__).parent / "fakes" / "isolation_probe.py"
        if hashlib.sha256(helper.read_bytes()).hexdigest() != _PROBE_SHA256:
            raise ExternalEffectDenied("disposable probe source changed; audit required")
        return self._run(
            [_PYTHON_EXECUTABLE, "-I", "-S", str(helper), mode, str(self.root)], input=input
        )

    def check_private_paths(self, files, *, directories=()):
        self._validate_root()
        files, directories = tuple(files), tuple(directories)
        from acp_gateway import windows_acl

        for value in [*files, *directories]:
            path = Path(value).absolute()
            if not path.is_relative_to(self.root):
                raise ExternalEffectDenied("ACL path outside fixture")
            for part in [path, *path.parents]:
                if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                    raise ExternalEffectDenied("ACL fixture path is a link")
                if part == self.root:
                    break
            if not path.resolve(strict=True).is_relative_to(self.root):
                raise ExternalEffectDenied("ACL path escaped fixture")
        if os.name != "nt":
            raise ExternalEffectDenied("read-only Windows ACL opt-in requires native Windows")
        if (
            hashlib.sha256(windows_acl.SCRIPT.encode()).hexdigest()
            != "d0f7cb907afb223c11f8817ba72342e76f984360b6d991b24134e6779ffa77fe"
        ):
            raise ExternalEffectDenied("ACL script changed; read-only audit required")
        executable = shutil.which("powershell.exe")
        if not executable or Path(executable).resolve() != _SYSTEM_POWERSHELL:
            raise ExternalEffectDenied("ACL verifier is not system PowerShell")
        argv = [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(windows_acl.SCRIPT.encode("utf-16-le")).decode("ascii"),
        ]
        token = _PERMIT.set(
            {
                "argv": argv,
                "cwd": None,
                "env": None,
                "executable": None,
                "started": False,
                "capability": self._capability,
            }
        )
        try:
            return windows_acl.check_private_paths(files, directories=directories)
        finally:
            _PERMIT.reset(token)


class ExternalEffectDenied(BaseException):
    """Fail closed even when production catches Exception/OSError/ValueError."""


# Audited CPython/Click local-console bootstrap, not a subprocess/native-manager
# opt-in. No loader may bind arbitrary symbols or load COM/SCM/task DLLs.
_CONSOLE_SYMBOLS = {
    "kernel32": {
        "GetStdHandle",
        "ReadConsoleW",
        "WriteConsoleW",
        "GetConsoleMode",
        # colorama console rendering imported by pytest before capture starts.
        "GetConsoleScreenBufferInfo",
        "SetConsoleTextAttribute",
        "SetConsoleCursorPosition",
        "FillConsoleOutputCharacterA",
        "FillConsoleOutputAttribute",
        "SetConsoleTitleW",
        "SetConsoleMode",
        "GetLastError",
        "GetCommandLineW",
        "LocalFree",
    },
    "shell32": {"CommandLineToArgvW"},
    None: {"PyObject_GetBuffer", "PyBuffer_Release"},
}


# Audited file-open/byte-range-lock FFI. Binding these symbols is not an
# external effect and they only open, inspect and lock files; every other
# library, name and binding stays denied. Names are exactly those the installed
# filelock 4.0.12 Windows backend and production lease() bind.
_LOCK_SYMBOLS = {
    "kernel32": {
        "CloseHandle",
        "GetFileInformationByHandle",
        "GetProcessTimes",
        "LockFileEx",
        "OpenProcess",
        "UnlockFileEx",
    },
    "ntdll": {
        "NtCreateFile",
        "RtlDosPathNameToNtPathName_U_WithStatus",
        "RtlFreeUnicodeString",
        "RtlNtStatusToDosError",
    },
}
_AUDITED_LIBRARIES = frozenset({*_CONSOLE_SYMBOLS, *_LOCK_SYMBOLS} - {None})


def _allowed_symbols(library):
    return _CONSOLE_SYMBOLS.get(library, frozenset()) | _LOCK_SYMBOLS.get(library, frozenset())


def _console_library(name):
    if name is None:
        return None
    if not isinstance(name, str):
        return "denied"
    normalized = name.lower().removesuffix(".dll")
    if normalized in _AUDITED_LIBRARIES:
        return normalized
    return "denied"


def _console_ctypes(event, args):
    if os.name != "nt":
        return False
    if event == "ctypes.dlopen" and len(args) == 1:
        return _console_library(args[0]) in _AUDITED_LIBRARIES
    if event == "ctypes.dlsym" and len(args) == 2:
        name = getattr(args[0], "_name", "denied")
        if name == "python dll" and getattr(args[0], "_handle", None) == sys.dllhandle:
            library = None
        else:
            library = _console_library(name)
        return args[1] in _allowed_symbols(library)
    return False


def _active_permit():
    permit = _PERMIT.get()
    capability = permit.get("capability") if permit is not None else None
    if capability is None or not capability.active or capability is not _ROOT.get():
        return None
    return permit


def _audit(event, args):
    if _console_ctypes(event, args):
        return
    permit = _active_permit()
    if permit is not None:
        owned = permit.get("owned_pid")
        if event == "os.kill" and len(args) == 2 and owned is not None and args[0] == owned:
            return
        if event == "subprocess.Popen" and not permit["started"]:
            executable, argv, cwd, env = args
            expected = permit["argv"]
            command = subprocess.list2cmdline(expected) if os.name == "nt" else expected
            expected_executable = permit.get("executable", expected[0])
            if (
                executable == expected_executable
                and argv == command
                and cwd == permit["cwd"]
                and env == permit["env"]
            ):
                permit["started"] = True
                return
        if event == "_winapi.CreateProcess" and _NATIVE_CALL.get():
            return
    if event in {
        "subprocess.Popen",
        "_winapi.CreateProcess",
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
        "ctypes.dlopen",
        "ctypes.dlsym",
        "ctypes.dlsym/handle",
    }:
        raise ExternalEffectDenied("test isolation denied boundary: " + event)


# Capability modules only. pywintypes/_win32sysloader are the pywin32 DLL
# loader that ordinary dependencies (for example the mcp SDK) import, so they
# stay importable; every module that can reach a service, scheduler or COM
# object stays denied outright.
_NATIVE_MANAGERS = frozenset(
    {
        "win32com",
        "pythoncom",
        "comtypes",
        "win32service",
        "win32serviceutil",
        "win32ts",
        "wmi",
    }
)

# pywin32 modules that installed dependencies import (mcp imports win32api for
# console utilities). They stay importable, but their process-creation entry
# points are replaced once the module loads. MCP also requires win32job imports;
# constants remain available but every job callable except its exception class
# is denied, including TerminateJobObject (no handle acquisition is needed).
_IMPORTABLE_WITH_DENIAL = {
    # MCP imports job constants. No job callable is needed for safe imports.
    "win32job": None,
    "win32api": (
        "CreateProcess",
        "ExitWindowsEx",
        "ExitWindows",
        "InitiateSystemShutdown",
        "InitiateSystemShutdownEx",
        "AbortSystemShutdown",
        "SetSystemPowerState",
        "GenerateConsoleCtrlEvent",
        "LoadLibrary",
        "LoadLibraryEx",
        "GetProcAddress",
        "ShellExecute",
        "ShellExecuteEx",
        "TerminateProcess",
        "WinExec",
    ),
    "win32process": (
        "CreateProcess",
        "CreateProcessAsUser",
        "CreateRemoteThread",
        "SetThreadContext",
        "TerminateProcess",
        "WriteProcessMemory",
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
    ),
}


def _denied_entry_point(name):
    def denied(*args, **kwargs):
        raise ExternalEffectDenied("test isolation denied native entry point: " + name)

    return denied


class _DeniedEntryPoints:
    """Load an importable pywin32 module, then disarm its process entry points."""

    def __init__(self, loader, name):
        self._loader = loader
        self._name = name

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        names = _IMPORTABLE_WITH_DENIAL[self._name]
        if names is None:
            names = [
                name for name, value in vars(module).items() if callable(value) and name != "error"
            ]
        for name in names:
            if hasattr(module, name):
                setattr(module, name, _denied_entry_point(name))


class _NativeManagerImports:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in _NATIVE_MANAGERS:
            raise ExternalEffectDenied("test isolation denied native manager import")
        if root in _IMPORTABLE_WITH_DENIAL and fullname == root:
            from importlib.machinery import PathFinder

            spec = PathFinder.find_spec(fullname, path)
            if spec is not None and spec.loader is not None:
                spec.loader = _DeniedEntryPoints(spec.loader, fullname)
            return spec
        return None


def _protect_native_create_process():
    if os.name != "nt":
        return
    import _winapi

    original = _winapi.CreateProcess
    terminate = _winapi.TerminateProcess

    def guarded(
        application, command, process_attrs, thread_attrs, inherit, flags, env, cwd, startup
    ):
        permit = _active_permit()
        if not (
            permit
            and permit["started"]
            and application == permit.get("executable", permit["argv"][0])
            and command == subprocess.list2cmdline(permit["argv"])
            and env == permit["env"]
            and cwd == permit["cwd"]
        ):
            raise ExternalEffectDenied("test isolation denied native process")
        token = _NATIVE_CALL.set(True)
        try:
            result = original(
                application, command, process_attrs, thread_attrs, inherit, flags, env, cwd, startup
            )
            permit["owned_process_handle"] = result[0]
            return result
        finally:
            _NATIVE_CALL.reset(token)

    def guarded_terminate(*args):
        permit = _active_permit()
        if not (len(args) == 2 and permit and permit.get("owned_process_handle") == args[0]):
            raise ExternalEffectDenied("test isolation denied native process termination")
        return terminate(*args)

    _winapi.CreateProcess = guarded
    _winapi.TerminateProcess = guarded_terminate


def _protect_posix_fork_exec():
    if os.name != "posix":
        return
    import _posixsubprocess

    original = _posixsubprocess.fork_exec

    def guarded(*args):
        permit = _active_permit()
        if not (permit and permit["started"] and len(args) == 23):
            raise ExternalEffectDenied("test isolation denied direct POSIX process")
        environment = [os.fsencode(k) + b"=" + os.fsencode(v) for k, v in permit["env"].items()]
        if not (
            args[0] == permit["argv"]
            and args[1] == (os.fsencode(permit["argv"][0]),)
            and args[4] == permit["cwd"]
            and args[5] == environment
            and args[21] is None
        ):
            raise ExternalEffectDenied("test isolation denied POSIX process arguments")
        pid = original(*args)
        # The only process this permit may signal is its own started child.
        permit["owned_pid"] = pid
        return pid

    _posixsubprocess.fork_exec = guarded
    subprocess._fork_exec = guarded


INSTALLED = False
CONTROLLED_STARTUP = False


def install():
    """Install the boundaries once, for an audited module set only."""
    global INSTALLED
    if INSTALLED:
        return
    if {"ctypes", "_ctypes"} & sys.modules.keys():
        raise ExternalEffectDenied(
            "ctypes loaded before test isolation; use python -I -S tests/strict_isolation_runner.py"
        )
    loaded = {name.split(".", 1)[0] for name in sys.modules}
    if loaded & _NATIVE_MANAGERS or loaded & _IMPORTABLE_WITH_DENIAL.keys():
        # A preloaded capability module keeps live references a finder cannot revoke.
        raise ExternalEffectDenied("native manager loaded before test isolation")
    _protect_posix_fork_exec()
    _protect_native_create_process()
    sys.meta_path.insert(0, _NativeManagerImports())
    sys.addaudithook(_audit)
    INSTALLED = True


REPOSITORY = Path(__file__).resolve().parents[1]
AUDITED_MODULES = frozenset(
    {
        "tests/test_external_effect_isolation.py",
        "tests/unit/test_windows_acl.py",
        "tests/unit/test_milestone_integration.py",
        "tests/unit/test_connector_maintenance.py",
        "tests/unit/test_connector_maintenance_ack.py",
    }
)


def selected_paths():
    """An environment selection may narrow, never extend, the reviewed set."""
    value = os.environ.get("ACPGW_STRICT_TEST_MODULES", "")
    requested = {name.strip() for name in value.split(",") if name.strip()}
    if requested - AUDITED_MODULES:
        raise ValueError("strict isolation refuses unaudited module selection")
    return frozenset((REPOSITORY / name).resolve() for name in requested or AUDITED_MODULES)
