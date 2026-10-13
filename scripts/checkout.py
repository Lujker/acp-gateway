"""Install, run and update a checkout through the user's isolated uv tool."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32"


class CommandFailed(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def call(argv: list[str | Path], *, capture: bool = False) -> str:
    result = subprocess.run(  # noqa: S603
        [str(arg) for arg in argv],
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise CommandFailed(result.returncode)
    return result.stdout.strip() if capture else ""


def installed_entry(uv: str) -> Path:
    directory = Path(call([uv, "tool", "dir", "--bin"], capture=True))
    if not directory.is_absolute():
        raise ValueError("uv did not return an absolute tool bin directory")
    return directory / ("acpgw.exe" if WINDOWS else "acpgw")


def verify_entry(entry: Path) -> None:
    if not entry.is_file():
        raise ValueError("The uv tool entry point is missing; run scripts/install first")
    info = json.loads(call([entry, "version", "--json"], capture=True))
    if not isinstance(info, dict) or info.get("manager") != "uv":
        raise ValueError("The entry point is not a managed uv tool; refusing to replace it")


def update(
    entry: Path,
    constraints: Path,
    config_dir: Path | None,
    directory: Path,
    *,
    with_services=False,
    service_role=None,
) -> None:
    argv: list[str | Path] = [entry]
    if config_dir is not None:
        argv += ["--config", config_dir / "config.yaml", "--env-file", config_dir / ".env"]
    argv += ["update", "--from", ROOT, "--constraints", constraints]
    if with_services:
        argv += ["--with-services"]
    if service_role is not None:
        argv += ["--service-role", service_role]
    handoff = directory / "handoff.json"
    if WINDOWS:
        argv += ["--handoff-file", handoff]
    call(argv)
    if WINDOWS:
        plan = json.loads(handoff.read_text(encoding="utf-8"))
        if not isinstance(plan, dict) or set(plan) != {"python", "worker", "manifest"}:
            raise ValueError("Invalid update handoff")
        if any(
            not isinstance(value, str) or not Path(value).is_absolute() for value in plan.values()
        ):
            raise ValueError("Update handoff paths must be absolute")
        # Wait outside the tool environment: Windows must release its Python DLLs.
        # Keep the locked constraints alive until the worker verifies the update.
        call([plan["python"], plan["worker"], plan["manifest"]])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        uv = shutil.which("uv")
        if uv is None:
            raise ValueError(
                "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
            )
        entry = installed_entry(uv)
        if argv and argv[0] == "run":
            verify_entry(entry)
            call([entry, *(argv[1:] or ["start"])])
            return 0
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("action", choices=("install", "update"))
        parser.add_argument("--config-dir", type=Path)
        parser.add_argument("--with-services", action="store_true")
        parser.add_argument("--service-role", choices=("gateway", "connector"))
        options, setup_args = parser.parse_known_args(argv)
        if options.service_role and not options.with_services:
            raise ValueError("--service-role requires --with-services")
        if options.action == "update" and setup_args:
            parser.error("update accepts only --config-dir")
        config_dir = options.config_dir.expanduser().absolute() if options.config_dir else None
        present = entry.exists() or entry.is_symlink()
        if present:
            verify_entry(entry)
            if setup_args:
                raise ValueError("Already installed; configure explicitly with scripts/run setup")
        elif options.action == "update":
            raise ValueError("Nothing to update; run scripts/install first")
        with tempfile.TemporaryDirectory(prefix="acpgw-checkout-") as scratch:
            directory = Path(scratch)
            constraints = directory / "requirements.lock.txt"
            call(
                [
                    uv,
                    "export",
                    "--project",
                    ROOT,
                    "--frozen",
                    "--no-dev",
                    "--no-emit-project",
                    "--format",
                    "requirements-txt",
                    "--output-file",
                    constraints,
                ]
            )
            if present:
                update(
                    entry,
                    constraints,
                    config_dir,
                    directory,
                    with_services=options.with_services,
                    service_role=options.service_role,
                )
            else:
                call(
                    [
                        uv,
                        "tool",
                        "install",
                        "--python",
                        "3.12",
                        "--constraints",
                        constraints,
                        ROOT,
                        "--refresh-package",
                        "acp-gateway",
                    ]
                )
                verify_entry(entry)
                setup: list[str | Path] = [entry, "setup"]
                if config_dir is not None:
                    setup += ["--config-dir", config_dir]
                call([*setup, *setup_args])
        print(f"{'Updated' if present else 'Installed'}: {entry}")
        return 0
    except CommandFailed as exc:
        print(f"Checkout command failed (exit {exc.code}).", file=sys.stderr)
        return exc.code
    except json.JSONDecodeError:
        print("Invalid JSON from the installed CLI or update handoff.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
