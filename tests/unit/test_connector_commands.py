"""The CLI defaults to plain WS and accepts root or reverse-proxy paths."""

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager

import pytest
from filelock import FileLock, Timeout

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
    # Registration is rebuilt on reconnect after a profile change.
    hello = captured["hello"]()
    assert hello.computer_id == "work"
    assert hello.agents[0].alias == "goose"


@pytest.fixture
def connector_args(config, tmp_path):
    key = tmp_path / "computer.key"
    store = Store.open_in(tmp_path / "data")
    try:
        store.computers.issue("work", key, display_name="Work")
    finally:
        store.close()
    return [
        "--config",
        str(config),
        "connector",
        "--dispatcher-url",
        "ws://192.0.2.1:9876/gateway/connect",
        "--computer-id",
        "work",
        "--token-file",
        str(key),
    ]


@pytest.mark.parametrize("crash", [False, True])
def test_connector_rejects_other_process_before_connecting(
    connector_args, tmp_path, monkeypatch, capsys, crash
):
    async def unexpected(*args, **kwargs):
        pytest.fail("duplicate connector must not contact the dispatcher or agent")

    monkeypatch.setattr("acp_gateway.connectors.commands.run_connector", unexpected)
    lock_path = tmp_path / "data" / "connector.lock"
    script = (
        "import sys; from filelock import FileLock; "
        "lock = FileLock(sys.argv[1], timeout=0); lock.acquire(); "
        "print('locked', flush=True); sys.stdin.read(); lock.release()"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        assert main(connector_args) == 78
        assert "another computer connector" in capsys.readouterr().err
    finally:
        if crash:
            process.terminate()
        process.communicate(timeout=10)
    if not crash:
        assert process.returncode == 0
    # POSIX retains the file; Windows may remove it on release. Both must permit reacquisition.
    if sys.platform != "win32":
        assert lock_path.exists()

    async def connect(*args, **kwargs):
        pass

    monkeypatch.setattr("acp_gateway.connectors.commands.run_connector", connect)
    assert main(connector_args) == 0


@pytest.mark.parametrize("outcome", ["normal", "interrupt", "error"])
def test_connector_holds_lock_and_releases_after_exit(
    connector_args, tmp_path, monkeypatch, outcome
):
    lock_path = tmp_path / "data" / "connector.lock"

    async def connect(*args, **kwargs):
        with pytest.raises(Timeout):
            FileLock(lock_path, timeout=0).acquire()
        if outcome == "interrupt":
            raise KeyboardInterrupt
        if outcome == "error":
            raise OSError("connection failed")

    monkeypatch.setattr("acp_gateway.connectors.commands.run_connector", connect)
    expected = {"normal": 0, "interrupt": 130, "error": 78}[outcome]
    # Dispatcher and gateway use separate locks and can share the directory.
    with (
        FileLock(tmp_path / "data" / "dispatcher.lock", timeout=0),
        FileLock(tmp_path / "data" / "daemon.lock", timeout=0),
    ):
        assert main(connector_args) == expected
    with FileLock(lock_path, timeout=0):
        pass


def test_connector_graceful_stop_waits_for_connection_cleanup(
    connector_args, tmp_path, monkeypatch, capsys
):
    from acp_gateway.connectors.lifecycle import RUNTIME_FILE, STOP_FILE

    cleaned = []
    directory = tmp_path / "data"

    async def connect(*args, **kwargs):
        record = json.loads((directory / RUNTIME_FILE).read_text())
        (directory / STOP_FILE).write_text(
            json.dumps({"version": 1, "instance": record["instance"]})
        )
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0.01)
            cleaned.append(True)

    monkeypatch.setattr("acp_gateway.connectors.commands.run_connector", connect)
    assert main(connector_args) == 0
    assert cleaned == [True]
    assert not (directory / RUNTIME_FILE).exists()


def test_task_control_identity_is_validated_and_published(connector_args, tmp_path, monkeypatch):
    from acp_gateway.connectors.lifecycle import RUNTIME_FILE

    async def connect(*args, **kwargs):
        record = json.loads((tmp_path / "data" / RUNTIME_FILE).read_text())
        assert record["controlId"] == "a" * 32

    monkeypatch.setattr("acp_gateway.connectors.commands.run_connector", connect)
    assert main([*connector_args, "--control-id", "a" * 32]) == 0
    assert main([*connector_args, "--control-id", "invalid"]) == 78
