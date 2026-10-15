# Development

[Documentation](README.md) · [Architecture](architecture.md) · [Roadmap](../road-map.md)

Use a source checkout for development. For deployment, use the
[uv/PyPI installation guide](setup/self-install-pypi.ru.md).
The source version is `0.2.0`. Changes on a working branch are separate from
published artifacts; see the [current Telegram journal](items/telegram-recovery.ru.md).
Development does not rebuild or publish an installed version.

## Checkout and environment

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git:

```sh
git clone https://github.com/Lujker/acp-gateway.git
cd acp-gateway
# Select the reviewed tag, branch or commit you intend to work on.
uv sync --frozen
git config core.hooksPath .githooks
```

Create a working branch before changing code. A checkout follows the selected
Git revision; it is not an automatic upgrade of an installed `uv tool`.
Linux/WSL uses its own `.venv`. Native Windows verification needs a separate
Windows Python environment; do not reuse the Linux virtual environment.

## Checks

```sh
uv run --frozen pytest
uv run --frozen ruff check
uv run --frozen ruff format --check
```

The pre-commit hook scans staged files for secrets and runs Ruff.
Integration tests use recorded ACP fixtures and fake agents/Bot API; passing
them does not imply live provider, Telegram or reboot acceptance.
Recorded Goose traffic is in `tests/fixtures/acp/`. Sanitize new recordings
with `scripts/sanitize_fixture.py` before committing.

## Explicit checkout configuration

Installed defaults use platform configuration directories. To opt into local
checkout files on Linux/WSL:

```sh
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
export ACPGW_CONFIG="$PWD/config.yaml"
export ACPGW_ENV_FILE="$PWD/.env"
uv run acpgw config check
```

Edit profiles and secret references before starting anything. Tokens belong
in the private env file, not YAML, shell history or Git. `acpgw paths` shows
resolved directories. The daemon and CLI must use the same selected config.

Direct Goose/TLS setup and the opt-in live `scripts/spike_acp.py` scenarios are
covered by [Work Goose](setup/work-goose.md) and the [gateway guide](setup/gateway.md).
Live prompt/permission/cancel scenarios contact the configured agent and may
execute commands; they are not part of an ordinary documentation check.

## Build

```sh
uv build
# Optional standalone executable, built on the target platform:
uv run --frozen --group build python scripts/build_binary.py
```

Artifacts go to `dist/`; configuration, secrets, pins and SQLite data remain
outside them. See [distribution](setup/distribution.md),
[binary compatibility](setup/binary.md) and [delivery evidence](items/p48-delivery-plan.ru.md).
Build/release workflows are manually dispatched. Building locally does not
publish a GitHub or PyPI release.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/acp_gateway/` | Agents, core, storage, connectors, channels, API and CLI |
| `tests/` | Unit/integration checks, fakes and sanitized traffic fixtures |
| `scripts/` | Build, fixture and verification utilities |
| `docs/setup/` | Installation and operating guides |
| `docs/items/` | Item designs, findings and implementation evidence |
| `docs/archive/` | Historical snapshots |
| `road-map.md`, `road-notes.md` | Russian plan/queue and decision journal |

Record substantial work in the relevant item journal and update roadmap
status by evidence. Keep published-version evidence distinct from source
checks and live acceptance. Do not put test transcripts into the root README.
