import textwrap

import pytest

from acp_gateway.cli.main import main

SECRET = "cli-" + "secret-value-77"

CONFIG = textwrap.dedent(
    """
    agents:
      - alias: work
        title: Work Goose
        kind: goose
        url: wss://work-laptop.lan:3000/acp
        secret_env: AGENT_WORK_SECRET
        default_cwd: /home/user/work
    """
)


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("acpgw ")


def test_config_check_ok_does_not_print_secrets(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / ".env").write_text(f"AGENT_WORK_SECRET={SECRET}\n")

    assert main(["config", "check"]) == 0

    out = capsys.readouterr().out
    assert "work [goose/remote] Work Goose" in out
    assert "AGENT_WORK_SECRET (set)" in out
    assert "trust-on-first-use" in out
    assert SECRET not in out


def test_config_check_fails_on_missing_secret(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text(CONFIG)
    assert main(["config", "check"]) == 1
    assert "missing agent secrets: work: AGENT_WORK_SECRET" in capsys.readouterr().err


def test_config_check_reports_invalid_config(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("gateway:\n  host: 0.0.0.0\n")
    assert main(["config", "check"]) == 2
    assert "loopback" in capsys.readouterr().err


def test_config_check_missing_explicit_file(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "nope.yaml"), "config", "check"]) == 2
    assert "file not found" in capsys.readouterr().err


def test_paths(capsys):
    assert main(["paths"]) == 0
    assert "acp-gateway" in capsys.readouterr().out
