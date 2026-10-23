"""Real owned Windows descriptors compared with the pre-backend PowerShell oracle."""

import base64
import json
import multiprocessing
import struct
import subprocess
import sys
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from acp_gateway import windows_acl
from fakes.windows_acl_oracle import SCRIPT

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="native Windows ACL corpus")
CASES = [
    *[
        (f"mutation-bit-{bit}", f"(A;;0x{bit:x};;;WD)", False, False)
        for bit in (2, 4, 16, 64, 256, 65536, 262144, 524288)
    ],
    ("current", "(A;;FA;;;{sid})", True, True),
    ("system", "(A;;FA;;;SY)", True, True),
    ("administrators", "(A;;FA;;;BA)", True, True),
    ("owner-rights", "(A;;FA;;;OW)", True, True),
    ("world-read", "(A;;FR;;;WD)", False, True),
    ("world-execute", "(A;;FX;;;WD)", False, True),
    ("world-write", "(A;;FW;;;WD)", False, False),
    ("world-delete", "(A;;SD;;;WD)", False, False),
    ("world-dacl", "(A;;WD;;;WD)", False, False),
    ("world-owner", "(A;;WO;;;WD)", False, False),
    ("world-delete-child", "(A;;DC;;;WD)", False, False),
    ("world-zero", "(A;;0x0;;;WD)", True, True),
    ("inherited-read", "(A;ID;FR;;;WD)", False, True),
    ("inherited-write", "(A;ID;FW;;;WD)", False, False),
    ("inherit-only-read", "(A;OIIO;FR;;;WD)", True, True),
    ("inherit-only-write", "(A;OIIO;FW;;;WD)", True, False),
    ("deny", "(D;;FA;;;WD)", True, True),
    ("deny-not-compensation", "(D;;FA;;;WD)(A;;FR;;;WD)", False, True),
    ("empty", None, True, True),
    ("null", "NULL", False, False),
    ("object-allow", "(OA;;FR;00112233-4455-6677-8899-aabbccddeeff;;WD)", True, True),
    ("callback-write", '(XA;;FW;;;WD;(@User.Title == "Developer"))', False, False),
    ("inherit-only-no-target", "(A;IO;FW;;;WD)", True, True),
    ("callback-allow", '(XA;;FR;;;WD;(@User.Title == "Developer"))', False, True),
]


def oracle(files, directories):
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii"),
        ],
        input=json.dumps(
            {"files": list(map(str, files)), "directories": list(map(str, directories))}
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        return False
    assert json.loads(result.stdout) == {"private": True}
    return True


def native(files, directories):
    try:
        windows_acl._verify_paths(
            {"files": list(map(str, files)), "directories": list(map(str, directories))}
        )
    except Exception:
        return False
    return True


@pytest.fixture
def security():
    import win32api
    import win32con
    import win32security as security

    with closing(
        security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    ) as token:
        sid = security.ConvertSidToStringSid(
            security.GetTokenInformation(token, security.TokenUser)[0]
        )
    return security, sid


def set_dacl(path, security, sddl):
    descriptor = security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
    security.SetNamedSecurityInfo(
        str(path),
        security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )


@WINDOWS
@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
@pytest.mark.parametrize("name,aces,file_ok,dir_ok", CASES, ids=[c[0] for c in CASES])
def test_real_descriptor_parity(tmp_path, security, directory, name, aces, file_ok, dir_ok):
    sec, sid = security
    path = tmp_path / "owned"
    if directory:
        path.mkdir()
    else:
        path.write_text("{}")
    baseline = f"D:P(A;;FA;;;{sid})"
    if aces == "NULL":
        sddl = "D:NO_ACCESS_CONTROL"
    elif aces is None:
        sddl = "D:P"
    else:
        sddl = baseline + aces.format(sid=sid)
    try:
        set_dacl(path, sec, sddl)
        # Read back the installed descriptor: the corpus tests the OS, not just SDDL parsing.
        actual = sec.GetNamedSecurityInfo(
            str(path), sec.SE_FILE_OBJECT, sec.DACL_SECURITY_INFORMATION
        )
        assert actual is not None
        files, directories = ([], [path]) if directory else ([path], [])
        old = oracle(files, directories)
        new = native(files, directories)
        expected = dir_ok if directory else file_ok
        assert (old, new) == (expected, expected), (name, directory, old, new)
    finally:
        set_dacl(path, sec, baseline)


@WINDOWS
@pytest.mark.parametrize("directory", [False, True])
def test_missing_and_reparse_parity(tmp_path, directory):
    import _winapi

    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(target), str(junction))
    try:
        for path in (tmp_path / "missing", junction):
            files, directories = ([], [path]) if directory else ([path], [])
            assert not oracle(files, directories)
            assert not native(files, directories)
    finally:
        junction.rmdir()


