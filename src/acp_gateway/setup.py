"""Create a private initial configuration without replacing existing files."""

import os
import secrets
from pathlib import Path

from acp_gateway import paths

CONFIG = """# ACP Gateway: add your ACP agent profiles here before sending prompts.
# See https://github.com/Lujker/acp-gateway/blob/main/docs/setup/work-goose.md
gateway:
  host: 127.0.0.1
  port: 8765
  api_token_env: ACPGW_API_TOKEN
  mcp_token_env: ACPGW_MCP_TOKEN

agents: []
# Example (replace the address and working directory):
#  - alias: work
#    kind: goose
#    url: wss://work-laptop.lan:3284/acp
#    secret_env: AGENT_WORK_SECRET
#    tls_fingerprint: ""  # trust on first use; preferably set the known fingerprint
#    default_cwd: /home/user/work
#    session_mode: smart_approve

policy:
  approver_channels: [cli]

logging:
  level: INFO
  format: auto
"""


def _create(path: Path, contents: str) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(contents)
    return True


def setup(directory: Path | None = None) -> int:
    directory = (directory or paths.config_dir()).expanduser().absolute()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = directory / "config.yaml"
    env = directory / ".env"
    created_config = _create(config, CONFIG)
    # Tokens are independent, never printed and never refreshed by repeated setup.
    created_env = _create(
        env,
        "# Secrets only. Keep this file private.\n"
        f"ACPGW_API_TOKEN={secrets.token_urlsafe(32)}\n"
        f"ACPGW_MCP_TOKEN={secrets.token_urlsafe(32)}\n"
        "AGENT_WORK_SECRET=\n",
    )
    for path, created in ((config, created_config), (env, created_env)):
        print(f"{'Created' if created else 'Kept existing'}: {path}")
    print("Add an agent profile to config.yaml and its secret to .env.")
    print(f'Check: acpgw --config "{config}" --env-file "{env}" config check')
    print(f'Run: acpgw --config "{config}" --env-file "{env}" serve')
    print(f'Autostart: acpgw --config "{config}" --env-file "{env}" service install')
    if directory != paths.config_dir().absolute():
        print("Use --config and --env-file for this custom directory in subsequent commands.")
    return 0
