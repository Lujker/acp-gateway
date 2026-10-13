"""Windows setup rejects unsafe persistence before writing credentials."""

import sys

import pytest

from acp_gateway import windows_acl
from acp_gateway.cli import wizard as w


def test_defaults_are_persistent_and_preserve_custom_paths(tmp_path):
    raw = {}
    w._windows_connector_defaults(raw, tmp_path / "config.yaml")
    assert raw == {
        "data_dir": str(tmp_path / "data"),
        "logging": {"file": str(tmp_path / "logs" / "connector.log")},
    }
    w._windows_connector_defaults(raw, tmp_path / "other" / "config.yaml")
    assert raw["data_dir"] == str(tmp_path / "data")


def test_repeat_setup_keeps_implicit_platform_data_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(w.paths, "data_dir", lambda: tmp_path / "existing-data")
    raw = {}
    w._windows_connector_defaults(raw, tmp_path / "config.yaml", existing=True)
    assert raw["data_dir"] == str(tmp_path / "existing-data")


@pytest.mark.parametrize("raw", [{"logging": None}, {"data_dir": 123}, {"logging": {"file": 123}}])
def test_malformed_paths_fail_with_validation_error(tmp_path, raw):
    with pytest.raises(ValueError):
        w._windows_connector_defaults(raw, tmp_path / "config.yaml")


@pytest.mark.parametrize("field", ["data_dir", "logging"])
def test_relative_runtime_paths_are_rejected(tmp_path, field):
    raw = {field: "relative" if field == "data_dir" else {"file": "relative.log"}}
    with pytest.raises(ValueError, match="absolute"):
        w._windows_connector_defaults(raw, tmp_path / "config.yaml")


def test_atomic_acl_failure_writes_no_secret_and_keeps_original(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows_acl, "ensure_private_directory", lambda path: None)
    target = tmp_path / "settings.env"
    target.write_bytes(b"original")

    def reject(files, **kwargs):
        assert len(files) == 1
        assert files[0].read_bytes() == b""
        raise ValueError("private ACL required")

    monkeypatch.setattr(windows_acl, "check_private_paths", reject)
    with pytest.raises(ValueError, match="private ACL"):
        w._atomic(target, b"secret")
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]


def test_snapshot_acl_failure_prevents_read(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    target = tmp_path / "settings.env"
    target.write_bytes(b"secret")

    def reject(files, **kwargs):
        raise ValueError("private ACL required")

    def forbidden_read(path):
        pytest.fail("read before ACL check")

    monkeypatch.setattr(windows_acl, "check_private_paths", reject)
    monkeypatch.setattr(type(target), "read_bytes", forbidden_read)
    with pytest.raises(ValueError, match="private ACL"):
        w._snapshot(target)
