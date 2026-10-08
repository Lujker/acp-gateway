"""Opt-in lifecycle smoke against the real Linux systemd user manager.

Temporarily installs acp-gateway.service; refuses any existing unit. All
application credentials/config/data are temporary and removed on completion.
"""

# ruff: noqa: S101 (explicit assertions in this opt-in smoke test)

import argparse
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from dotenv import dotenv_values

from acp_gateway import service


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, help="installed acpgw command or Linux binary")
    executable = parser.parse_args().executable.resolve(strict=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ACPGW_")}

    def run(*args: str, check=True):
        result = subprocess.run(  # noqa: S603
            args, capture_output=True, text=True, env=env, timeout=30
        )
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result

    def state():
        result = run(
            "systemctl",
            "--user",
            "show",
            service.UNIT,
            "--property=LoadState,ActiveState,UnitFileState,MainPID",
            check=False,
        )
        if "LoadState=" not in result.stdout:
            raise RuntimeError(result.stderr.strip() or "systemd user bus is unavailable")
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    unit = service.unit_path()
    if unit.exists() or unit.is_symlink() or state().get("LoadState") != "not-found":
        raise SystemExit("Refusing to replace an existing acp-gateway.service; stop here.")
    with tempfile.TemporaryDirectory(prefix="acpgw-service-smoke-") as temporary:
        root = Path(temporary)
        run(str(executable), "setup", "--config-dir", str(root))
        config = root / "config.yaml"
        secrets_file = root / ".env"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        settings = yaml.safe_load(config.read_text())
        settings["gateway"]["port"] = port
        settings["data_dir"] = str(root / "data")
        config.write_text(yaml.safe_dump(settings))
        before = (config.read_bytes(), secrets_file.read_bytes())
        token = dotenv_values(secrets_file)["ACPGW_API_TOKEN"]
        command = (str(executable), "--config", str(config), "--env-file", str(secrets_file))

        def control(action):
            run(*command, "service", action)

        def ready(old_pid=None):
            deadline = time.monotonic() + 30
            with httpx.Client(timeout=1, trust_env=False) as client:
                while time.monotonic() < deadline:
                    current = state()
                    try:
                        response = client.get(
                            f"http://127.0.0.1:{port}/health",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        response.raise_for_status()
                        assert response.json()["database"]["schema_version"] >= 2
                        if current["ActiveState"] == "active" and current["MainPID"] != old_pid:
                            return current["MainPID"]
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
            raise RuntimeError(f"Service did not become healthy: {state()}")

        def inactive():
            current = state()
            assert current["ActiveState"] == "inactive", current
            assert current["MainPID"] == "0", current

        try:
            control("install")
            inactive()
            control("enable")
            pid = ready()
            assert state()["UnitFileState"] == "enabled"
            control("status")
            control("stop")
            inactive()
            assert state()["UnitFileState"] == "enabled"
            control("start")
            pid = ready(pid)
            control("restart")
            pid = ready(pid)
            # Real systemd restart-on-failure, not a mocked systemctl process.
            run("systemctl", "--user", "kill", "--signal=SIGKILL", service.UNIT)
            ready(pid)
            sentinel = root / "data" / "preserve-on-uninstall"
            sentinel.write_text("persisted\n")
            control("disable")
            inactive()
            assert state()["UnitFileState"] == "disabled"
            control("enable")
            ready()
            control("uninstall")
            assert not unit.exists()
            assert state()["LoadState"] == "not-found"
            assert before == (config.read_bytes(), secrets_file.read_bytes())
            assert sentinel.read_text() == "persisted\n"
            assert (root / "data" / "gateway.db").is_file()
        finally:
            # Do not remove a unit replaced by another process during this test.
            if unit.is_file() and str(config) in unit.read_text():
                control("uninstall")
    print(
        "Real service smoke passed: install/enable/status/start/stop/restart, "
        "crash recovery, disable/re-enable/uninstall; configuration/data preserved."
    )


if __name__ == "__main__":
    main()
