"""Standard-library-only maintenance helper, copied outside the replaced tool.

Runs with the base interpreter, not the Python environment uv is replacing.
Keeps one recovery snapshot of the environment, entry point and selected SQLite
database. A failed package operation or post-install health check restores it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


@contextlib.contextmanager
def lease(path: Path, *, exclusive: bool):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if sys.platform == "win32":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            lock = kernel.LockFileEx
            lock.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(Overlapped),
            ]
            lock.restype = wintypes.BOOL
            unlock = kernel.UnlockFileEx
            unlock.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(Overlapped),
            ]
            unlock.restype = wintypes.BOOL
            handle = msvcrt.get_osfhandle(descriptor)
            overlap = Overlapped()
            if not lock(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlap)):
                error = ctypes.get_last_error()
                if error != 33:
                    raise ctypes.WinError(error)
                raise ValueError("installation is in use; stop its running processes first")
        else:
            import fcntl

            try:
                fcntl.flock(
                    descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                )
            except BlockingIOError:
                raise ValueError(
                    "installation is in use; stop its running processes first"
                ) from None
        try:
            yield descriptor
        finally:
            if sys.platform == "win32":
                unlock(handle, 0, 1, 0, ctypes.byref(overlap))
    finally:
        os.close(descriptor)


def run(command: list[str], *, timeout=300) -> None:
    result = subprocess.run(command, timeout=timeout, check=False)  # noqa: S603 — explicit argv
    if result.returncode:
        raise ValueError(f"maintenance subprocess failed (exit {result.returncode})")


def snapshot(plan: dict, backup: Path) -> None:
    backup.mkdir(mode=0o700)
    prefix = Path(plan["prefix"])
    shutil.copytree(prefix, backup / "environment", symlinks=True)
    entry = Path(plan["entry"])
    if entry.is_symlink():
        (backup / "entry").symlink_to(entry.readlink())
    else:
        shutil.copy2(entry, backup / "entry")
    database = Path(plan["database"]) if plan.get("database") else None
    if database and database.is_file():
        # SQLite backup includes committed WAL content; copying only .db does not.
        with (
            contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source,
            contextlib.closing(sqlite3.connect(backup / "database")) as destination,
        ):
            source.backup(destination)
        (backup / "database").chmod(0o600)
    (backup / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (backup / "plan.json").chmod(0o600)


def restore(plan: dict, backup: Path) -> None:
    prefix = Path(plan["prefix"])
    recovered = prefix.with_name(prefix.name + ".acpgw-recovery")
    if recovered.exists():
        raise ValueError("recovery staging directory already exists; inspect it before retrying")
    shutil.copytree(backup / "environment", recovered, symlinks=True)
    if prefix.exists():
        shutil.rmtree(prefix)
    recovered.replace(prefix)
    entry = Path(plan["entry"])
    entry.unlink(missing_ok=True)
    saved = backup / "entry"
    if saved.is_symlink():
        entry.symlink_to(saved.readlink())
    else:
        shutil.copy2(saved, entry)
    database = Path(plan["database"]) if plan.get("database") else None
    if database and (backup / "database").is_file():
        for suffix in ("-wal", "-shm"):
            Path(str(database) + suffix).unlink(missing_ok=True)
        temporary = database.with_name(database.name + ".acpgw-recovery")
        shutil.copy2(backup / "database", temporary)
        temporary.replace(database)


def health(plan: dict) -> None:
    entry = plan["entry"]
    result = subprocess.run(  # noqa: S603
        [entry, "version", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    info = json.loads(result.stdout)
    if info["manager"] != "uv":
        raise ValueError("updated entry point does not belong to uv")
    if plan.get("expected_version") and info["version"] != plan["expected_version"]:
        raise ValueError("updated package has an unexpected version")
    if plan.get("config"):
        command = [entry, "--config", plan["config"]]
        if plan.get("env_file"):
            command += ["--env-file", plan["env_file"]]
        run([*command, "config", "check"], timeout=30)
    # Exercise packaged migrations and actual database compatibility before restart.
    # The database was backed up and the exclusive lease remains held.
    python = str(Path(plan["prefix"]) / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    code = (
        "import sys; from acp_gateway.storage.db import Store, _migrations; "
        "assert _migrations(), 'SQL resources missing'; "
        "store = Store.open(sys.argv[1]); store.close()"
    )
    database = plan.get("database")
    run(
        [python, "-c", code, database if database and Path(database).is_file() else ":memory:"],
        timeout=30,
    )


def record_result(plan: dict, status: str, code: int | None = None) -> None:
    if not plan.get("result"):
        return
    result = Path(plan["result"])
    temporary = result.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"operation": plan["operation"], "status": status, "returncode": code}),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(result)


def execute(plan: dict, backup: Path) -> int:
    previous = None
    current = Path(plan["current"]) if plan.get("current") else None
    if current and current.exists():
        previous = Path(json.loads(current.read_text(encoding="utf-8"))["backup"])
        if previous.parent != backup.parent or previous == backup:
            raise ValueError("invalid recovery pointer")
    if plan["operation"] == "rollback":
        if previous is None:
            raise ValueError("no previous recovery snapshot exists")
        saved_plan = json.loads((previous / "plan.json").read_text(encoding="utf-8"))
        if any(saved_plan.get(key) != plan.get(key) for key in ("prefix", "entry", "database")):
            raise ValueError("recovery snapshot belongs to another installation/configuration")
    snapshot(plan, backup)
    try:
        if plan["operation"] == "rollback":
            restore(plan, previous)
            health(plan)
        else:
            run(plan["command"])
        if plan["operation"] == "update":
            health(plan)
    except (OSError, ValueError, subprocess.SubprocessError):
        restore(plan, backup)
        shutil.rmtree(backup)
        print(
            "Update failed; previous environment, entry point and database were restored.",
            flush=True,
        )
        return 1
    if current:
        temporary = current.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            temporary.chmod(0o600)
            json.dump({"backup": str(backup)}, stream)
        temporary.replace(current)
        if previous and (previous / "plan.json").is_file():
            shutil.rmtree(previous)
    print(f"Maintenance verified. Recovery snapshot: {backup}", flush=True)
    return 0


def main() -> int:
    manifest = Path(sys.argv[1])
    plan = json.loads(manifest.read_text(encoding="utf-8"))
    try:
        record_result(plan, "running")
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            if plan["pid"] != os.getpid():
                handle = kernel.OpenProcess(0x00100000, False, plan["pid"])
                if handle:
                    try:
                        if kernel.WaitForSingleObject(handle, 30000) != 0:
                            raise ValueError("original maintenance process did not exit")
                    finally:
                        kernel.CloseHandle(handle)
            with lease(Path(plan["lease"]), exclusive=True):
                code = execute(plan, Path(plan["backup"]))
        else:
            code = execute(plan, Path(plan["backup"]))
        record_result(plan, "succeeded" if code == 0 else "failed", code)
        return code
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(
            f"Maintenance failed: {type(exc).__name__}: {exc}. Recovery files were kept.",
            file=sys.stderr,
        )
        if Path(plan["backup"]).is_dir():
            print(f"Recovery snapshot: {plan['backup']}", file=sys.stderr)
        record_result(plan, "failed", 1)
        return 1
    finally:
        shutil.rmtree(manifest.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
