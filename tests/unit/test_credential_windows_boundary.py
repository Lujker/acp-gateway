"""Exercise Windows credential preflight ordering using POSIX fds and ACL doubles."""

import os
from contextlib import closing
from types import SimpleNamespace

import pytest

from acp_gateway import windows_acl
from acp_gateway.connectors import control
from acp_gateway.storage import Store, computers


def windows_os(monkeypatch, module):
    monkeypatch.setattr(module, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd doubles; native ACL tests are separate")
def test_windows_export_checks_parent_and_empty_file_before_secret_bytes(tmp_path, monkeypatch):
    windows_os(monkeypatch, computers)
    target = tmp_path / "new.key"
    calls = []

    def check(files, **kwargs):
        calls.append((files, kwargs))
        if files == [target]:
            assert target.read_bytes() == b""

    monkeypatch.setattr(windows_acl, "check_private_paths", check)
    with closing(Store.open(":memory:")) as store:
        store.computers.issue("home", target, display_name="Home")
        assert store.computers.authenticate("home", target.read_text().strip())
    assert calls[0][0] == [tmp_path]
    assert calls[1][0] == [target]
    assert all(kwargs["directories"] == [tmp_path] for _, kwargs in calls)


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd doubles; native ACL tests are separate")
@pytest.mark.parametrize("failure", ["parent", "file"])
def test_windows_failed_export_cannot_rotate_or_leak_secret(tmp_path, monkeypatch, failure):
    with closing(Store.open(":memory:")) as store:
        old = tmp_path / "old.key"
        store.computers.issue("home", old, display_name="Home")
        previous = old.read_text().strip()
        windows_os(monkeypatch, computers)
        target = tmp_path / "rejected.key"

        def reject(files, **kwargs):
            if failure == "parent" or files == [target]:
                if target.exists():
                    assert target.read_bytes() == b""
                raise ValueError("private Windows ACL required")

        monkeypatch.setattr(windows_acl, "check_private_paths", reject)
        with pytest.raises(ValueError, match="private Windows ACL"):
            store.computers.issue("home", target)
        assert not target.exists() and not store._conn.in_transaction
        assert store.computers.authenticate("home", previous)
        assert store.computers.list()[0]["generation"] == 1


def test_windows_reader_checks_acl_before_open_or_decode(tmp_path, monkeypatch):
    windows_os(monkeypatch, control)
    path = tmp_path / "key"

    def reject(files, **kwargs):
        assert files == [path] and kwargs["directories"] == [tmp_path]
        raise ValueError("private Windows ACL required")

    monkeypatch.setattr(windows_acl, "check_private_paths", reject)

    def unexpected_open(*args):
        pytest.fail("secret was opened before ACL verification")

    monkeypatch.setattr(control.os, "open", unexpected_open)
    with pytest.raises(ValueError, match="private Windows ACL"):
        control.read_credential(path)
