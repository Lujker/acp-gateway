import json
import os
import sys

import httpx
import pytest

from acp_gateway import installation
from acp_gateway.cli.main import main


@pytest.fixture
def uv_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(
        installation,
        "installation_info",
        lambda: {"version": "0.1.0", "manager": "uv", "source": "file"},
    )
    monkeypatch.setattr(installation, "_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(installation.paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(installation, "_service_preflight", lambda **kwargs: [])
    return tmp_path


def test_version_without_configuration(monkeypatch, capsys):
    monkeypatch.setattr(
        installation,
        "installation_info",
        lambda: {"version": "0.1.0", "manager": "unmanaged", "source": "editable"},
    )
    assert main(["--config", "/missing/config.yaml", "version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "editable"


@pytest.mark.parametrize("manager", ["unmanaged", "binary"])
def test_other_installations_are_not_modified(monkeypatch, capsys, manager):
    monkeypatch.setattr(
        installation,
        "installation_info",
        lambda: {"version": "0.1.0", "manager": manager, "source": "file"},
    )
    monkeypatch.setattr(os, "execv", lambda *args: pytest.fail("unexpected mutation"))
    assert main(["uninstall"]) == 1
    assert "original package manager" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "linux", reason="Linux runtime lease")
def test_active_runtime_blocks_mutation(uv_tool, monkeypatch, capsys):
    monkeypatch.setattr(os, "execv", lambda *args: pytest.fail("active runtime modified"))
    with installation.runtime_lease():
        # Different descriptors in one process also conflict on Linux flock.
        assert main(["update", "--version", "0.2.0"]) == 1
        assert main(["uninstall"]) == 1
    assert "installation is in use" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "linux", reason="Linux runtime lease")
def test_update_uses_named_requirement_and_holds_lease(uv_tool, monkeypatch):
    calls = []

    def execute(args, argv, descriptor):
        calls.append(argv)
        with pytest.raises(ValueError, match="in use"), installation.runtime_lease():
            pass

    monkeypatch.setattr(installation, "_handoff", execute)
    assert main(["update", "--from", "https://example.com/acp_gateway-0.2.0-py3-none-any.whl"]) == 0
    assert calls == [
        [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            "--python",
            "3.12",
            "acp-gateway @ https://example.com/acp_gateway-0.2.0-py3-none-any.whl",
        ]
    ]


def test_file_install_needs_explicit_source(uv_tool, monkeypatch, capsys):
    monkeypatch.setattr(os, "execv", lambda *args: pytest.fail("unexpected mutation"))
    assert main(["update"]) == 1
    assert "require update --from" in capsys.readouterr().err


@pytest.mark.parametrize("source", ["--help", "other-package", "https://example.com/\nsecret"])
def test_invalid_sources_are_rejected(uv_tool, capsys, source):
    assert main(["update", f"--from={source}"]) == 1


def test_dry_run_does_not_remove_units(uv_tool, monkeypatch):
    monkeypatch.setattr(
        installation, "_service_preflight", lambda **kwargs: pytest.fail("mutation")
    )
    assert main(["uninstall", "--dry-run"]) == 0


def test_services_are_all_preflighted_before_uninstall(monkeypatch, tmp_path):
    from acp_gateway import service

    for role, marker in (("gateway", service.MARKER), ("connector", service.CONNECTOR_MARKER)):
        (tmp_path / role).write_text(
            marker + "ExecStart=" + service._quote(installation.sys.executable) + " serve\n"
        )
    monkeypatch.setattr(service, "unit_path", lambda role: tmp_path / role)
    monkeypatch.setattr(
        service,
        "_systemctl",
        lambda *args: "active" if service.CONNECTOR_UNIT in args else "inactive",
    )
    monkeypatch.setattr(
        service, "control", lambda *args, **kwargs: pytest.fail("partial uninstall")
    )
    with pytest.raises(ValueError, match="--role connector stop"):
        installation._service_preflight(uninstall=True)


@pytest.mark.parametrize("foreign", [False, True])
def test_update_check_ignores_yanked_and_prerelease(monkeypatch, capsys, foreign):
    data = {
        "info": {
            "project_urls": {
                "Repository": "https://example.com/foreign" if foreign else installation.REPOSITORY
            }
        },
        "releases": {"0.1.0": [{}], "0.2.0": [{}], "9.0.0": [{"yanked": True}], "10.0.0a1": [{}]},
    }

    def get(client, url):
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    if foreign:
        with pytest.raises(ValueError, match="does not identify"):
            installation.check_update()
    else:
        assert installation.check_update() == 0
        assert "latest stable PyPI release: 0.2.0" in capsys.readouterr().out


def test_manager_checks_current_environment_not_just_uv_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(installation.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(installation, "_tool_directory", lambda executable: tmp_path)
    monkeypatch.setattr(installation, "_source", lambda: "file")
    monkeypatch.setattr(installation.sys, "prefix", str(tmp_path / "another-environment"))
    assert installation.installation_info()["manager"] == "unmanaged"
    monkeypatch.setattr(installation.sys, "prefix", str(tmp_path / "acp-gateway"))
    assert installation.installation_info()["manager"] == "uv"
