"""Create private Docker runtime files without overwriting an existing deployment."""

import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path


def write_new(path: Path, text: str) -> None:
    try:
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8"
        ) as file:
            file.write(text)
    except FileExistsError:
        print(f"Kept existing {path.name}")


def uv_executable():
    executable = shutil.which("uv")
    if executable is None:
        raise ValueError("Install uv on the Docker host for independent update delivery")
    return executable


def prepare_updater(root, compose_files):
    """Prepare host autostart as part of deployment, without mounting a Docker socket."""
    release = root.parents[1] / "release.json"
    uv = uv_executable()
    if release.is_file():
        version = json.loads(release.read_text())["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError("Docker host bootstrap requires a stable release bundle")
        command = [
            uv,
            "tool",
            "run",
            "--python",
            "3.12",
            "--from",
            "acp-gateway==" + version,
            "python",
        ]
    elif (root.parents[1] / "pyproject.toml").is_file():
        command = [uv, "run", "--project", str(root.parents[1]), "--frozen", "python"]
    else:
        raise ValueError("Host bootstrap requires the complete release bundle or source deployment")
    command += ["-m", "acp_gateway.update_bootstrap", "docker-host", str(root)]
    for path in compose_files:
        command += ["--compose-file", str(path)]
    result = subprocess.run(command, timeout=300, check=False, capture_output=True, text=True)  # noqa: S603 — fixed package/checkout
    if result.returncode not in {0, 2}:
        raise ValueError(
            "Docker host updater preparation failed; gateway deployment was not changed"
        )
    if not isinstance(result.stdout, str) or len(result.stdout) > 65536:
        raise ValueError("Invalid Docker host bootstrap response")
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ValueError("Invalid Docker host bootstrap response") from exc
    if not isinstance(value, dict) or type(value.get("ready")) is not bool:
        raise ValueError("Invalid Docker host bootstrap readiness")
    if value["ready"] and result.returncode == 0:
        state = "ready"
    elif value.get("reason") == "disabled" and not value["ready"] and result.returncode == 0:
        state = "disabled"
    elif not value["ready"] and result.returncode == 2:
        state = "prepared"
    else:
        raise ValueError("Inconsistent Docker host bootstrap readiness")
    print("Host updater: " + state + ". " + str(value.get("reason", "")))
    return state


def main(argv=None, *, root=None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Compose overlay selected for this deployment",
    )
    parser.add_argument(
        "--no-updater", action="store_true", help="Explicitly opt out of host updater preparation"
    )
    args = parser.parse_args(argv)
    root = root or Path(__file__).resolve().parent
    for name in ("runtime", "runtime/tls", "state", "state/enrollment"):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    write_new(root / ".env", f"ACPGW_UID={os.getuid()}\nACPGW_GID={os.getgid()}\n")
    write_new(root / "runtime/gateway.yaml", (root / "gateway.example.yaml").read_text())
    write_new(root / "runtime/nginx.conf", (root / "nginx.conf.example").read_text())
    write_new(
        root / "runtime/gateway.env",
        f"ACPGW_API_TOKEN={secrets.token_urlsafe(32)}\n"
        f"ACPGW_MCP_TOKEN={secrets.token_urlsafe(32)}\n"
        "TELEGRAM_BOT_TOKEN=\n",
    )
    if not args.no_updater:
        files = [root / "compose.yaml", *(root / item for item in args.compose_file)]
        prepare_updater(root, files)
    print(
        "Prepared Docker files. Edit runtime/gateway.yaml and runtime/gateway.env before starting."
    )


if __name__ == "__main__":
    main()
