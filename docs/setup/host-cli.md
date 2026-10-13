# Target-host setup through the CLI (development version)

The commands below are in the current source tree and need a new package release.
In a checkout use `uv run acpgw` in place of `acpgw`. Global `--config` and
`--env-file` options go before the command. Published v0.1.0 does not contain
this workflow yet.

The intended topology is a server with routed profiles such as `home/goose`
and `work/goose`, plus an outgoing connector on each executor computer. Each
computer keeps its own Goose credentials and model configuration. Only its
enrollment key and ACP traffic cross the dispatcher connection.

## 1. Prepare the dispatcher

```bash
acpgw setup --role server
```

Enter the ingress address, port and URL path; add the computers and their agent
aliases. The wizard exports a private enrollment key for every newly registered
computer, with its exact path shown after saving. Existing enrollment is kept.
Transfer each key privately to its intended computer; do not paste it into chat
or commit it. You can also manage keys separately:

```bash
acpgw computers list
acpgw computers enroll home --name "Home computer" --token-file /private/home.key
acpgw computers rotate home --token-file /private/home-new.key
acpgw computers revoke home
```

Enrollment creates an identity; add its routed agent profile through setup or
`agents configure` on the server as well. Rotation invalidates the previous key.

Use WSS for regular public operation: provide TLS files or put ingress behind
your existing reverse proxy. The owner API and MCP stay on loopback. DNS,
certificates and firewall rules remain host-specific; the wizard explains the
choices and asks explicitly before unencrypted WS testing.

The wizard can install/start the Gateway systemd user service on Linux/WSL.
For foreground operation:

```bash
acpgw doctor
acpgw start
```

Use another terminal for `acpgw doctor --live` and `acpgw computers status`.

## 2. Prepare an executor computer

Goose can be installed and its provider configured separately:

```bash
acpgw agents install goose --dry-run
acpgw agents install goose --version 1.53.0
acpgw agents provider
acpgw setup --role local
```

The version above is an example explicit pin, not a recommendation to downgrade.
Omit `--version` to use the official stable release. `--yes` accepts installation
for automation. Installation uses the official Goose shell installer on
Linux/macOS and PowerShell installer on Windows, installs into the Gateway data
directory under `tools/goose`, and leaves provider configuration to the explicit
`agents provider` step. It does not change your shell profile. An existing CLI
in PATH or `~/.local/bin` can also be used. Windows architecture support follows
the upstream installer; native service adapters are not implemented here.

In setup, choose Goose and **Prepare a managed local Goose listener**. The
wizard creates a loopback ACP profile and a separate random private agent key.
Choose **Connect this computer to an external dispatcher now**, enter its
WSS URL, computer ID and the local path of the copied enrollment key. This saves
the connector role without reentering the agent profile. Alternatively, start
with `setup --role connector`, or connect an already configured local host later:

```bash
acpgw connect
```

After saving, the wizard can install Goose, launch its provider setup, and enable
its Linux/WSL service. You can also perform these steps directly:

```bash
acpgw agents list
acpgw agents configure
acpgw agents serve goose          # foreground local listener
```

In another terminal on that computer:

```bash
acpgw doctor --live
acpgw start                       # outgoing connector for the saved role
```

For Linux/WSL autostart, the wizard offers both services, or use:

```bash
acpgw agents service goose install
acpgw agents service goose enable
acpgw service --role connector install \
  --dispatcher-url wss://server.example/acpgw/connect \
  --computer-id home --token-file /private/home.key
acpgw service --role connector enable
```

`agents service <alias>` also supports status, logs, start, stop, restart,
disable and uninstall. Stop/status work even with broken YAML. Commands refuse
unmanaged units and an explicitly selected config that differs from the unit.
Stop both the connector/Gateway and managed Goose services before updating or
uninstalling Gateway. Maintenance checks the Goose units too; uninstall removes
owned units while preserving configuration, enrollment and downloaded agent data.

For an existing Goose Desktop or another ACP executor, decline managed Goose
preparation. You can inspect a known local Goose URL/key first with
[`agents probe goose`](goose-attach.md), without creating a session or changing
its mode. Automatic Goose Desktop rediscovery remains open. Enter the known ACP
URL and hidden secret in setup. Generic ACP profiles are
supported; installing or managing those other executors is a separate adapter
task. The connector routes to them with the same enrollment workflow.

## 3. Diagnose and change settings

```bash
acpgw doctor
acpgw doctor --live --timeout 5
acpgw doctor --live --json
acpgw config edit --section agents
acpgw config edit --section connection
acpgw config edit --section telegram
acpgw config edit --section hermes
acpgw config edit --section gateway
acpgw menu
```

`doctor` returns 0 when there are no errors, 1 for diagnostic errors and 130
when interrupted. Warnings and unchecked items remain explicit in text/JSON.
Offline mode checks configuration, saved role, credentials, private files,
working/data directories, enrollment, TLS files and a Hermes integration saved
by setup. It does not repair files or start services.

Live mode additionally checks the owner API on a Gateway/server, initializes
direct ACP agents, probes dispatcher TCP/TLS with CA validation or its configured
pin, and checks an enabled Telegram bot with `getMe`. It creates no ACP session,
sends no model prompt or Telegram message, and leaves the existing TOFU pin store
unchanged. It does not register a diagnostic connector, which could replace the
active computer connection. Registration authentication is confirmed by
`start` and the server's `computers status`. TCP/TLS success alone does not prove
that the reverse proxy path or enrollment is accepted.

ACP initialize does not confirm model-provider authentication or successful
inference. Check those through Goose provider setup and an intentional prompt
after the transport is ready. Existing Goose authorization/keyring behavior
also applies to unattended services.

Selective editing preserves the saved role, unrelated settings and keys. API,
Telegram and Hermes belong on the Gateway/server host; a connector edits its
local profiles and dispatcher connection. Active managed Gateway services use
the wizard's existing review/idle/stop/restart safeguards. Stop foreground
instances yourself and restart them after editing their configuration.

Upstream references: [Goose installation](https://github.com/aaif-goose/goose/blob/main/documentation/docs/getting-started/installation.md)
and [CLI commands](https://github.com/aaif-goose/goose/blob/main/documentation/docs/guides/goose-cli-commands.md).
