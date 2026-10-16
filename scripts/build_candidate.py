"""Build an offline development snapshot without changing the checkout or publishing."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from packaging.version import Version

REPO = Path(__file__).resolve().parents[1]


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(destination, version):
    parsed = Version(version)
    if not parsed.is_devrelease or str(parsed) != version:
        raise ValueError("Use a canonical development version, e.g. 0.2.1.dev20261016")
    uv = shutil.which("uv")
    git = shutil.which("git")
    if uv is None or git is None:
        raise ValueError("uv and git are required")
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Candidate output must be empty; existing artifacts are never overwritten")
    destination.mkdir(parents=True, exist_ok=True)
    source_files = [REPO / "pyproject.toml", *sorted((REPO / "src").rglob("*.py"))]
    source_files += [Path(__file__).resolve()]
    source_hashes = {str(p.relative_to(REPO)): checksum(p) for p in source_files}
    with tempfile.TemporaryDirectory(prefix="acpgw-candidate-") as temporary:
        root = Path(temporary)
        shutil.copytree(REPO / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("README.md", "LICENSE"):
            shutil.copy(REPO / name, root / name)
        project = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        project = re.sub(
            r'^version = "[^"]+"$', f'version = "{version}"', project, count=1, flags=re.M
        )
        (root / "pyproject.toml").write_text(project, encoding="utf-8")
        subprocess.run(  # noqa: S603 — fixed CLI, local snapshot, no network or publishing
            [
                uv,
                "build",
                "--offline",
                "--no-python-downloads",
                "--out-dir",
                str(destination),
                str(root),
            ],
            check=True,
        )
    for path, digest in source_hashes.items():
        if checksum(REPO / path) != digest:
            raise ValueError(
                "Source changed during candidate build; rebuild from a stable snapshot"
            )
    guide = destination / "remote-updates.md"
    shutil.copy(REPO / "docs/setup/remote-updates.md", guide)
    artifacts = [*sorted(destination.glob("*.whl")), *sorted(destination.glob("*.tar.gz")), guide]
    if len(artifacts) != 3:
        raise ValueError("Expected one wheel, one source distribution and the companion guide")
    manifest = {
        "version": version,
        "kind": "local-development-snapshot",
        "published": False,
        "source_head": subprocess.check_output(  # noqa: S603 — fixed read-only git command
            [git, "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        "working_tree_snapshot": True,
        "source_sha256": source_hashes,
        "artifacts": {p.name: checksum(p) for p in artifacts},
    }
    path = destination / "candidate.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    artifacts.append(path)
    (destination / "SHA256SUMS").write_text(
        "".join(f"{checksum(p)}  {p.name}\n" for p in artifacts), encoding="utf-8"
    )
    print(f"Prepared local development candidate: {destination}. Nothing published or installed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    build(args.output, args.version)
