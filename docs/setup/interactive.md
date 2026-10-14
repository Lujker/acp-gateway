# Independent interactive setup (0.2.0rc3)

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
| agents | Local profiles or dispatcher routes |
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
a connector service requires both. A server's connection menu does not silently
convert its role to connector. Device pairing and user/workspace accounts remain
separate work; this version still uses explicit enrollment keys and routes.

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
