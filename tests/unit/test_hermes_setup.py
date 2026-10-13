import json

import pytest
import yaml

from acp_gateway.cli import hermes_setup as h
from acp_gateway.cli import wizard as w
from fakes.setup_prompts import Answers, save_answers


@pytest.fixture
def discovered(tmp_path, monkeypatch):
    rows = [
        {
            "status": "ready",
            "role": "desktop-serve",
            "version": "0.21.6",
            "profiles": ["personal", "work"],
            "active_sessions": 4,
        },
        {
            "status": "ready",
            "role": "serve",
            "version": "0.21.6",
            "profiles": ["default"],
            "active_sessions": 0,
        },
    ]
    monkeypatch.setattr(h, "inspect", lambda *args, **kwargs: rows)
    monkeypatch.setattr(h, "default_lock_dir", lambda: tmp_path / "locks")
    monkeypatch.setattr(w, "_managed_running", lambda *args: False)
    monkeypatch.setattr(w, "_check_ports", lambda *args: None)
    which = w.shutil.which
    monkeypatch.setattr(
        w.shutil, "which", lambda name: which(name) if name == "powershell.exe" else None
    )
    return rows


def test_select_existing_instance_profile_saves_only_discovery_recipe(discovered, tmp_path):
    result = h.select(
        Answers(**{"Existing Hermes instance": "desktop-serve", "Hermes profile": "work"}), {}
    )
    assert result == {
        "role": "desktop-serve",
        "profile": "work",
        "lock_dir": str(tmp_path / "locks"),
    }
    assert not {"pid", "port", "token", "endpoint"} & result.keys()


def test_unavailable_setup_needs_explicit_offline_choice(discovered):
    discovered[:] = [{"status": "missing", "role": "serve", "detail": "not running"}]
    with pytest.raises(ValueError, match="No verified Hermes"):
        h.select(Answers(), {})
    result = h.select(
        Answers(**{"Save this Hermes attachment without a live verification?": True}), {}
    )
    assert result["role"] == "desktop-serve"


def test_hermes_setup_cancel_and_repeat_preserve_credentials(discovered, tmp_path):
    directory = tmp_path / "config"
    answers = {"Agent type": "hermes", "Hermes profile": "work"}
    assert w.wizard(directory, prompts=Answers(**answers)) == 0
    assert not directory.exists()
    assert w.wizard(directory, prompts=save_answers(**answers)) == 0
    before = (directory / ".env").read_bytes()
    raw = yaml.safe_load((directory / "config.yaml").read_text())
    profile = raw["agents"][0]
    assert profile["alias"] == "hermes" and profile["backend"] == "hermes"
    assert profile["hermes"]["profile"] == "work"
    assert not profile.get("url") and not profile.get("secret_env")
    assert w.wizard(directory, prompts=save_answers()) == 0
    assert (directory / ".env").read_bytes() == before


def test_switch_goose_to_hermes_discards_mode_url_and_keeps_saved_secret(discovered):
    old = {
        "alias": "agent",
        "kind": "goose",
        "url": "ws://127.0.0.1:3284/acp",
        "default_cwd": "/work",
        "session_mode": "smart_approve",
        "secret_env": "GOOSE_SECRET",
    }
    values = {"GOOSE_SECRET": "keep-private"}
    result = w._agent(
        Answers(**{"Agent type": "hermes", "Hermes profile": "personal"}), values, old=old
    )
    assert result["backend"] == "hermes" and not result.get("session_mode")
    assert not result.get("url") and not result.get("secret_env")
    assert values == {"GOOSE_SECRET": "keep-private"}


def test_server_hermes_route_does_not_discover_or_store_host_fields(discovered, monkeypatch):
    monkeypatch.setattr(
        h, "inspect", lambda *a, **k: pytest.fail("VPS must not discover a host agent")
    )
    result = w._agent(Answers(**{"Agent type": "hermes"}), {}, routed=True)
    assert result["backend"] == "connector" and result["computer_id"] == "home"
    assert "hermes" not in result


def test_connector_accepts_hermes_attachment(discovered, tmp_path):
    key = tmp_path / "computer.key"
    key.write_text("acpc_" + "x" * 43)
    key.chmod(0o600)
    directory = tmp_path / "connector"
    assert (
        w.wizard(
            directory,
            prompts=save_answers(
                **{
                    "Role": "connector",
                    "Agent type": "hermes",
                    "Enrollment key file copied privately from the server": str(key),
                }
            ),
        )
        == 0
    )
    assert json.loads((directory / w.STATE).read_text())["role"] == "connector"
