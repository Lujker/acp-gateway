"""Read-only private ACL preflight in a bounded, disposable native child."""

import json
import struct
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

# Preserve the old verifier deadline. A native call is never made in the parent:
# even local filesystem security APIs can stall and a thread cannot cancel them.
_VERIFY_TIMEOUT = 10
_CLEANUP_TIMEOUT = 1
# pythonw.exe is a GUI-subsystem program: without this flag Windows shows the
# "working in background" cursor for every verifier child it starts.
_STARTF_FORCEOFFFEEDBACK = 0x00000080


def _startupinfo():
    if not hasattr(subprocess, "STARTUPINFO"):
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= _STARTF_FORCEOFFFEEDBACK
    return info


def _verify_paths(payload):
    # Lazy imports keep this standalone module usable on non-Windows callers and
    # in the updater's copied support files. Only the child enters native APIs.
    import win32api
    import win32con
    import win32security

    with closing(
        win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    ) as token:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    allowed = {
        win32security.ConvertSidToStringSid(sid),
        "S-1-5-18",
        "S-1-5-32-544",
    }
    readers = allowed | {"S-1-3-4"}
    for paths, directory in ((payload["files"], False), (payload["directories"], True)):
        for path in paths:
            attributes = win32api.GetFileAttributes(path)
            if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Reparse point")
            descriptor = win32security.GetNamedSecurityInfo(
                path,
                win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
            )
            owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
            if owner not in allowed:
                raise ValueError("Untrusted owner")
            dacl = descriptor.GetSecurityDescriptorDacl()
            control, _revision = descriptor.GetSecurityDescriptorControl()
            if not control & win32security.SE_DACL_PRESENT or dacl is None:
                raise ValueError("Absent or null DACL")
            if not dacl.IsValid():
                raise ValueError("Invalid DACL")
            container = bool(attributes & win32con.FILE_ATTRIBUTE_DIRECTORY)
            for rights, trustee in _access_rules(bytes(memoryview(descriptor)), container):
                if win32security.ConvertSidToStringSid(win32security.SID(trustee)) in readers:
                    continue
                if (not directory and rights != 0) or (directory and rights & 852310):
                    raise ValueError("Public ACL")


def _access_rules(data, container):
    """Match .NET CommonObjectSecurity's GetAccessRules(true, true, SID).

    CommonAcl drops meaningless inherit-only ACEs; GetRules considers CommonAce
    only (including callbacks), NOT ObjectAce. This mirrors the saved oracle,
    not an effective-access calculation. Callback conditions are not evaluated:
    their Allow masks are checked conservatively, just like PowerShell.
    pywin32 GetAce cannot decode callback ACEs, so parse their documented binary
    layout from the validated, self-relative descriptor, with explicit bounds.
    """
    if len(data) < 20 or not struct.unpack_from("<H", data, 2)[0] & 0x8000:
        raise ValueError("Invalid self-relative descriptor")
    offset = struct.unpack_from("<I", data, 16)[0]
    if offset < 20 or offset + 8 > len(data):
        raise ValueError("Invalid DACL offset")
    _revision, _reserved, size, count, _reserved2 = struct.unpack_from("<BBHHH", data, offset)
    end = offset + size
    if size < 8 or end > len(data):
        raise ValueError("Invalid DACL size")
    cursor = offset + 8
    for _index in range(count):
        if cursor + 4 > end:
            raise ValueError("Invalid ACE header")
        kind, flags, length = struct.unpack_from("<BBH", data, cursor)
        if length < 4 or length % 4 or cursor + length > end:
            raise ValueError("Invalid ACE size")
        ace = data[cursor : cursor + length]
        cursor += length
        if kind not in range(22):
            raise ValueError("Unsupported ACE type")
        # Only access-allowed CommonAce entries become Allow access rules in
        # .NET; all deny/audit/object ACEs are ignored by the SCRIPT's loop.
        if kind not in (0, 9):
            continue
        if len(ace) < 16:
            raise ValueError("Truncated Allow ACE")
        rights = struct.unpack_from("<I", ace, 4)[0]
        sid_end = 16 + ace[9] * 4
        if ace[8] != 1 or sid_end > len(ace):
            raise ValueError("Invalid SID")
        if flags & 0x08 and (not container or not flags & 0x03):
            continue
        yield rights, ace[8:sid_end]


