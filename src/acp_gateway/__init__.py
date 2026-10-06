"""ACP Gateway: routes messages from external channels to ACP agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("acp-gateway")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"

APP_NAME = "acp-gateway"
