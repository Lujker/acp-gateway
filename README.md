# ACP Gateway

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![Status: early development](https://img.shields.io/badge/status-early%20development-orange.svg)
[![ACP](https://img.shields.io/badge/protocol-Agent%20Client%20Protocol-6f42c1.svg)](https://agentclientprotocol.com)

**ACP Gateway** is a local gateway that lets chat channels and other agents —
Hermes, Telegram, later XMPP, e-mail and a web UI — talk to a remote
[Agent Client Protocol](https://agentclientprotocol.com) agent such as
[goose](https://github.com/aaif-goose/goose). It delivers messages to the
agent and returns answers and tool-approval requests to the channels, with a
human in the loop for every risky action.

The first target is **Work Goose**: `goose serve` on a work laptop. Work MCP
servers, credentials, files and the model stay on that machine; the gateway
only knows the agent's address, a shared secret and the fingerprint of its TLS
certificate.

```mermaid
flowchart LR
    subgraph home["Home host (WSL)"]
        hermes["Hermes"] -- MCP --> gw
        tg["Telegram"] --> gw
        cli["CLI / Web UI"] -- HTTP --> gw
        gw["ACP Gateway<br/>sessions · approvals · policy · audit"]
    end
    subgraph work["Work laptop (WSL)"]
        goose["goose serve<br/>MCP · files · model"]
    end
    gw -- "ACP over WebSocket<br/>TLS pinning + X-Secret-Key" --> goose
    desktop["Goose Desktop"] -. direct .-> goose
```

## Status

Early development. The foundation is done: the connection to a real goose
1.53.0 over the LAN is verified, along with sessions, approvals, cancellation
and session restore. The daemon, local HTTP/SSE API and human CLI channel are
implemented and verified against Work Goose. The Hermes MCP facade is implemented
and checked with Hermes' installed MCP module against the recorded Goose mock.
Linux/WSL service lifecycle and Telegram commands/approval buttons are implemented;
actual reboot and the live human Telegram acceptance remain pending. Connector
development includes local computer enrollment and a pinned WSS registration
channel; task relay is next. The plan
and the work queue are in [`road-map.md`](road-map.md) (in Russian).

| Area | State |
|---|---|
| Scaffold, config, secret redaction, pre-commit hook | ✅ |
| ACP over WebSocket with TLS pinning, verified on goose 1.53.0 | ✅ |
| Agent client (`acp_gateway.agents`) and recorded-traffic mock agent | ✅ `P1.1`, `P1.2` |
| Core: sessions in SQLite, jobs, event bus, channel contract | ✅ `P1.3` |
| Approvals: human routing, deadlines, audit; core policy limits | ✅ `P1.4` |
| Daemon, local API, approvals from the CLI | ✅ `P1.5` |
| Hermes MCP facade; actions approved through the CLI | ✅ `P2.1`, `P2.2` (Hermes MCP module + mock agent) |
| Checkout installer, setup, Linux/WSL service CLI, standalone Linux binary recipe | Binary, real user-service/crash recovery and Windows task lifecycle verified; actual reboot pending |
| Telegram | Commands, private user allowlist and audited approval buttons implemented; live acceptance pending |
| Runtime diagnostics | Component health/probes, private rotated JSON logs and Telegram throttling/recovery implemented |
| [VPS connector](docs/items/connector.md) | Enrollment and pinned WSS registration/heartbeats implemented; task relay next |
| Snikket/XMPP, e-mail, web UI | 🗓 `P4` |

## Principles

- **The agent executes, the gateway relays.** The gateway makes no decisions
  for the agent and stores no work MCP credentials.
- **A human approves.** Agent actions are approved in a channel with a live
  human (CLI, Telegram). LLM channels, Hermes included, never get an "allow"
  button. With no approver connected, the action is rejected.
- **Secure by default.** The local API listens on loopback only. Unencrypted
  transport to an agent is allowed only on loopback. The secret is sent only
  after the pinned certificate is verified. ACP client capabilities (`fs`,
  `terminal`) are disabled. Secrets are masked in logs and kept out of git.
- **Channels are thin adapters.** A new channel implements a shared contract
  and does not touch the core.

More in [`docs/architecture.md`](docs/architecture.md).

## Quick start

You need [uv](https://docs.astral.sh/uv/) and git; uv installs Python 3.12
itself. Linux/WSL is the primary platform; Windows and macOS are planned.

```bash
git clone git@github.com:Lujker/acp-gateway.git
cd acp-gateway
uv sync
```

For an installed `acpgw` command outside the checkout, run
`sh scripts/install.sh`, then configure the files created by `acpgw setup`.
The installer keeps existing configuration and tokens. Installation and
service commands: [`docs/setup/service.md`](docs/setup/service.md).

To build a standalone Linux executable with no Python required on the runtime
host, use `uv run --frozen --group build python scripts/build_binary.py`.
Build artifacts, checksums, compatibility limits and installation:
[`docs/setup/binary.md`](docs/setup/binary.md).
Build locally and attach archives/checksums to a manually created GitHub
release. The optional GitHub Actions build runs only on explicit manual
dispatch; pushes, PRs, tags and releases do not consume build minutes.

Telegram setup and human approvals: [`docs/setup/telegram.md`](docs/setup/telegram.md).
Health probes and rotating logs: [`docs/setup/observability.md`](docs/setup/observability.md).

### 1. Prepare the agent

On the agent machine, run `goose serve` with TLS and a secret and open the
port to the LAN. Step by step, including the Hyper-V firewall rule for WSL:
[`docs/setup/work-goose.md`](docs/setup/work-goose.md).

```bash
GOOSE_SERVER__SECRET_KEY='<long random secret>' \
goose serve --host 0.0.0.0 --port 3284 --tls
# note the GOOSED_CERT_FINGERPRINT=... line
```

### 2. Configure the gateway

```bash
cp config.example.yaml config.yaml      # agents, policy, logging
cp .env.example .env && chmod 600 .env  # secrets only
```

Set the agent address and certificate fingerprint in `config.yaml`:

```yaml
agents:
  - alias: work
    title: Work Goose
    kind: goose
    url: https://<agent-address>:3284   # /acp is appended automatically
    secret_env: AGENT_WORK_SECRET
    tls_fingerprint: "<GOOSED_CERT_FINGERPRINT>"   # empty = trust on first use
    default_cwd: /home/<user>
```

In `.env`: `AGENT_WORK_SECRET=<the same secret>`.
Also set `ACPGW_API_TOKEN` to a separate random owner token. It must differ
from the agent secret and `ACPGW_MCP_TOKEN` (if configured).

### 3. Check the connection

```bash
uv run acpgw config check                        # config is valid, secrets are present
uv run python scripts/spike_acp.py init          # TLS + ACP handshake
uv run python scripts/spike_acp.py ping          # a short request to the agent
```

The spike script has more scenarios: `modes`, `permission`, `cancel`, `load`,
`all`. Traffic is recorded to `spike-runs/` (git-ignored). The scenarios
`permission --permission allow` and `cancel` run harmless `echo` and `sleep`
commands on the agent machine.

### 4. Start the daemon and use the CLI

```bash
uv run acpgw serve                           # keep this terminal open
```

In another terminal:

```bash
uv run acpgw status
uv run acpgw ask "Remember the code word AMBER. Answer only OK."
uv run acpgw ask "What was the code word?"    # same active session
uv run acpgw sessions
uv run acpgw new                             # start a fresh session
uv run acpgw switch 1                        # gateway session row id from sessions
```

For actions that need approval, keep a third terminal connected:

```bash
uv run acpgw approvals watch                 # allow once, reject, or skip interactively
```

The daemon alone does not count as a connected human. With no approval
watcher, requests are rejected. `acpgw approvals` lists pending requests;
`acpgw approvals approve <id>` and `reject <id>` decide an existing request
once. Long jobs can be submitted with `ask --no-stream`, retrieved with
`result <job_id> --wait 30`, and cancelled with `stop`. Ctrl+C during a
streaming `ask` requests cancellation of that job.

The CLI reads the same config and secrets as the daemon. Details and the
HTTP/SSE contract: [`docs/setup/gateway.md`](docs/setup/gateway.md).

### 5. Connect Hermes

Set a separate `ACPGW_MCP_TOKEN`, restart the daemon and add its `/mcp` URL
and bearer header to Hermes' `mcp_servers`. The six tools for each agent
support sessions, explicit threads, asynchronous jobs and cancellation.
Actions still require a human in `acpgw approvals watch`.
Configuration, tool arguments and compatibility checks:
[`docs/setup/hermes.md`](docs/setup/hermes.md).

### 6. Manage autostart on Linux/WSL

```bash
acpgw service install   # install/update the user unit using the selected config
acpgw service enable    # enable autostart and start now
acpgw service status    # service state, including when the daemon is stopped
acpgw service disable   # disable autostart and stop now
acpgw service --help    # start, stop, restart and uninstall too
```

WSL also needs a Windows logon task and a user manager that starts with the
distro. Instructions and the remaining reboot check:
[`docs/setup/service.md`](docs/setup/service.md).

## Configuration

| Source | Contents |
|---|---|
| `config.yaml` (`--config`, `ACPGW_CONFIG`, `./`, platform config dir) | structure: `gateway`, `agents`, `policy`, `logging` |
| `ACPGW_<SECTION>__<FIELD>` variables | overrides, e.g. `ACPGW_LOGGING__LEVEL=DEBUG` |
| `.env` (`--env-file`, `ACPGW_ENV_FILE`, `./`, config dir) | **secrets only**, referenced by name (`secret_env`) |

`uv run acpgw paths` shows the config, data and log directories on the
current platform.

## Development

```bash
uv sync                                  # environment and dependencies
git config core.hooksPath .githooks      # pre-commit: secret scan + ruff
uv run pytest                            # unit + integration tests
uv run ruff check && uv run ruff format --check
```

Integration tests start a mock goose (`tests/fakes/fake_goose.py`) whose
messages come from real goose 1.53.0 recordings in `tests/fixtures/acp/`; it
covers TLS, the secret, session modes, approvals, cancellation, session
loading and restarts. New recordings are cleaned with
`scripts/sanitize_fixture.py` before committing.

The development plan lives in [`road-map.md`](road-map.md) (statuses and
queue) and [`road-notes.md`](road-notes.md) (decisions and findings); both are
kept in Russian.

## Layout

```text
src/acp_gateway/   ACP client, core, SQLite storage, daemon, HTTP/SSE API, CLI, channels
scripts/           spike_acp.py (agent check), check_secrets.py (hook), sanitize_fixture.py
tests/             unit, integration, fakes, fixtures/acp
docs/              architecture.md, setup/, archive/
```

## License

[MIT](LICENSE)