def _verify_child(payload, connection):
    try:
        _verify_paths(payload)
        connection.send_bytes(b"private")
    except Exception:
        # Neither native diagnostics nor input-dependent messages cross IPC.
        return
    finally:
        connection.close()


def _verifier_main():
    try:
        _verify_paths(json.load(sys.stdin))
    except Exception:
        return 1
    print('{"private":true}')
    return 0


def check_private_paths(files, *, directories=(), purpose="Hermes discovery"):
    payload = {
        "files": [str(path) for path in files],
        "directories": [str(path) for path in directories],
    }
    if getattr(sys, "frozen", False):
        # Bundled executables are not Python interpreters. Their existing
        # freeze_support entry can spawn the same fixed native verifier target.
        _check_spawn_paths(payload, purpose)
        return
    try:
        # A standalone fixed entry avoids re-importing the application's main
        # module (multiprocessing spawn measured too slow for IPC freshness).
        # -I ignores PYTHONPATH, user site and cwd; no requested path is code.
        result = subprocess.run(  # noqa: S603 — fixed interpreter/module; JSON stdin
            [sys.executable, "-I", str(Path(__file__).absolute())],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERIFY_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            startupinfo=_startupinfo(),
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Windows discovery ACL verification timed out") from None
    except OSError:
        raise ValueError("Windows discovery ACL verifier could not be started") from None
    if result.returncode:
        raise ValueError(f"{purpose} files must have private Windows ACLs")
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or response.get("private") is not True:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(
            "Windows discovery ACL verification returned an invalid response"
        ) from None


def _check_spawn_paths(payload, purpose):
    import multiprocessing

    # Explicit spawn also works with the binary entry's freeze_support(), and
    # does not inherit unrelated open handles or rely on a global start method.
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    child = context.Process(target=_verify_child, args=(payload, send), daemon=True)
    started = False
    try:
        deadline = time.monotonic() + _VERIFY_TIMEOUT
        child.start()
        started = True
        send.close()
        if not receive.poll(max(0, deadline - time.monotonic())):
            raise ValueError("Windows discovery ACL verification timed out")
        # The fixed child writes one tiny frame only after all native calls have
        # finished. No pickle response, streaming output or input-dependent text.
        if receive.recv_bytes(maxlength=7) != b"private":
            raise ValueError("Windows discovery ACL verification returned an invalid response")
        child.join(timeout=max(0, min(_CLEANUP_TIMEOUT, deadline - time.monotonic())))
        if child.is_alive() or child.exitcode != 0 or time.monotonic() > deadline:
            raise ValueError("Windows discovery ACL verification timed out")
    except (OSError, EOFError):
        raise ValueError(f"{purpose} files must have private Windows ACLs") from None
    finally:
        receive.close()
        send.close()
        if started:
            if child.is_alive():
                child.terminate()
                child.join(timeout=_CLEANUP_TIMEOUT)
            if child.is_alive():
                child.kill()
                child.join(timeout=_CLEANUP_TIMEOUT)
            if not child.is_alive():
                child.close()
        else:
            child.close()


def ensure_private_directory(directory: Path) -> None:
    """Create only missing directories; validate existing ACLs without repairing them."""
    if not directory.is_absolute():
        raise ValueError("Private Windows directories require absolute paths")
    missing = []
    current = directory
    while not current.exists():
        if current.is_symlink():
            raise ValueError("Private Windows directories cannot be links")
        missing.append(current)
        current = current.parent
    check_private_paths([], directories=[current], purpose="Gateway setup")
    if not missing:
        # Owned runtime/config directories also contain future secret files:
        # reject public read inheritance, not just public write access.
        check_private_paths([current], purpose="Gateway setup")
    for path in reversed(missing):
        path.mkdir(mode=0o700)
        check_private_paths([path], purpose="Gateway setup")


if __name__ == "__main__":
    raise SystemExit(_verifier_main())
