# Interactive CLI (development version)

These commands are available in the current source tree. The published v0.1.0
package still has template-only setup; this feature needs a new release.
From a checkout, prefix the commands below with `uv run`.

For the complete dispatcher/executor workflow, Goose installation, targeted
settings and `doctor`, see [target-host CLI setup](host-cli.md). A disposable
[Docker laboratory](cli-lab.md) exercises the topology without model accounts.

```bash
acpgw menu                 # configuration and management console
acpgw setup --interactive  # first setup or repeat setup
acpgw config edit          # edit the selected configuration with the wizard
acpgw start                # run the saved role in the foreground
```

Running `acpgw` without arguments in a terminal opens the menu. Choose a number
or a name; Enter selects the displayed default. Outside a terminal, commands
keep their scriptable behavior: `setup --non-interactive` creates templates,
and explicit interactive commands require a terminal.

To manage a particular installation, put its paths before the command:

```bash
acpgw --config /path/to/config.yaml --env-file /path/to/.env menu
```

The menu retains these paths for all actions. Setup defaults to the platform
configuration directory; `setup --config-dir /path` chooses another directory.
Interactive setup and `start` use `.env` beside the selected YAML when no
explicit secrets file is provided.

The wizard supports three roles:

| Role | What to prepare | What the wizard configures |
| --- | --- | --- |
| `local` | A running ACP listener, for example Goose | Direct agent profiles, local API/MCP, optional Telegram and Hermes |
| `server` | VPS, reverse proxy and TLS for regular public use | Connector ingress, computer/agent routes and private enrollment key exports |
| `connector` | Local ACP listener, server URL, computer ID and enrollment key | Local profiles and outgoing server connection; Telegram remains on the server |

For two computers, add two routed profiles on the server, each with its own
computer ID. The wizard can export a separate private key per new computer to
`enrollment/<computer-id>.key`. Transfer each key privately to its computer and
run setup there with role `connector`. Existing enrolled computers keep their
keys. Use the Computers menu to list, enroll, rotate or revoke credentials.
Enrolling a computer through that menu also requires adding its agent route
through setup on the server. Rotation invalidates the old key; point the
computer at the new file and restart its connector.

Repeated setup lets you keep, edit, add, remove or replace agent profiles.
Hidden secret inputs keep the saved value on Enter. Existing keys and unrelated
settings are preserved. Review the final plan before saving; cancellation
before saving leaves configuration and services untouched. Changed files have
private backups in `setup-backups/`, and partial file writes are rolled back.
YAML comments and formatting are rewritten when saving.

On Linux/WSL with systemd, setup can install and start the matching user service.
It checks ownership of an existing unit, asks before stopping a running managed
service, and checks that the gateway has no running jobs. Port conflicts abort
saving and restore stopped services. Foreground instances must be stopped
explicitly before changing their listener settings. `start` refuses to duplicate
an already running managed service. Use the Service menu for status, logs,
start/stop/restart and enabling/disabling autostart. Native Windows connectors
also have a [Task Scheduler adapter](windows-connector.md); the wizard's complete
Windows wizard prepares private files and persistent data/log paths and offers
task registration, enable and startup with rollback on failure. Disable an
existing task before editing; its snapshot must match the selected config/env.
See the Windows runbook for recovery boundaries. macOS uses foreground
`start`; native macOS services remain a separate roadmap item.

Optional connection checks perform ACP initialization without creating a session
or sending a model prompt, Telegram `getMe` without sending a message, and a TCP
reachability probe for the server. Server credentials and TLS are validated when
the connector starts. Choosing agent type `hermes` attaches to an existing
backend through read-only discovery: instance/profile selection, without a
session or model call. It stores the directory/role/profile, never the local
token or temporary port. See [Hermes attach](hermes-attach.md) for restrictions
on dedicated conversations, cancellation and recovery. This attachment does
not require restarting Hermes. The separate optional MCP integration updates
its `acpgw` MCP entry and MCP token while retaining other servers; restart
Hermes after changing that MCP configuration to discover the tools.
Install and authorize Goose/Hermes separately, obtain the Telegram bot token
from BotFather, and arrange DNS/TLS before regular public use.

The menu also exposes configuration checks, runtime status, session listing,
new sessions, agent messages, cancellation and interactive human approvals.
Use a separate terminal for `approvals watch` while a message is waiting for a
human decision. Ctrl+C returns from a running action to the menu; interrupting
a streamed message requests job cancellation. Update checking is available in
the menu; installation, rollback and uninstall keep their explicit CLI commands
described in [distribution.md](distribution.md).
