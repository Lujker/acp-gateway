"""Explicit local grant using the existing guarded service refresh transaction."""

from types import SimpleNamespace

import yaml

from acp_gateway.cli.connection_service import apply_connection_settings
from acp_gateway.config import Settings
from acp_gateway.connectors.launch import ConnectorLaunch


def allow(config):
    path = config.config_path
    if path is None:
        raise ValueError("A saved connector configuration is required; run acpgw connect")
    launch = ConnectorLaunch.from_saved(config)
    original = path.read_bytes()
    raw = yaml.safe_load(original)
    raw.setdefault("connector", {})["remote_profiles"] = True
    Settings.model_validate(raw)
    content = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True).encode()
    # save_files acquires the shared writer lock and rechecks the original snapshot.
    _, managed = apply_connection_settings(
        config,
        launch,
        {path: content},
        {path: original},
        path.parent,
        # Invoking this explicit local grant authorizes the existing service refresh.
        SimpleNamespace(yes=lambda *args: True),
    )
    print("Remote profile management enabled.")
    print(
        "Connector service refreshed." if managed else "Restart your foreground connector to apply."
    )
    return 0
