"""service install compares .env values literally, exactly as config loading reads them."""

import subprocess

from acp_gateway import service
from acp_gateway.config import load_config
from acp_gateway.setup import setup


def test_install_accepts_secret_that_looks_like_interpolation(tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ACPGW_EXPANDED", "expanded")
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    setup(tmp_path)
    env = tmp_path / ".env"
    literal = "tok-${ACPGW_EXPANDED}-" + "x" * 32
    lines = [
        f"ACPGW_API_TOKEN={literal}" if line.startswith("ACPGW_API_TOKEN=") else line
        for line in env.read_text().splitlines()
    ]
    env.write_text("\n".join(lines) + "\n")
    cfg = load_config(tmp_path / "config.yaml", env)
    assert cfg.secrets.get("ACPGW_API_TOKEN").get_secret_value() == literal
    assert service.install(cfg) == 0
