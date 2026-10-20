"""Deployment preparation wires the host updater, never a gateway Docker socket."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def load_prepare():
    path = Path(__file__).resolve().parents[2] / "deploy/docker/prepare.py"
    spec = importlib.util.spec_from_file_location("docker_prepare", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_prepare_automatically_runs_exact_host_bootstrap(tmp_path, monkeypatch):
    prepare = load_prepare()
    root = tmp_path / "deploy/docker"
    root.mkdir(parents=True)
    (tmp_path / "release.json").write_text(json.dumps({"version": "0.3.0"}))
    calls = []
    monkeypatch.setattr(prepare, "uv_executable", lambda: "/usr/bin/uv", raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append((command, kwargs))
            or subprocess.CompletedProcess(
                command,
                2,
                json.dumps(
                    {
                        "ready": False,
                        "reason": "Docker host companion prepared; start gateway",
                        "policy": "/private/updater.json",
                    }
                ),
            )
        ),
    )
    assert (
        prepare.prepare_updater(root, [root / "compose.yaml", root / "compose.proxy.yaml"])
        == "prepared"
    )
    command, kwargs = calls[0]
    assert command[:9] == [
        "/usr/bin/uv",
        "tool",
        "run",
        "--python",
        "3.12",
        "--from",
        "acp-gateway==0.3.0",
        "python",
        "-m",
    ]
    assert command[9:12] == ["acp_gateway.update_bootstrap", "docker-host", str(root)]
    assert command[12:] == [
        "--compose-file",
        str(root / "compose.yaml"),
        "--compose-file",
        str(root / "compose.proxy.yaml"),
    ]
    assert kwargs["timeout"] == 300


@pytest.mark.parametrize("opt_out", [False, True])
def test_normal_prepare_owns_host_bootstrap_and_preserves_existing_files(
    tmp_path, monkeypatch, opt_out
):
    prepare = load_prepare()
    (tmp_path / "gateway.example.yaml").write_text("data_dir: /data\n")
    (tmp_path / "nginx.conf.example").write_text("example")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/gateway.env").write_text("PRIVATE=kept\n")
    calls = []
    monkeypatch.setattr(prepare, "prepare_updater", lambda root, files: calls.append((root, files)))
    prepare.main(
        ["--no-updater"] if opt_out else ["--compose-file", "compose.proxy.yaml"], root=tmp_path
    )
    assert (tmp_path / "runtime/gateway.env").read_text() == "PRIVATE=kept\n"
    assert calls == (
        []
        if opt_out
        else [(tmp_path, [tmp_path / "compose.yaml", tmp_path / "compose.proxy.yaml"])]
    )


def test_host_opt_out_is_not_misreported_as_a_ready_worker(tmp_path, monkeypatch):
    prepare = load_prepare()
    root = tmp_path / "deploy/docker"
    root.mkdir(parents=True)
    (tmp_path / "release.json").write_text(json.dumps({"version": "0.3.0"}))
    monkeypatch.setattr(prepare, "uv_executable", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps({"ready": False, "reason": "disabled", "policy": None})
        ),
    )
    assert prepare.prepare_updater(root, [root / "compose.yaml"]) == "disabled"
