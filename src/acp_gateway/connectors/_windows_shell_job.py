"""Standalone Win32 job ownership for the gated shell worker (stdlib only)."""

import ctypes
import re
import sys
from ctypes import wintypes


class _Limits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IO(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _Extended(ctypes.Structure):
    _fields_ = [
        ("basic", _Limits),
        ("io", _IO),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user", ctypes.c_int64),
        ("kernel", ctypes.c_int64),
        ("period_user", ctypes.c_int64),
        ("period_kernel", ctypes.c_int64),
        ("faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


def _api():
    if sys.platform != "win32":
        raise OSError("Windows job objects require Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            wintypes.BOOL,
        ),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            wintypes.BOOL,
        ),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "IsProcessInJob": (
            [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
            wintypes.BOOL,
        ),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _check(result):
    if not result:
        raise OSError("Windows shell job operation failed")
    return result


def _name(name):
    if not isinstance(name, str) or not re.fullmatch(r"Local\\ACPGW-Shell-[0-9a-f]{64}", name):
        raise ValueError("invalid shell job identity")
    return name


class WindowsShellJob:
    def __init__(self, name):
        self.name, self.api = _name(name), _api()
        ctypes.set_last_error(0)
        self.handle = _check(self.api.CreateJobObjectW(None, self.name))
        try:
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                raise OSError("shell job identity already exists")
            limits = _Extended()
            limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway flags.
            _check(
                self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
            )
        except BaseException:
            self.close()
            raise

    def terminate(self):
        _check(self.api.TerminateJobObject(self.handle, 1))

    def active(self):
        info = _Accounting()
        _check(
            self.api.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
            )
        )
        return info.active

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            _check(self.api.CloseHandle(handle))


def enter_windows_job(name):
    """Attach the real worker, including a frozen bootloader's child, before input."""
    api = _api()
    # ASSIGN_PROCESS | QUERY only. The worker cannot terminate or change limits.
    handle = _check(api.OpenJobObjectW(0x0001 | 0x0004, False, _name(name)))
    try:
        current = api.GetCurrentProcess()
        _check(api.AssignProcessToJobObject(handle, current))
        assigned = wintypes.BOOL()
        _check(api.IsProcessInJob(current, handle, ctypes.byref(assigned)))
        _check(assigned.value)
    finally:
        # Parent is the sole handle owner after READY; never inherit into a command.
        _check(api.CloseHandle(handle))
