# Independent interactive setup (0.2.0rc4)

Run `acpgw setup` in a terminal to open the setup menu. Configuration is divided
into independent steps; completing one returns to the menu. Opening the menu or
exiting does not write files. Start with `base`: select the operational role and
network settings. No agent, Telegram bot, enrollment or autostart is required.

```bash
acpgw setup
acpgw setup --section base --role server
acpgw setup --section telegram
acpgw setup --section agents
```

| Section | Purpose |
| --- | --- |
| base | Initial role/network configuration |
| gateway | API and server connector ingress |
| agents | Agent profiles on the executor computer |
| connection | Outgoing computer connection (not a server listener) |
| telegram | Bot token, allowlist and progress preferences |
| hermes | Local Hermes Agent MCP integration |
| computers | Private key enrollment, rotation and revocation |
| agent-tools | Local Goose installation, provider and service operations |
| service | Install/manage systemd user service or Windows connector task |
| policy / rate-limits / retention / logging | Independent runtime settings |
| check | Configuration validation and optional connection probes |

A server can run without agents. A connector can save its base first and add
local profiles and the outgoing connection in either order. Starting/installing
a connector service requires both. The server menu hides outgoing connection
setup; an explicit connection command cannot silently change its role. Device pairing and user/workspace accounts remain
separate work; this version uses explicit enrollment keys and automatic routes.

Agent configuration does not install local tools, launch services or export
keys implicitly. Each has its own menu action. Repeated edits preserve existing
secrets and unrelated settings. Cancellation leaves files/services unchanged.
Editing a running gateway requires an idle check and explicit stop confirmation;
the existing service is restored afterwards. A running Windows connector must
be disabled before editing. Private backups, port checks and rollback remain.

Choose a number or section name; Enter uses the displayed default. Explicit
paths are retained by every menu action:

```bash
acpgw --config /private/gateway.yaml --env-file /private/gateway.env setup
acpgw setup --config-dir /private/acpgw
```

Non-terminal `setup --non-interactive` still creates templates. Explicit
`--section all` retains the legacy combined wizard for existing workflows.
`acpgw menu` remains the operational console; its setup entry opens this menu.
See [the Ubuntu/Windows guide](self-install-pypi.ru.md) and
[installation/update behavior](distribution.md).


The setup menu filters sections by role. A fresh installation first offers base
setup. A server enrolls computers and discovers their profiles on connection;
local working directories, agent discovery and permissions are configured only
on each connector PC. Legacy explicit server routes remain available through
`setup --section agents` for old connectors. `agents list` and conversation
selection on a server read the live runtime catalog.

Upgrade the dispatcher before rc4 connectors: registration hello v2 includes
validated kind/cwd/mode metadata; other control frames remain v1. The rc4 server
still accepts legacy hello v1. The catalog persists in the existing database
schema 8 and restores offline routes after restart. Keep computer IDs and agent
aliases stable to retain conversation addresses. A changed cwd applies to new
sessions; existing sessions retain their recorded cwd.

A local cwd must be an existing absolute directory. It is the real project folder
passed to the agent, not a filesystem sandbox. Connector base setup may be saved
before agents or connection; complete both before service install/enable.
Windows connector settings must be edited while its task is disabled, then
re-enabled. See the installation guide for separate service and uv update steps.

MCP names escape underscores inside computer/alias components as `_u`, keeping
`__` for the component boundary. Simple names such as `home__hermes_ask` stay
unchanged. Refresh the client's tool list after a catalog change; stateless MCP
clients that cache tool names may need reconnection.
