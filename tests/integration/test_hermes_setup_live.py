"""Setup/doctor against real loopback Hermes discovery and HTTP/WS mocks."""

import asyncio
import json
import secrets

import pytest
import yaml

from acp_gateway.cli import hermes_setup as h
from acp_gateway.cli import wizard as w
from acp_gateway.cli.doctor import collect
from acp_gateway.cli.probe import probe_hermes
from acp_gateway.config import load_config
from fakes.fake_hermes import FakeHermes
from fakes.setup_prompts import save_answers


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch):
    monkeypatch.setattr(w, "_managed_running", lambda *a: False)
    monkeypatch.setattr(w, "_check_ports", lambda *a: None)
    which = w.shutil.which
    # shutil is shared with native ACL verification; hide only service tools.
    monkeypatch.setattr(
        w.shutil, "which", lambda name: which(name) if name == "powershell.exe" else None
    )


async def test_setup_and_live_doctor_probe_two_backends_without_mutations(tmp_path, capsys):
    async with (
        FakeHermes(tmp_path / "first-locks") as first,
        FakeHermes(tmp_path / "second-locks") as second,
    ):
        prompts = save_answers(**{"Agent type": "hermes"})
        original_ask = prompts.ask
        original_yes = prompts.yes
        directories = iter((str(first.directory), str(second.directory)))
        aliases = iter(("first", "second"))
        more = iter((True, False))

        def ask(label, *args, **kwargs):
            if label == "Hermes discovery directory":
                return next(directories)
            if label == "Agent alias":
                return next(aliases)
            return original_ask(label, *args, **kwargs)

        def yes(label, *args, **kwargs):
            return (
                next(more)
                if label == "Add another agent?"
                else original_yes(label, *args, **kwargs)
            )

        prompts.ask, prompts.yes = ask, yes
        directory = tmp_path / "gateway"
        assert await asyncio.to_thread(w.wizard, directory, prompts=prompts) == 0
        cfg = load_config(directory / "config.yaml", directory / ".env")
        assert [p.alias for p in cfg.settings.agents] == ["first", "second"]
        contents = (directory / "config.yaml").read_text()
        assert first.token not in contents and second.token not in contents
        assert "pid:" not in contents and "port: " not in json.dumps(
            yaml.safe_load(contents)["agents"]
        )
        checks = await asyncio.to_thread(
            collect, directory / "config.yaml", directory / ".env", live=True, timeout=1
        )
        assert all(
            any(c["name"] == f"agent:{name}" and c["status"] == "ok" for c in checks)
            for name in ("first", "second")
        )
        for backend in (first, second):
            assert not backend.sessions and not backend.replies
            assert {call["method"] for call in backend.calls} == {
                "ping",
                "gateway.capabilities",
                "session.active_list",
            }
        output = capsys.readouterr().out
        assert first.token not in output and second.token not in output
        assert "0.21.6" in output


async def test_profile_mismatch_is_explicit_and_probe_is_read_only(tmp_path):
    async with FakeHermes(tmp_path / "locks") as backend:
        row = (
            await asyncio.to_thread(
                h.inspect, backend.directory, role="desktop-serve", profile="missing-profile"
            )
        )[0]
        assert row["status"] == "profile-missing"
        assert not backend.sessions and not backend.replies


async def test_probe_distinguishes_invalid_record_auth_and_offline(tmp_path):
    directory = tmp_path / "locks"
    async with FakeHermes(directory) as backend:
        backend.token = secrets.token_urlsafe(32)
        row = (await probe_hermes(directory, role="desktop-serve", timeout=1))[0]
        assert row["status"] == "auth-required"
    row = (await probe_hermes(directory, role="desktop-serve", timeout=0.1))[0]
    assert row["status"] == "offline"
    (directory / "host-desktop-serve.json").write_text("not json")
    row = (await probe_hermes(directory, role="desktop-serve"))[0]
    assert row["status"] == "incompatible"
