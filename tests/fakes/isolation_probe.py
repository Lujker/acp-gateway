"""Pinned model-free canaries; no manager invocation, shell, updates or accounts.

Startup mode inspects a ctypes process export without calling it. Collection
modes copy only the reviewed gate/runner/conftest and import harmless app logging.
"""

import json
import sys
import time
from pathlib import Path

STARTUP_MODES = {
    "collection-direct",
    "collection-nodeid",
    "collection-recursive",
    "startup-prebound",
    "ancestor-conftest-root-direct",
    "ancestor-conftest-root-nodeid",
    "ancestor-conftest-root-recursive",
    "ancestor-conftest-unit-direct",
    "ancestor-conftest-unit-nodeid",
    "ancestor-conftest-unit-recursive",
    "package-init-root-direct",
    "package-init-root-nodeid",
    "package-init-root-recursive",
    "package-init-unit-direct",
    "package-init-unit-nodeid",
    "package-init-unit-recursive",
    "config-plugin-root-direct",
    "config-plugin-root-nodeid",
    "config-plugin-root-recursive",
    "config-plugin-unit-direct",
    "config-plugin-unit-nodeid",
    "config-plugin-unit-recursive",
    "conftest-plugin-root-direct",
    "conftest-plugin-root-nodeid",
    "conftest-plugin-root-recursive",
    "conftest-plugin-unit-direct",
    "conftest-plugin-unit-nodeid",
    "conftest-plugin-unit-recursive",
}


def startup_probe(mode, root):
    """Fixed harmless startup/collection harness; no arbitrary script API."""
    import shutil

    repository = Path(__file__).resolve().parents[2]
    if mode == "startup-prebound":
        import ctypes

        # Resolve exports only, NEVER invoke process creation or termination.
        dangerous = (
            ctypes.WinDLL("kernel32").CreateProcessW
            if sys.platform == "win32"
            else ctypes.CDLL(None).system
        )
        assert callable(dangerous)
        sys.path.insert(0, str(repository / "tests"))
        import external_effect_guard as guard

        try:
            guard.install()
        except guard.ExternalEffectDenied:
            print(json.dumps({"prebound_denied": True}))
        else:
            print(json.dumps({"prebound_denied": False}))
        return
    tree = root / "collection-canary"
    tests = tree / "tests"
    tests.mkdir(parents=True)  # Existing paths fail rather than reuse foreign state.
    for name in ("external_effect_guard.py", "conftest.py", "strict_isolation_runner.py"):
        shutil.copyfile(repository / "tests" / name, tests / name)
    # Keep canonical support bytes unchanged; only logging is needed in collection.
    source = tree / "src/acp_gateway"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "log.py").write_text("def clear_registered_secrets(): pass\n", encoding="utf-8")
    marker = tree / "IMPORTED"
    (tests / "test_external_effect_isolation.py").write_text(
        "import external_effect_guard as guard\n"
        "assert guard.INSTALLED and guard.CONTROLLED_STARTUP\n"
        "def test_harmless():\n    pass\n",
        encoding="utf-8",
    )
    (tests / "test_unreviewed.py").write_text(
        "from pathlib import Path\nPath("
        + repr(str(marker))
        + ").write_text('imported')\ndef test_harmless():\n    pass\n",
        encoding="utf-8",
    )
    nested = tests / "unreviewed"
    nested.mkdir()
    (nested / "conftest.py").write_text(
        "from pathlib import Path\nPath("
        + repr(str(marker))
        + ").write_text('nested conftest imported')\n",
        encoding="utf-8",
    )
    (tree / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n', encoding="utf-8"
    )
    target = "tests" if mode == "collection-recursive" else "tests/test_unreviewed.py"
    if mode == "collection-nodeid":
        target += "::test_harmless"
    if mode not in {"collection-direct", "collection-nodeid", "collection-recursive"}:
        kind, location, selection = mode.rsplit("-", 2)
        unit = tests / "unit"
        unit.mkdir()
        (unit / "test_windows_acl.py").write_text("def test_harmless(): pass\n", encoding="utf-8")
        parent = tests if location == "root" else unit
        payload = (
            "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('imported')\n"
        )
        if kind == "ancestor-conftest":
            support = parent / "conftest.py"
            prior = support.read_text(encoding="utf-8") if support.exists() else ""
            support.write_text(prior + "\n" + payload, encoding="utf-8")
        elif kind == "package-init":
            (tests / "__init__.py").write_text(
                payload if location == "root" else "", encoding="utf-8"
            )
            if location == "unit":
                (unit / "__init__.py").write_text(payload, encoding="utf-8")
        else:
            (tests / "marker_plugin.py").write_text(payload, encoding="utf-8")
            if kind == "config-plugin":
                (tree / "pyproject.toml").write_text(
                    '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n'
                    'addopts = "-p marker_plugin"\n',
                    encoding="utf-8",
                )
            else:
                support = parent / "conftest.py"
                prior = support.read_text(encoding="utf-8") if support.exists() else ""
                support.write_text(
                    prior + '\npytest_plugins = ["marker_plugin"]\n', encoding="utf-8"
                )
        target = (
            "tests/test_external_effect_isolation.py"
            if location == "root"
            else "tests/unit/test_windows_acl.py"
        )
        if selection == "nodeid":
            target += "::test_harmless"
        elif selection == "recursive":
            target = "tests"
    import os
    import runpy

    os.chdir(tree)
    sys.argv = [str(tests / "strict_isolation_runner.py"), "--collect-only", "-q", target]
    observed = []

    def startup_order(event, args):
        if event == "import" and args[0].split(".", 1)[0] in {
            "pytest",
            "_pytest",
            "pytest_asyncio",
        }:
            guard = sys.modules.get("external_effect_guard")
            assert guard is not None and guard.INSTALLED, "pytest imported before guard"
            observed.append(args[0])

    sys.addaudithook(startup_order)
    try:
        runpy.run_path(str(tests / "strict_isolation_runner.py"), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    else:
        code = 0
    print(
        json.dumps(
            {
                "exit_code": code,
                "marker_created": marker.exists(),
                "pytest_imports_guarded": bool(observed),
            }
        )
    )


if len(sys.argv) != 3 or sys.argv[1] not in {"scheduler", "stall", *STARTUP_MODES}:
    raise SystemExit(2)
root = Path(sys.argv[2])
if not root.is_absolute() or not root.is_dir() or Path.cwd() != root:
    raise SystemExit(3)
if sys.argv[1] in STARTUP_MODES:
    startup_probe(sys.argv[1], root)
    raise SystemExit(0)
if sys.argv[1] == "stall":
    # Deterministic stall for bounded cleanup tests; no external effect.
    time.sleep(15)
    print("stalled")
    raise SystemExit(0)
request = json.loads(sys.stdin.read(65536))
if request.get("action") not in {"status", "snapshot", "start", "stop", "install", "uninstall"}:
    raise SystemExit(4)
# Synthetic manager result, never a claim about the user's scheduler.
print(json.dumps({"installed": False}))
