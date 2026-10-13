import hashlib
import json
import os
import secrets
import sys
from types import SimpleNamespace

import pytest

from acp_gateway.discovery.hermes import (
    HermesDiscoveryError,
    default_lock_dir,
    read_endpoint,
)
from acp_gateway.discovery.hermes_rpc import stored_session_id


def test_create_and_activate_use_the_same_stored_identity():
    # Hermes 0.21.6 create uses stored_session_id; activate may use session_key.
    created = {"session_id": "live-1", "stored_session_id": "stored-1"}
    activated = {"session_id": "live-1", "session_key": "stored-1"}
    assert stored_session_id(created) == stored_session_id(activated) == "stored-1"
    assert stored_session_id({"session_id": "live-1"}) is None
    with pytest.raises(HermesDiscoveryError, match="conflicting"):
        stored_session_id({"stored_session_id": "one", "session_key": "other"})
    with pytest.raises(HermesDiscoveryError, match="invalid"):
        stored_session_id({"session_key": 123})


@pytest.fixture
def rendezvous(tmp_path):
    root = tmp_path / "locks"
    root.mkdir(mode=0o700)
    token = secrets.token_urlsafe(32)
    record = {
        "role": "desktop-serve",
        "pid": 1234,
        "host": "127.0.0.1",
        "port": 4549,
        "protocolVersion": 1,
        "profiles": ["default"],
        "tokenFingerprint": hashlib.sha256(token.encode()).hexdigest()[:16],
    }

    def write(**overrides):
        for name, value in (
            ("json", json.dumps(record | overrides)),
            ("token", token),
        ):
            path = root / f"host-desktop-serve.{name}"
            path.write_text(value)
            path.chmod(0o600)
        return root

    write()
    return root, record, token, write


def test_local_endpoint_and_token_fingerprint(rendezvous):
    root, _, token, write = rendezvous
    endpoint = read_endpoint(root, "desktop-serve")
    assert endpoint.base_url == "http://127.0.0.1:4549"
    assert token not in repr(endpoint)
    endpoint.check_identity(
        {"ok": True, "protocolVersion": 1, "pid": 1234, "role": "desktop-serve"}
    )
    with pytest.raises(HermesDiscoveryError, match="identity"):
        endpoint.check_identity(
            {"ok": True, "protocolVersion": 1, "pid": 9, "role": "desktop-serve"}
        )
    write(host="::1")
    assert read_endpoint(root, "desktop-serve").base_url == "http://[::1]:4549"
    write(tokenFingerprint="obsolete")
    with pytest.raises(HermesDiscoveryError, match="do not match") as error:
        read_endpoint(root, "desktop-serve")
    assert token not in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "203.0.113.1"},
        {"host": "localhost"},
        {"host": "user:secret@127.0.0.1"},
        {"pid": True},
        {"port": True},
        {"port": 0},
        {"protocolVersion": True},
        {"protocolVersion": 2},
        {"profiles": ["default", "default"]},
        {"profiles": []},
        {"role": "serve"},
    ],
)
def test_untrusted_records_are_not_network_targets(rendezvous, overrides):
    root, _, _, write = rendezvous
    write(**overrides)
    with pytest.raises(HermesDiscoveryError, match="invalid or unsupported"):
        read_endpoint(root, "desktop-serve")


def test_private_file_bounds_and_symlinks(rendezvous):
    root, _, _, _ = rendezvous
    record = root / "host-desktop-serve.json"
    record.write_bytes(b" " * 65537)
    with pytest.raises(HermesDiscoveryError, match="size limit"):
        read_endpoint(root, "desktop-serve")
    target = root / "original.json"
    record.rename(target)
    record.symlink_to(target)
    reason = "private Windows ACLs" if sys.platform == "win32" else "regular file"
    with pytest.raises(HermesDiscoveryError, match=reason):
        read_endpoint(root, "desktop-serve")


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and permissions")
def test_shared_token_is_refused(rendezvous):
    root, _, _, _ = rendezvous
    (root / "host-desktop-serve.token").chmod(0o644)
    with pytest.raises(HermesDiscoveryError, match="private"):
        read_endpoint(root, "desktop-serve")


def test_discovery_uses_this_environment_only(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_GATEWAY_LOCK_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_lock_dir() == tmp_path / "hermes" / "gateway-locks"
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", "relative")
    with pytest.raises(HermesDiscoveryError, match="absolute"):
        default_lock_dir()


def test_windows_acl_denial_precedes_reading_any_discovery_bytes(rendezvous, monkeypatch):
    from acp_gateway import windows_acl
    from acp_gateway.discovery import hermes

    root, _, _, _ = rendezvous
    monkeypatch.setattr(hermes, "sys", SimpleNamespace(platform="win32"))

    def check(files, *, directories):
        assert files == [root / "host-desktop-serve.json", root / "host-desktop-serve.token"]
        assert directories == [root]
        raise ValueError("private Windows ACLs required")

    monkeypatch.setattr(windows_acl, "check_private_paths", check)
    monkeypatch.setattr(
        hermes, "_private_read", lambda *args: pytest.fail("ACL denied before read")
    )
    with pytest.raises(HermesDiscoveryError, match="private") as exc:
        read_endpoint(root, "desktop-serve")
    assert exc.value.status == "auth-required"


def test_windows_acl_checked_before_valid_endpoint_and_missing_stays_missing(
    rendezvous, monkeypatch
):
    from acp_gateway import windows_acl
    from acp_gateway.discovery import hermes

    root, _, token, _ = rendezvous
    monkeypatch.setattr(hermes, "sys", SimpleNamespace(platform="win32"))
    checks = []
    monkeypatch.setattr(
        windows_acl, "check_private_paths", lambda *args, **kwargs: checks.append(args)
    )
    assert read_endpoint(root, "desktop-serve").token == token
    assert len(checks) == 1
    (root / "host-desktop-serve.token").unlink()
    with pytest.raises(FileNotFoundError):
        read_endpoint(root, "desktop-serve")
    assert len(checks) == 1
