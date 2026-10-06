"""Platform-specific locations for config and runtime data.

Linux/WSL: ~/.config/acp-gateway, ~/.local/share/acp-gateway, ~/.local/state/acp-gateway
Windows:   %LOCALAPPDATA%\\acp-gateway\\...
macOS:     ~/Library/Application Support/acp-gateway, ~/Library/Logs/acp-gateway
"""

from pathlib import Path

from platformdirs import PlatformDirs

from acp_gateway import APP_NAME

_dirs = PlatformDirs(appname=APP_NAME, appauthor=False)


def config_dir() -> Path:
    return Path(_dirs.user_config_dir)


def data_dir() -> Path:
    return Path(_dirs.user_data_dir)


def log_dir() -> Path:
    return Path(_dirs.user_log_dir)
