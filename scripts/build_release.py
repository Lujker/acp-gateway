"""Build a versioned Python/deployment bundle locally; never publish anything."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DEPLOY_FILES = (
    "compose.yaml",
    "compose.direct.yaml",
    "compose.nginx.yaml",
    "gateway.example.yaml",
    "computer.example.yaml",
    "nginx.conf.example",
    "nginx-location.conf.example",
    "prepare.py",
)


def build(destination: Path, image: str, *, include_image=False) -> Path:
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    git = shutil.which("git")
    if uv is None or git is None:
        raise ValueError("uv and git are required to build a release")
    with tempfile.TemporaryDirectory(prefix="acpgw-release-") as temporary:
        root = Path(temporary) / f"acpgw-{version}"
        root.mkdir()
        subprocess.run(  # noqa: S603 — fixed command, explicit argv
            [uv, "build", "--out-dir", str(root)], cwd=REPO, check=True
        )
        subprocess.run(  # noqa: S603
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(root / "requirements.lock.txt"),
            ],
            cwd=REPO,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        shutil.copy(REPO / "scripts/install_release.sh", root / "install.sh")
        shutil.copy(REPO / "scripts/install_release.ps1", root / "install.ps1")
        for name in ("README.md", "LICENSE"):
            shutil.copy(REPO / name, root / name)
        docs = root / "docs/setup"
        docs.mkdir(parents=True)
        # Include all public docs so relative links continue to work offline.
        shutil.copytree(REPO / "docs", root / "docs", dirs_exist_ok=True)
        deployment = root / "deploy/docker"
        deployment.mkdir(parents=True)
        for name in DEPLOY_FILES:
            shutil.copy(REPO / "deploy/docker" / name, deployment / name)
        compose = yaml.safe_load((deployment / "compose.yaml").read_text())
        compose["services"]["gateway"].pop("build")
        compose["services"]["gateway"]["image"] = "${ACPGW_IMAGE:-" + image + "}"
        (deployment / "compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
        if include_image:
            docker = shutil.which("docker")
            if docker is None:
                raise ValueError("Docker is required for --include-image")
            subprocess.run(  # noqa: S603 — export an existing local image; never push
                [docker, "image", "save", "--output", str(root / "gateway-image.tar"), image],
                check=True,
                timeout=120,
            )
        commit = subprocess.check_output(  # noqa: S603
            [git, "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip()
        dirty = bool(
            subprocess.check_output(  # noqa: S603
                [git, "status", "--porcelain"], cwd=REPO, text=True
            ).strip()
        )
        (root / "release.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "commit": commit,
                    "dirty": dirty,
                    "image": image,
                    "bundled_image": include_image,
                },
                indent=2,
            )
            + "\n"
        )
        for file in sorted(root.glob("*.whl")) + sorted(root.glob("*.tar.gz")):
            shutil.copy(file, destination / file.name)
        archive = destination / f"acpgw-{version}-bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(root, arcname=root.name)
        files = sorted(destination.glob("*.whl")) + sorted(destination.glob("*.tar.gz"))
        (destination / "SHA256SUMS").write_text(
            "".join(f"{checksum(file)}  {file.name}\n" for file in files)
        )
    print(f"Prepared {archive}; no packages, images or releases were published.")
    return archive


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / "dist/release")
    parser.add_argument("--image", help="versioned image reference included in deployment bundle")
    parser.add_argument(
        "--include-image", action="store_true", help="bundle a local image for docker load"
    )
    args = parser.parse_args()
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    build(
        args.output,
        args.image or f"ghcr.io/lujker/acp-gateway:{version}",
        include_image=args.include_image,
    )
