"""Build a Linux executable and a versioned archive from the locked environment."""

import hashlib
import json
import os
import platform
import subprocess
import sys
import tarfile
from pathlib import Path

from acp_gateway import __version__

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if sys.platform not in {"linux", "darwin", "win32"}:
        raise SystemExit("Supported build systems: Linux, macOS, Windows.")
    machine = {"AMD64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    if machine not in {"x86_64", "aarch64"}:
        raise SystemExit(f"Unsupported build architecture: {machine}")
    system = {"linux": "linux", "darwin": "macos", "win32": "windows"}[sys.platform]
    executable = "acpgw.exe" if sys.platform == "win32" else "acpgw"
    output = ROOT / "dist" / f"acpgw-{system}-{machine}"
    work = ROOT / "build" / "binary"
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "acpgw",
        "--distpath",
        str(output),
        "--workpath",
        str(work),
        "--specpath",
        str(work),
        "--copy-metadata",
        "acp-gateway",
        "--copy-metadata",
        "agent-client-protocol",
        "--copy-metadata",
        "mcp",
        "--collect-data",
        "acp_gateway",
        "--collect-submodules",
        "acp_gateway",
        "--collect-submodules",
        "uvicorn",
        "--collect-submodules",
        "websockets",
        "--collect-submodules",
        "anyio",
        str(ROOT / "scripts" / "binary_entry.py"),
    ]
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / "cache"))
    subprocess.run(command, cwd=ROOT, env=env, check=True)  # noqa: S603
    archive = ROOT / "dist" / f"acpgw-{__version__}-{system}-{machine}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path, name in (
            (output / executable, executable),
            (ROOT / "LICENSE", "LICENSE"),
            (ROOT / "docs/setup/binary.md", "INSTALL.md"),
        ):
            bundle.add(path, arcname=name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="utf-8"
    )
    archive.with_suffix(archive.suffix + ".build.json").write_text(
        json.dumps(
            {
                "version": __version__,
                "architecture": machine,
                "system": system,
                "python": platform.python_version(),
                "libc": platform.libc_ver(),
                "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Executable: {output / executable}")
    print(f"Archive: {archive}")


if __name__ == "__main__":
    main()
