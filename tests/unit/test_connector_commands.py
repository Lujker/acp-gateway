"""The CLI defaults to plain WS and accepts root or reverse-proxy paths."""

from contextlib import asynccontextmanager

import pytest

from acp_gateway.cli.main import main
from acp_gateway.connectors.control import ControlDispatcher
from acp_gateway.storage import Store


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data_dir: {tmp_path / 'data'}\n"
        "agents:\n  - alias: goose\n    url: ws://127.0.0.1:1/acp\n    default_cwd: /work\n"
    )
    return path


def test_dispatcher_cli_runs_without_tls_arguments(config, monkeypatch):
    captured = {}

    @asynccontextmanager
    async def listen(self, host, port, *, tls=None):
        captured.update(host=host, port=port, tls=tls, path=self.connect_path)
        raise KeyboardInterrupt
        yield  # pragma: no cover — async context manager syntax

    monkeypatch.setattr(ControlDispatcher, "listen", listen)
    assert (
        main(
            [
                "--config",
                str(config),
                "dispatcher",
                "--host",
                "0.0.0.0",  # noqa: S104 — the listener is mocked
                "--port",
                "9876",
                "--connect-path",
                "/gateway/connect",
            ]
        )
        == 130
    )
    assert captured == {
        "host": "0.0.0.0",  # noqa: S104
        "port": 9876,
        "tls": None,
        "path": "/gateway/connect",
    }


@pytest.mark.parametrize("argument", ["--tls-cert", "--tls-key"])
def test_dispatcher_requires_matching_tls_arguments(config, argument, capsys):
    assert main(["--config", str(config), "dispatcher", argument, "/missing.pem"]) == 1
    assert "provide both" in capsys.readouterr().err


def test_invalid_path_is_rejected_before_opening_database(config, capsys):
    assert main(["--config", str(config), "dispatcher", "--connect-path", "relative"]) == 1
    assert "connection path" in capsys.readouterr().err
    assert not (config.parent / "data" / "gateway.db").exists()


def test_connector_cli_needs_no_tls_pin_for_ws(config, tmp_path, monkeypatch):
    key = tmp_path / "computer.key"
    store = Store.open_in(tmp_path / "data")
    store.computers.issue("work", key, display_name="Work")
    store.close()
    captured = {}

    async def connect(url, **kwargs):
        captured.update(url=url, **kwargs)

    monkeypatch.setattr("acp_gateway.connectors.commands.run_connector", connect)
    url = "ws://192.0.2.1:9876/gateway/connect"
    assert (
        main(
            [
                "--config",
                str(config),
                "connector",
                "--dispatcher-url",
                url,
                "--computer-id",
                "work",
                "--token-file",
                str(key),
            ]
        )
        == 0
    )
    assert captured["url"] == url
    assert captured["fingerprint"] is None
    assert captured["hello"].computer_id == "work"
    assert captured["hello"].agents[0].alias == "goose"
