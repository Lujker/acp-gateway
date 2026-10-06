import logging
import os

import pytest
import structlog

from acp_gateway import log


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Each test runs in an empty directory without ACPGW_* variables or leftover log state."""
    for name in list(os.environ):
        if name.startswith(("ACPGW_", "AGENT_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("acp_gateway.paths.config_dir", lambda: tmp_path / "platform-config")
    yield
    log.clear_registered_secrets()
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()
