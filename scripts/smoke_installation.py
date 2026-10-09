"""Real isolated uv install/update/uninstall and optional local Git acceptance.

No host installation, services, configuration, bot or user database is changed.
The second wheel is a private fixture built from the same source with a distinct
version; it is never copied to release output or published.
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from acp_gateway.log import redact_text

REPO = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def smoke(wheel: Path, git_ref: str | None, bundle: Path | None = None):
    uv = shutil.which("uv")
    require(uv is not None, "uv is required")
    wheel = wheel.resolve()
    require(wheel.is_file(), "wheel does not exist")
    with tempfile.TemporaryDirectory(prefix="acpgw-install-") as directory:
        root = Path(directory)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("ACPGW_", "AGENT_", "PYTHON", "UV_TOOL"))
        }
        env.update(
            {
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "UV_TOOL_DIR": str(root / "tools"),
                "UV_TOOL_BIN_DIR": str(root / "bin"),
            }
        )

        def run(*command, ok=True, cwd=root):
            result = subprocess.run(  # noqa: S603 — controlled argv, no shell
                command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180
            )
            require(
                (result.returncode == 0) == ok,
                f"unexpected exit {result.returncode}: {redact_text(result.stderr)}",
            )
            return result.stdout + result.stderr

        def command(*args, **kwargs):
            return run(str(root / "bin/acpgw"), *args, **kwargs)

        def start():
            output = (root / "daemon.log").open("w")
            process = subprocess.Popen(  # noqa: S603
                [str(root / "bin/acpgw"), "serve"],
                env=env,
                cwd=root,
                stdout=output,
                stderr=output,
            )
            output.close()
            for _ in range(100):
                require(process.poll() is None, "installed daemon exited during startup")
                result = subprocess.run(  # noqa: S603
                    [str(root / "bin/acpgw"), "status"],
                    env=env,
                    cwd=root,
                    capture_output=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    return process
                time.sleep(0.1)
            process.kill()
            process.wait(10)
            raise RuntimeError("installed daemon did not become ready")

        def stop(process):
            process.send_signal(signal.SIGINT)
            try:
                process.wait(20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(10)
                raise RuntimeError("installed daemon did not stop") from None

        run(uv, "tool", "install", "--python", "3.12", str(wheel))
        installed = json.loads(command("version", "--json"))
        require(installed["manager"] == "uv", "uv installation not recognized")
        command("setup")
        configuration = root / "config/acp-gateway/config.yaml"
        secret_file = configuration.parent / ".env"
        settings = yaml.safe_load(configuration.read_text())
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            settings["gateway"]["port"] = listener.getsockname()[1]
        configuration.write_text(yaml.safe_dump(settings))
        original = secret_file.read_bytes()
        command("setup")
        require(secret_file.read_bytes() == original, "setup changed tokens")
        key = root / "computer.key"
        command("computers", "enroll", "smoke", "--name", "Smoke", "--token-file", str(key))
        credential = key.read_bytes()
        registered = json.loads(command("computers", "list"))
        command("config", "check")
        process = start()
        try:
            denied = command("update", "--from", str(wheel), ok=False)
            require("installation is in use" in denied, "active update was not refused")
            require(
                "installation is in use" in command("uninstall", ok=False),
                "active uninstall was not refused",
            )
        finally:
            stop(process)

        fixture = root / "fixture"
        fixture.mkdir()
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy(REPO / name, fixture / name)
        metadata = fixture / "pyproject.toml"
        metadata.write_text(
            metadata.read_text().replace(
                f'version = "{installed["version"]}"', 'version = "0.1.0+acceptance"', 1
            )
        )
        shutil.copytree(REPO / "src", fixture / "src", ignore=shutil.ignore_patterns("__pycache__"))
        run(uv, "build", "--wheel", "--out-dir", str(root / "next"), cwd=fixture)
        next_wheel = next((root / "next").glob("*.whl"))
        command("update", "--from", str(next_wheel))
        require(
            json.loads(command("version", "--json"))["version"] == "0.1.0+acceptance",
            "second version was not installed",
        )
        command("update", "--from", (root / "missing.whl").as_uri(), ok=False)
        require(
            json.loads(command("version", "--json"))["version"] == "0.1.0+acceptance",
            "failed install removed the previous version",
        )
        require(json.loads(command("computers", "list")) == registered, "enrollment lost on update")
        process = start()
        stop(process)
        # Full prompt/approval/MCP/persistence suite using the installed wheel,
        # in a separate private HOME and an empty child PATH.
        run(
            sys.executable,
            str(REPO / "scripts/smoke_binary.py"),
            str(root / "tools/acp-gateway/bin/acpgw"),
            "--installed-tool",
        )
        database = root / "data/acp-gateway/gateway.db"
        saved_database = database.read_bytes()
        saved_config = configuration.read_bytes()
        command("uninstall")
        require(not (root / "bin/acpgw").exists(), "tool entry point was not removed")
        require(secret_file.read_bytes() == original, "tokens changed")
        require(configuration.read_bytes() == saved_config, "config changed on uninstall")
        require(key.read_bytes() == credential, "enrollment key changed")
        require(database.read_bytes() == saved_database, "database changed on uninstall")
        print(
            "PASS: wheel install, setup, real version change, active-runtime refusal, failed "
            "update, daemon/API/ACP/MCP/approvals and data-preserving uninstall.",
            flush=True,
        )

        if bundle:
            import tarfile

            unpacked = root / "bundle"
            unpacked.mkdir()
            with tarfile.open(bundle.resolve()) as archive:
                archive.extractall(unpacked, filter="data")
            installer = next(unpacked.glob("*/install.sh"))
            shell = shutil.which("sh")
            require(shell is not None, "sh is required for the bundle installer")
            run(shell, str(installer))
            require(
                json.loads(command("version", "--json"))["manager"] == "uv",
                "bundle installer did not install a uv tool",
            )
            require(secret_file.read_bytes() == original, "bundle install replaced tokens")
            process = start()
            stop(process)
            command("uninstall")
            print("PASS: release bundle installer outside the repository.", flush=True)

        if git_ref:
            git = shutil.which("git")
            shell = shutil.which("sh")
            require(git is not None and shell is not None, "git and sh are required")
            checkout = root / "checkout"
            run(git, "clone", "--quiet", "--no-hardlinks", str(REPO), str(checkout))
            run(git, "checkout", "--quiet", git_ref, cwd=checkout)
            run(shell, str(checkout / "scripts/install.sh"))
            require(
                json.loads(command("version", "--json"))["manager"] == "uv",
                "checkout install not recognized",
            )
            require(secret_file.read_bytes() == original, "Git install replaced tokens")
            require(json.loads(command("computers", "list")) == registered, "Git install lost data")
            # Test direct Git URL installation separately, without a user checkout.
            source = "git+" + REPO.as_uri() + "@" + git_ref
            command("update", "--from", source)
            require(
                json.loads(command("version", "--json"))["source"] == "git",
                "direct Git source not recorded",
            )
            process = start()
            stop(process)
            command("uninstall")
            require(
                secret_file.read_bytes() == original and key.read_bytes() == credential,
                "Git uninstall changed secrets",
            )
            print(
                "PASS: cloned Git checkout installer and direct Git-ref installation, restart "
                "and data preservation.",
                flush=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--git-ref", help="also clone/install this local commit/tag (e.g. HEAD)")
    parser.add_argument("--bundle", type=Path, help="also test the extracted release installer")
    args = parser.parse_args()
    smoke(args.wheel, args.git_ref, args.bundle)
