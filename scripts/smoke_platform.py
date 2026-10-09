"""Native runner acceptance: uv maintenance, recovery, installer and binary CLI.

Uses explicit temporary config/data and uv tool directories. Does not install
services or contact Telegram or any real agent. Intended for manual CI runners.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def smoke(wheel: Path, binary: Path | None = None):
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    with tempfile.TemporaryDirectory(prefix="acpgw-native-") as temporary:
        root = Path(temporary)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("ACPGW_", "AGENT_", "PYTHON", "UV_TOOL"))
        }
        environment.update(
            {
                "UV_TOOL_DIR": str(root / "tools"),
                "UV_TOOL_BIN_DIR": str(root / "bin"),
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "platform-config"),
                "XDG_DATA_HOME": str(root / "platform-data"),
            }
        )
        config_dir = root / "config"
        config = config_dir / "config.yaml"
        env_file = config_dir / ".env"
        entry = root / "bin" / ("acpgw.exe" if os.name == "nt" else "acpgw")
        prefix = [str(entry), "--config", str(config), "--env-file", str(env_file)]

        def run(argv, *, ok=True, cwd=root):
            result = subprocess.run(  # noqa: S603 — controlled argv
                argv, env=environment, cwd=cwd, capture_output=True, text=True, timeout=240
            )
            if (result.returncode == 0) != ok:
                raise RuntimeError(f"Unexpected exit {result.returncode}: {result.stderr}")
            return result.stdout

        def command(*args, **kwargs):
            return run([*prefix, *args], **kwargs)

        run([uv, "tool", "install", "--python", "3.12", str(wheel.resolve())])
        command("setup", "--config-dir", str(config_dir))
        settings = yaml.safe_load(config.read_text())
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            settings["gateway"]["port"] = listener.getsockname()[1]
        settings["data_dir"] = str(root / "data")
        config.write_text(yaml.safe_dump(settings))
        secret = env_file.read_bytes()
        command(
            "computers",
            "enroll",
            "native",
            "--name",
            "Native",
            "--token-file",
            str(root / "computer.key"),
        )
        registered = json.loads(command("computers", "list"))
        require(
            json.loads(command("version", "--json"))["manager"] == "uv",
            "native acceptance check failed",
        )

        def start():
            log = (root / "daemon.log").open("w")
            child = subprocess.Popen(  # noqa: S603 — temporary installed tool
                [*prefix, "serve"], env=environment, cwd=root, stdout=log, stderr=log
            )
            log.close()
            try:
                for _ in range(50):
                    if child.poll() is not None:
                        raise RuntimeError((root / "daemon.log").read_text())
                    try:
                        command("status")
                        return child
                    except RuntimeError:
                        time.sleep(0.2)
                raise RuntimeError("Daemon did not become ready")
            except BaseException:
                child.kill()
                child.wait(15)
                raise

        child = start()
        try:
            command("update", "--from", str(wheel.resolve()), ok=False)
            command("uninstall", ok=False)
        finally:
            child.terminate()
            child.wait(20)

        fixture = root / "fixture"
        fixture.mkdir()
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy(REPO / name, fixture / name)
        shutil.copytree(REPO / "src", fixture / "src", ignore=shutil.ignore_patterns("__pycache__"))
        project = fixture / "pyproject.toml"
        original = project.read_text()
        import tomllib

        version = tomllib.loads(original)["project"]["version"]
        project.write_text(
            original.replace(f'version = "{version}"', 'version = "0.1.0+native"', 1)
        )
        run([uv, "build", "--wheel", "--out-dir", str(root / "next")], cwd=fixture)
        candidate = next((root / "next").glob("*.whl"))
        command("update", "--from", str(candidate))
        require(
            json.loads(command("version", "--json"))["version"] == "0.1.0+native",
            "native acceptance check failed",
        )
        require(
            json.loads(command("computers", "list")) == registered, "native acceptance check failed"
        )

        # A package that installs successfully but fails its SQL health probe.
        project.write_text(
            original.replace(f'version = "{version}"', 'version = "0.1.0+broken"', 1)
        )
        (fixture / "src/acp_gateway/storage/migrations/0006_broken.sql").write_text("INVALID SQL;")
        run([uv, "build", "--wheel", "--out-dir", str(root / "broken")], cwd=fixture)
        command("update", "--from", str(next((root / "broken").glob("*.whl"))), ok=False)
        require(
            json.loads(command("version", "--json"))["version"] == "0.1.0+native",
            "native acceptance check failed",
        )
        require(
            json.loads(command("computers", "list")) == registered, "native acceptance check failed"
        )
        command("update", "--rollback")
        require(
            json.loads(command("version", "--json"))["version"] == version,
            "native acceptance check failed",
        )
        require(
            json.loads(command("computers", "list")) == registered, "native acceptance check failed"
        )
        require(env_file.read_bytes() == secret, "native acceptance check failed")

        if os.name == "nt":
            installer = root / "installer"
            installer.mkdir()
            shutil.copy(wheel, installer / wheel.name)
            shutil.copy(REPO / "scripts/install_release.ps1", installer / "install.ps1")
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if powershell is None:
                raise RuntimeError("PowerShell is required for the Windows installer check")
            run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(installer / "install.ps1"),
                    "-ConfigDir",
                    str(config_dir),
                ]
            )
        command("uninstall")
        require(not entry.exists(), "native acceptance check failed")
        require(env_file.read_bytes() == secret, "native acceptance check failed")
        require((root / "data/gateway.db").is_file(), "native acceptance check failed")
        if binary:
            run([str(binary.resolve()), "--version"])
            run([str(binary.resolve()), "version", "--json"])
            run([str(binary.resolve()), "setup", "--config-dir", str(root / "binary-config")])
            run(
                [
                    str(binary.resolve()),
                    "--config",
                    str(root / "binary-config/config.yaml"),
                    "--env-file",
                    str(root / "binary-config/.env"),
                    "config",
                    "check",
                ]
            )
        print(
            "PASS: native wheel/daemon, active guard, update, failed-health recovery, rollback, "
            "data-preserving uninstall and optional binary/Windows installer."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, nargs="?")
    parser.add_argument("--wheel-dir", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--built-binary", action="store_true")
    arguments = parser.parse_args()
    if arguments.wheel_dir:
        wheels = list(arguments.wheel_dir.glob("*.whl"))
        require(len(wheels) == 1, "Expected exactly one wheel")
        arguments.wheel = wheels[0]
    if arguments.wheel is None:
        parser.error("wheel or --wheel-dir is required")
    if arguments.built_binary:
        binaries = [
            path
            for path in (REPO / "dist").glob("acpgw-*/*")
            if path.is_file() and path.name in ("acpgw", "acpgw.exe")
        ]
        require(len(binaries) == 1, "Expected exactly one built executable")
        arguments.binary = binaries[0]
    smoke(arguments.wheel, arguments.binary)