@WINDOWS
def test_native_child_real_batch_without_powershell(tmp_path, security, monkeypatch):
    sec, sid = security
    file = tmp_path / "credential.json"
    file.write_text("{}")
    baseline = f"D:P(A;;FA;;;{sid})"
    set_dacl(file, sec, baseline)
    set_dacl(tmp_path, sec, baseline)

    original = subprocess.run

    def forbidden(argv, **kwargs):
        assert all("powershell" not in arg.lower() for arg in argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", forbidden)
    windows_acl.check_private_paths([file], directories=[tmp_path])


def _stall_verifier(_payload, _connection):
    time.sleep(30)


def test_deadline_reaps_owned_stalled_process(monkeypatch):
    real_context = multiprocessing.get_context("spawn")
    children = []

    def process(**kwargs):
        child = real_context.Process(target=_stall_verifier, args=kwargs["args"], daemon=True)
        children.append(child)
        return child

    monkeypatch.setattr(windows_acl, "_VERIFY_TIMEOUT", 0.1)
    monkeypatch.setattr(
        multiprocessing,
        "get_context",
        lambda method: SimpleNamespace(Pipe=real_context.Pipe, Process=process),
    )
    started = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        windows_acl._check_spawn_paths(
            {"files": ["not-read-by-stalled-child"], "directories": []}, "test"
        )
    assert time.monotonic() - started < 3
    assert children[0]._closed


@WINDOWS
@pytest.mark.parametrize("owner", ["S-1-5-18", "S-1-5-32-544", "S-1-1-0"])
def test_synthetic_owner_policy(tmp_path, security, monkeypatch, owner):
    # Arbitrary owners cannot be installed without elevated privileges. Real SID
    # descriptors here are substituted only at the read-only API boundary.
    sec, _sid = security
    descriptor = sec.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{owner}D:P(A;;FA;;;OW)", 1
    )
    monkeypatch.setattr(sec, "GetNamedSecurityInfo", lambda *args: descriptor)
    assert native([tmp_path], []) is (owner != "S-1-1-0")


@WINDOWS
@pytest.mark.parametrize("dacl", [False, True])
def test_synthetic_absent_null_dacl(tmp_path, security, monkeypatch, dacl):
    sec, sid = security
    descriptor = sec.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(sec.ConvertStringSidToSid(sid), False)
    descriptor.SetSecurityDescriptorDacl(dacl, None, False)
    monkeypatch.setattr(sec, "GetNamedSecurityInfo", lambda *args: descriptor)
    assert not native([tmp_path], [])


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\0" * 20,
        struct.pack("<BBHIIII", 1, 0, 0x8000, 0, 0, 0, 100),
        struct.pack("<BBHIIII", 1, 0, 0x8000, 0, 0, 0, 20) + b"\0" * 8,
        struct.pack("<BBHIIII", 1, 0, 0x8000, 0, 0, 0, 20) + struct.pack("<BBHHH", 2, 0, 8, 1, 0),
        struct.pack("<BBHIIII", 1, 0, 0x8000, 0, 0, 0, 20)
        + struct.pack("<BBHHHBBH", 2, 0, 12, 1, 0, 0, 0, 1),
        struct.pack("<BBHIIII", 1, 0, 0x8000, 0, 0, 0, 20)
        + struct.pack("<BBHHHBBH", 2, 0, 12, 1, 0, 22, 0, 4),
    ],
)
def test_binary_parser_refuses_malformed_or_unknown_ace(data):
    with pytest.raises(ValueError):
        list(windows_acl._access_rules(data, True))


def test_interpreter_deadline_kills_owned_child(monkeypatch):
    original = subprocess.Popen
    children = []

    def popen(_argv, **kwargs):
        child = original([sys.executable, "-I", "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(windows_acl, "_VERIFY_TIMEOUT", 0.1)
    started = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        windows_acl.check_private_paths(["not-read-by-stalled-child"])
    assert time.monotonic() - started < 3
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed


@WINDOWS
def test_real_setup_atomic_config_and_runtime(tmp_path, security):
    from acp_gateway.cli import wizard
    from acp_gateway.connectors.control import read_credential

    sec, sid = security
    set_dacl(tmp_path, sec, f"D:P(A;OICI;FA;;;{sid})")
    root = tmp_path / "config" / "runtime"
    windows_acl.ensure_private_directory(root)
    path = root / "settings.env"
    credential = "acpc_" + "a" * 43
    content = (credential + "\n").encode()
    wizard._atomic(path, content)
    assert wizard._snapshot(path) == content
    assert read_credential(path) == credential
    # Do not repair an unsafe existing file. Fail before replacing original bytes.
    set_dacl(path, sec, f"D:P(A;;FA;;;{sid})(A;;FR;;;WD)")
    with pytest.raises(ValueError, match="private Windows ACLs"):
        wizard._snapshot(path)
