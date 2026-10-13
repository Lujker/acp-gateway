import pytest

from acp_gateway.cli.main import main
from acp_gateway.cli.menu import menu
from acp_gateway.cli.wizard import Prompts


def choose(monkeypatch, answers):
    iterator = iter(answers)
    monkeypatch.setattr("builtins.input", lambda label: next(iterator))


def test_menu_dispatch_preserves_explicit_paths(tmp_path, monkeypatch):
    config = tmp_path / "custom" / "config.yaml"
    env = tmp_path / "private.env"
    calls = []
    choose(monkeypatch, ["check", "updates", "exit"])
    assert menu(config, env, prompts=Prompts(), execute=lambda args: calls.append(args) or 0) == 0
    prefix = ["--config", str(config), "--env-file", str(env)]
    assert calls == [[*prefix, "config", "check"], [*prefix, "update", "--check"]]


def test_menu_survives_command_error(monkeypatch, capsys):
    choose(monkeypatch, ["status", "updates", "exit"])
    calls = []

    def execute(args):
        calls.append(args)
        if args == ["status"]:
            raise ValueError("gateway offline")
        return 0

    assert menu(prompts=Prompts(), execute=execute) == 0
    assert "gateway offline" in capsys.readouterr().err
    assert calls == [["status"], ["update", "--check"]]


def test_menu_service_uses_saved_connector_role_and_confirms_stop(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    (tmp_path / "setup.json").write_text(
        '{"version": 1, "role": "connector", "connection": '
        '{"dispatcher_url": "wss://example.test", "computer_id": "home", "token_file": "/key"}}'
    )
    calls = []
    choose(monkeypatch, ["service", "stop", "no", "service", "status", "exit"])
    assert menu(config, prompts=Prompts(), execute=lambda args: calls.append(args) or 0) == 0
    assert len(calls) == 1
    assert calls[0][-4:] == ["service", "--role", "connector", "status"]


def test_no_arguments_terminal_opens_menu(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    calls = []
    monkeypatch.setattr("acp_gateway.cli.menu.menu", lambda *args: calls.append(args) or 0)
    assert main([]) == 0
    assert calls == [(None, None)]


def test_no_arguments_without_terminal_keeps_argparse_help():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
