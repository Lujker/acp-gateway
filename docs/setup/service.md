# Install, configure and manage the gateway

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
changes and rerun the script. Existing configuration and tokens are kept.
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

After a real Windows reboot, sign in and verify `acpgw service status` and
`acpgw status` without opening a WSL terminal first. Also verify disable,
enable and shutdown behavior. The unit syntax and CLI paths are tested;
the PowerShell task and reboot scenario still require validation on Windows.
Native Windows service control is planned with `P3.2`/`P3.5`.
