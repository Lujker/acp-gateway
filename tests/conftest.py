import contextlib
import logging
import os
from pathlib import Path

import pytest
import structlog

from acp_gateway import log

# Strict external-effect isolation is opt-in for an explicitly audited module
# set. The ordinary suite keeps its normal subprocess behaviour.
_STRICT = os.environ.get("ACPGW_STRICT_TEST_ISOLATION") == "1"

if _STRICT:
    import external_effect_guard as _guard

    if not _guard.CONTROLLED_STARTUP:
        raise _guard.ExternalEffectDenied(
            "strict isolation requires python -I -S tests/strict_isolation_runner.py"
        )
    _guard.install()
else:
    _guard = None


def pytest_configure(config):
    if _guard is not None:
        allowed = _strict_paths()
        for target in config.args:
            path = Path(str(target).split("::", 1)[0]).resolve()
            if path not in allowed and not (
                path.is_dir()
                and any(item.is_relative_to(path) for item in allowed)
                and path.is_relative_to(_guard.REPOSITORY)
            ):
                raise pytest.UsageError("strict isolation refuses unaudited target: " + str(target))


def pytest_ignore_collect(collection_path, config):
    if _guard is None:
        return None
    path = Path(collection_path).resolve()
    allowed = _strict_paths()
    if path.is_dir():
        return not any(item.is_relative_to(path) for item in allowed)
    return path not in allowed


def pytest_collection_modifyitems(config, items):
    if _guard is None:
        return
    allowed = _strict_paths()
    foreign = [item.nodeid for item in items if Path(item.path).resolve() not in allowed]
    if foreign:
        listed = "\n".join(foreign[:10])
        raise pytest.UsageError("strict isolation refuses unaudited modules:\n" + listed)


def _strict_paths():
    try:
        return _guard.selected_paths()
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Each test runs in an empty directory without ACPGW_* variables or leftover log state."""
    for name in list(os.environ):
        if name.startswith(("ACPGW_", "AGENT_")):
            monkeypatch.delenv(name)
    for name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
        "TMP",
        "TEMP",
    ):
        directory = tmp_path / name.lower()
        directory.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(directory))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("acp_gateway.paths.config_dir", lambda: tmp_path / "platform-config")
    scope = _guard.fixture_root(tmp_path) if _guard is not None else contextlib.nullcontext()
    with scope:
        yield
    log.clear_registered_secrets()
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


@pytest.fixture
def audited_external_effects(_isolated, tmp_path):
    """Explicit fixed fake-child/read-only fixture ACL opt-in; never arbitrary argv."""
    if _guard is None:
        pytest.skip("audited external effects need the strict isolation runner")
    return _guard.ScopedEffects(tmp_path)
