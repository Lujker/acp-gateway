"""Wizard readiness checks use the live Hermes observer protocol only."""

import asyncio

import pytest

from acp_gateway.cli import wizard
from acp_gateway.config import AppConfig, SecretStore, Settings
from fakes.fake_hermes import FakeHermes


@pytest.mark.parametrize(
    ("profile", "status"), [("default", "ready"), ("not-advertised", "profile-missing")]
)
async def test_check_connections_hermes_inside_running_loop(tmp_path, capsys, profile, status):
    async with FakeHermes(tmp_path / "locks") as backend:
        settings = Settings.model_validate(
            {
                "agents": [
                    {
                        "alias": "hermes",
                        "kind": "hermes",
                        "backend": "hermes",
                        "default_cwd": str(tmp_path),
                        "hermes": {
                            "lock_dir": str(backend.directory),
                            "role": "desktop-serve",
                            "profile": profile,
                        },
                    }
                ],
                "telegram": {"enabled": False},
            }
        )
        cfg = AppConfig(settings, SecretStore({}), tmp_path / "config.yaml", tmp_path / ".env")

        # The synchronous entry owns its loop; the fake server stays on this loop.
        await asyncio.to_thread(wizard.check_connections, cfg, {"role": "local"})

        assert not backend.sessions
        assert not backend.replies
        assert [call["method"] for call in backend.calls] == [
            "ping",
            "gateway.capabilities",
            "session.active_list",
        ]
        output = capsys.readouterr().out
        assert f"hermes: Hermes {status} (observer check; no session or prompt sent)" in output
        assert backend.token not in output
