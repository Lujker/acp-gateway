"""Controlled CPython 3.12 accident-guard runner (not a Python sandbox).

Run with the locked venv interpreter: python -I -S -B tests/strict_isolation_runner.py
Only reviewed test paths, optional node IDs, and bounded reporting options are
accepted. No site startup, .pth files, environment plugins or third-party plugin
autoload execute. The guard installs before pytest and the fixed asyncio plugin.
"""

import os
import sys
from pathlib import Path

# Canonical reviewed support file: no pytest_plugins declarations. Any change
# requires review and a new pin, rather than executing dynamic plugin declarations
# to discover what they import. No package initializers are currently reviewed.
_REVIEWED_CONFTEST_SHA256 = "4e32650ec2b957337db080c44413aff58943f59808cf2dfc39cbc44f11f5775c"


def validate_support_imports(tests, allowed):
    """Validate every selectable ancestor BEFORE pytest can import support code."""
    import hashlib

    reviewed = tests / "conftest.py"
    if (
        reviewed.resolve() != reviewed
        or not reviewed.is_file()
        or hashlib.sha256(reviewed.read_bytes()).hexdigest() != _REVIEWED_CONFTEST_SHA256
    ):
        raise SystemExit("strict isolation refuses changed reviewed conftest (including plugins)")
    for item in allowed:
        # pytest's package resolution may walk above confcutdir; inspect the
        # complete package chain, not only the conftest discovery boundary.
        for parent in item.parents:
            if (parent / "__init__.py").exists():
                raise SystemExit(
                    "strict isolation refuses unaudited package initializer: " + str(parent)
                )
            if parent == tests or parent.is_relative_to(tests):
                support = parent / "conftest.py"
                if support != reviewed and support.exists():
                    raise SystemExit("strict isolation refuses unaudited conftest: " + str(support))


def main():
    if not (sys.flags.isolated and sys.flags.no_site):
        raise SystemExit("strict runner requires interpreter flags -I -S")
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        raise SystemExit("strict runner supports audited CPython 3.12 only")
    tests = Path(__file__).resolve().parent
    repository = tests.parent
    sys.path.insert(0, str(tests))
    import external_effect_guard as guard

    guard.install()
    guard.CONTROLLED_STARTUP = True
    os.environ["ACPGW_STRICT_TEST_ISOLATION"] = "1"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.environ.pop("PYTEST_PLUGINS", None)
    # -S on Python 3.12 leaves sys.prefix at the base interpreter. Use the
    # invoked venv executable, NOT its resolved symlink, to locate dependencies.
    environment = Path(sys.executable).parent.parent
    if not (environment / "pyvenv.cfg").is_file():
        raise SystemExit("strict runner requires an explicit audited virtualenv")
    site = (
        environment / "Lib/site-packages"
        if os.name == "nt"
        else environment
        / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    if not site.is_dir():
        raise SystemExit("virtualenv site-packages missing")
    sys.path.extend([str(repository / "src"), str(site)])
    if os.name == "nt":
        # Fixed pywin32 search directories, without executing pywin32.pth.
        sys.path.extend(str(site / part) for part in ("win32", "win32/lib", "Pythonwin"))
        # Equivalent to the reviewed pywin32_bootstrap search-path operation;
        # retain the handle for the whole run, without executing a .pth file.
        _dll_directory = os.add_dll_directory(str(site / "pywin32_system32"))
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="*")
    parser.add_argument("-q", action="store_true")
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--tb", choices=("short", "long", "no"), default="short")
    parser.add_argument("--basetemp")
    options = parser.parse_args()
    try:
        allowed = guard.selected_paths()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    targets = options.targets or sorted(str(path) for path in allowed)
    # Validate BEFORE importing pytest or plugins, including node-id selections.
    for target in targets:
        path = Path(target.split("::", 1)[0]).resolve()
        if path not in allowed and not (
            path.is_dir()
            and path.is_relative_to(repository)
            and any(item.is_relative_to(path) for item in allowed)
        ):
            raise SystemExit("strict isolation refuses unaudited target: " + target)
    validate_support_imports(tests, allowed)
    import pytest

    args = [
        "-c",
        str(repository / "pyproject.toml"),
        # pytest reads override_ini before expanding ini addopts. Never let
        # configuration inject -p plugins (or other collection/startup flags).
        "-o",
        "addopts=-ra --strict-markers",
        "--confcutdir",
        str(tests),
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "no:cacheprovider",
        "--tb=" + options.tb,
    ]
    if options.q:
        args.append("-q")
    if options.collect_only:
        args.append("--collect-only")
    if options.basetemp:
        args.extend(["--basetemp", options.basetemp])
    return pytest.main([*args, *targets])


if __name__ == "__main__":
    raise SystemExit(main())
