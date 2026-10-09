# Install, configure and manage the gateway

For installation without Git and internal update/uninstall commands, see
[installation and updates](distribution.md). The Git checkout remains a separate
supported route.

## Install from a checkout

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
git clone https://github.com/Lujker/acp-gateway.git
cd acp-gateway
sh scripts/install.sh
uv tool update-shell  # if the tool bin directory is not already on PATH
```

The installer constrains dependencies to the checked-in `uv.lock` and uses
[uv's isolated tool environment](https://docs.astral.sh/uv/guides/tools/)
and creates a private initial configuration through `acpgw setup`. You can
run `acpgw` from any directory. To update a checkout installation, pull the
changes, stop the installed services/foreground processes and rerun the script. Existing configuration and tokens are kept.
If the executable/interpreter path changes, rerun `service install` and
`service restart` to update the installed unit.

For development, use `uv sync --frozen` followed by `uv run acpgw setup`.
Both installation routes expose the same CLI commands. A standalone Linux
executable with an embedded Python runtime can also be built; see
[binary build and installation](binary.md).

```bash
acpgw --help
acpgw setup --help
acpgw paths
```

`setup` creates `config.yaml` and `.env` in the platform config directory.
It generates independent owner and MCP tokens, with file mode `600` and a
new directory mode `700` on Linux. It prints paths, never tokens. It skips
existing files, including on repeated installation. Use `setup --config-dir
/path` for a custom destination; pass matching global `--config` and
`--env-file` options in later commands. An existing checkout's `config.yaml`
and `.env` still take precedence over platform defaults: install the service
with explicit paths when choosing between them.

Add your agent profile to the config and its secret to `.env`, following
[Work Goose setup](work-goose.md). New configurations start with no agents.
Then run `acpgw config check` and `acpgw serve` to check the foreground daemon.

## Linux and WSL service

The service is a systemd **user** unit; no root-owned gateway process or
system-wide service is installed. With systemd and a working user manager:

```bash
acpgw service --help
acpgw service install
acpgw service enable
acpgw service status
acpgw status
```

| Command | Behavior |
|---|---|
| `service install` | Write/update the managed unit and reload systemd; keep it stopped |
| `service enable` | Enable autostart and start now |
| `service disable` | Disable autostart and stop now |
| `service start` / `stop` | Start/stop now without changing autostart |
| `service restart` | Restart after a config or installation change |
| `service status` | Report load, active, substate, enabled state and PID; works while stopped |
| `service logs` | Print the latest 50 journal entries |
| `service uninstall` | Disable, stop and remove the managed unit; keep config, secrets and SQLite data |

`acpgw status` queries daemon health over the owner API. `acpgw service status`
queries systemd and works even with a broken app config or an unavailable
daemon. `status` returns success when it can read service state, including
inactive/not-installed; inspect `ActiveState` and `UnitFileState` separately.

To select persistent files explicitly:

```bash
acpgw --config /absolute/path/config.yaml --env-file /absolute/path/.env service install
```

The generated unit lives at `$XDG_CONFIG_HOME/systemd/user/acp-gateway.service`
(normally `~/.config/systemd/user/`). It references absolute interpreter,
config and `.env` paths, uses the config directory as its working directory,
restarts on failure, and applies `UMask=0077`. Secrets are read from `.env`
and never embedded in the unit. Persist required credentials in that file;
shell-only credentials are refused at installation. Use an absolute
`data_dir` if you override it. Shell `ACPGW_*` setting overrides are not
captured: persist settings in `config.yaml` for unattended startup.
Unmanaged units and symlinks are not overwritten or uninstalled.

Logs and low-level diagnostics:

```bash
journalctl --user -u acp-gateway.service -n 100 --no-pager
systemctl --user status acp-gateway.service
```

## Separate computer connector service

On the computer, prepare a persistent config containing direct network agent
profiles, an environment file with their required secrets, and the private
credential exported during VPS enrollment. An empty environment file is valid
when the local agents need no secrets. Connector installation does not require
owner, MCP or Telegram credentials. Use absolute paths for config, environment,
credential, custom data directory and log file.

```bash
acpgw --config /absolute/path/computer.yaml --env-file /absolute/path/computer.env \
  service --role connector install \
  --dispatcher-url ws://192.0.2.10:8766/connect \
  --computer-id work-laptop --token-file /private/path/work-laptop.key
