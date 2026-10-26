"""Local permission grant shares guarded configuration/service writes."""

from types import SimpleNamespace

import pytest
import yaml

from acp_gateway.cli import remote_profiles as module
from acp_gateway.cli.main import _build_parser
from acp_gateway.config import AppConfig, SecretStore, Settings


def test_command_is_local_and_explicit():
    args = _build_parser().parse_args(["agents", "allow-remote-profiles"])
    assert args.agent_command == "allow-remote-profiles"


def test_local_grant_makes_backup_without_service(tmp_path, monkeypatch):
    from acp_gateway import service
    from acp_gateway.cli import connection_service

    monkeypatch.setattr(connection_service, "sys", SimpleNamespace(platform="linux"))

    path = tmp_path / "config.yaml"
    raw = {"agents": [], "connector": {"remote_profiles": False}}
    path.write_text(yaml.safe_dump(raw))
    config = AppConfig(Settings(**raw), SecretStore({}), path, None)
    monkeypatch.setattr(module.ConnectorLaunch, "from_saved", lambda config: object())
    monkeypatch.setattr(service, "unit_path", lambda *args: tmp_path / "missing.service")
    assert module.allow(config) == 0
    assert yaml.safe_load(path.read_text())["connector"]["remote_profiles"]
    assert list((tmp_path / "setup-backups").iterdir())


def test_local_grant_passes_selected_config_to_guarded_service_refresh(tmp_path, monkeypatch):
    path = tmp_path / "selected.yaml"
    path.write_text("connector: {remote_profiles: false}\nagents: []\n")
    config = AppConfig(Settings(), SecretStore({}), path, tmp_path / "selected.env")
    launch = object()
    monkeypatch.setattr(module.ConnectorLaunch, "from_saved", lambda config: launch)
    seen = []

    def apply(cfg, selected_launch, files, originals, directory, prompts):
        assert cfg is config and selected_launch is launch
        assert originals[path] == path.read_bytes()
        assert yaml.safe_load(files[path])["connector"]["remote_profiles"]
        assert prompts.yes("refresh")
        seen.append(True)
        return None, True

    monkeypatch.setattr(module, "apply_connection_settings", apply)
    assert module.allow(config) == 0 and seen


@pytest.mark.parametrize("answer", [None, False])
def test_connect_permission_defaults_to_yes_and_honours_opt_out(tmp_path, monkeypatch, answer):
    from acp_gateway.cli import connection_service
    from acp_gateway.cli import wizard as w
    from fakes.setup_prompts import save_answers

    path = tmp_path / "config.yaml"
    path.write_text("agents: []\n")
    (tmp_path / w.STATE).write_text('{"version":1,"role":"connector"}')
    monkeypatch.setattr(w, "_connection", lambda *args, **kwargs: {"computer_id": "home"})
    monkeypatch.setattr(w, "_managed_running", lambda *args: False)
    seen = []

    def apply(cfg, launch, files, originals, directory, prompts):
        seen.append(yaml.safe_load(files[path])["connector"]["remote_profiles"])
        return None, True

    monkeypatch.setattr(connection_service, "apply_connection_settings", apply)
    base = save_answers()

    def yes(label, default=False):
        if label.startswith("Allow the dispatcher"):
            assert default is True
            return default if answer is None else answer
        return base.yes(label, default)

    prompts = SimpleNamespace(yes=yes, ask=base.ask, choice=base.choice)
    assert w.wizard(tmp_path, section="connection", prompts=prompts) == 0
    assert seen == [answer is None]