acpgw service --role connector enable
acpgw service --role connector status
acpgw service --role connector logs
acpgw service --role connector restart
acpgw service --role connector disable
acpgw service --role connector uninstall
```

The separate unit is `acp-gateway-connector.service`. Install validates config
and credential permissions, writes the unit and reloads the user manager;
it leaves the service stopped. Lifecycle commands affect only the chosen role.
Uninstall preserves config, secrets, credentials and local data. `status` and
`logs` work without loading the application config; status includes the last
process result and exit code. Connector output uses the configured logging
format and optional rotating log file, with journal output available as above.

Network failures retry inside the running connector with delays of
1/2/5/10/30 seconds. Access, protocol and TLS failures stop with exit code 78;
invalid configuration syntax exits with 2. The unit uses
`RestartPreventExitStatus=2 78` to keep these failures stopped, following
[systemd's service exit policy](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml).
Unexpected crashes restart automatically. SIGTERM closes active streams and
exits successfully. Correct config or replace a revoked/rotated key, rerun
install if launch arguments changed, then explicitly restart the connector.

For the current test route, use plain WS by IP and port. A domain and nginx
root/subpath are supported by `--dispatcher-url`; optional WSS also accepts
`--tls-fingerprint`. See [connector configuration](../items/connector.md).
The same WSL keepalive task below supports either service role; enable the
connector unit separately. Installing the unit does not install the task.

Live acceptance remains an owner check: install/enable with real files, verify
`computers status` on the VPS, perform a real agent turn, stop/start the service,
check reconnect after a network interruption, and verify Windows logon startup.
Automated tests use mocked service managers and a real connector CLI process
against a mock ACP agent; they do not install or restart your running services.

## WSL at Windows logon

Enable [systemd in WSL](https://learn.microsoft.com/en-us/windows/wsl/systemd)
if your distro does not already use it. Add `[boot]` with `systemd=true` to
`/etc/wsl.conf`, then restart WSL with `wsl.exe --shutdown` from PowerShell.
This stops all WSL distributions, so do it when their work can be interrupted.

Inside WSL, enable the user manager at distro boot:

```bash
sudo loginctl enable-linger "$USER"
acpgw service install
acpgw service enable
acpgw service status
```

Systemd services do not keep a WSL instance alive by themselves, as described
in [Microsoft's WSL documentation](https://learn.microsoft.com/en-us/windows/wsl/systemd).
The Windows logon task starts the selected distro/user and runs `sleep infinity`
to keep the instance available while you are logged in. Gateway autostart
remains controlled by the systemd unit: the task never starts a disabled
gateway service directly.

From PowerShell in a Windows-accessible checkout:

```powershell
.\deploy\windows\wsl-task.ps1 -Action install -Distro Ubuntu -LinuxUser your_linux_user
.\deploy\windows\wsl-task.ps1 -Action enable
.\deploy\windows\wsl-task.ps1 -Action status
# Disable/remove the Windows keepalive separately:
.\deploy\windows\wsl-task.ps1 -Action disable
.\deploy\windows\wsl-task.ps1 -Action uninstall
```

The task uses your interactive Windows identity without storing a password,
is initially disabled, and has no execution time limit. Task management
refuses to change an existing task with another description. Installing the
Linux service does not install this Windows task or enable lingering for you.
The helper accepts distro names containing letters, digits, dots, underscores
and hyphens, without spaces. It passes distro/user as unquoted arguments:
quoted names caused `WSL_E_DISTRO_NOT_FOUND` on the tested Windows host.

To repeat the real Task Scheduler lifecycle check without changing your
installed task, run from PowerShell:

```powershell
.\scripts\smoke_wsl_task.ps1 -Distro Ubuntu-22.04 -LinuxUser your_linux_user
```

This creates a uniquely named temporary task, verifies the logon trigger and
unlimited runtime, checks install/start/status/stop/re-enable/uninstall, and
cleans up on failure. It holds each running state for three seconds before
checking, so a launcher that immediately exits cannot pass. This full cycle
passed on the development Windows/WSL host on 2026-10-08.

If `systemctl --user` fails with `Failed to connect to bus`, confirm that
`$XDG_RUNTIME_DIR/bus` exists and your distro provides a user D-Bus session.
On Ubuntu/Debian, check/install `dbus-user-session`. Linger alone does not
provide this socket. Restart the user manager or WSL distro when the affected
work can be interrupted; restarting the user manager stops all of that user's
systemd services, including ssh-agent. On the development host, installing
the missing package and restarting `user@1000.service` restored the bus.
The first start hit a transient cgroup `EBUSY`; a subsequent start succeeded.

The real gateway user-service lifecycle passed for both the checkout CLI
and standalone Linux executable, including recovery after `SIGKILL` and
preservation of config/tokens/database on uninstall. To repeat the opt-in
check when no gateway service is already installed:

```bash
uv run --frozen python scripts/smoke_service.py .venv/bin/acpgw
# Or test a built executable:
uv run --frozen python scripts/smoke_service.py dist/acpgw-linux-x86_64/acpgw
```

This briefly installs/enables the real user unit with temporary application
config/data, then uninstalls it. It refuses to replace any existing unit.

After a real Windows reboot, sign in and verify `acpgw service status` and
`acpgw status` without opening a WSL terminal first. Also verify disable,
enable and shutdown behavior. The unit syntax and CLI paths are tested;
the PowerShell task and user-service lifecycles passed on Windows/WSL.
The actual reboot scenario still requires validation.
Native Windows service control is planned with `P3.2`/`P3.5`.
