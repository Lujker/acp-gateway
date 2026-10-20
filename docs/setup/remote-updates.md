# Telegram updates: dispatcher and connectors

> **Since 0.2.2:** automatic bootstrap and official immutable Docker delivery
> are described in [Updates out of the box](updates-out-of-box.md). Normal
> setup/start does not require `--init`, `install`, `enable`, or per-version
> `allow-image`. Explicit `updates.enabled: false` remains an opt-out. The manual
> instructions below remain the advanced path and match 0.2.1.

[Documentation](../README.md) · [Development contract](../items/remote-connector-management.ru.md)

This feature is included in 0.2.1 and is not present in 0.2.0. Start with the
[0.2.1 upgrade instructions](update-0.2.1.ru.md). The first scope is
single-owner, one target at a time, newer
stable `major.minor.patch` versions, Linux/Windows uv services and a Linux Docker
dispatcher. macOS, prereleases, Git/file installs and batch updates are excluded.

## How it runs

Telegram shows versions with `/updates`; **Check versions** resolves the stable
PyPI version. **Update** creates a saved confirmation with the exact computer,
installed version and requested version. **Confirm** admits that operation once.
An expired confirmation or changed computer connection/version is rejected.

The local computer must enable updates and run an independent updater companion.
The companion owns its local policy and installation commands. Requests contain
an operation ID and exact version, never shell commands, executable paths or URLs.
The connector continues using its outgoing authenticated WS/WSS connection; no
new public endpoint is opened. Old peers keep agent operation without update rights.

The gateway saves confirmation and dispatch state in `update-control.db`. The
local mailbox is `update-spool/operations.db`, separate from `gateway.db` so it
survives package/database rollback. A companion claims an operation durably before
calling an installer. Restarting it never dispatches a claimed operation again.

`/update_history` and `/update_status ID` retrieve saved state after reconnect or
restart. The bot also sends a terminal/unknown notification to the originating
allowed private chat. A send with an ambiguous outcome is not retried automatically;
the saved operation remains available. Reading status never repeats installation.

## uv installations

Use this only in an installation that already contains this feature and is owned
by `uv tool`, from the registry, with the normal managed connector/gateway service.
First generate a private companion policy on the target computer:

```sh
acpgw --config /absolute/config.yaml --env-file /absolute/private.env updater \
  --policy /absolute/private/updater.json --init --role connector
```

In Windows PowerShell, use one line with your native paths:

```powershell
acpgw --config "C:\private\acpgw\config.yaml" --env-file "C:\private\acpgw\private.env" updater --policy "C:\private\acpgw\updater.json" --init --role connector
```

For a native dispatcher use `--role dispatcher`. The connector target is read from
its saved enrollment; the policy records the exact config/env, uv entry point and
installation-specific result directory. It does not store enrollment/API/bot secrets.
An existing policy is never overwritten by `--init`.

Enable the local gateway/connector config and restart that component normally:

```yaml
updates:
  enabled: true
  # Optional; use the same directory as updater.json:
  # spool_dir: /absolute/private/update-spool
```

Install the independent companion's autostart, then enable it:

```sh
acpgw updater --policy /absolute/private/updater.json install
acpgw updater --policy /absolute/private/updater.json enable
acpgw updater --policy /absolute/private/updater.json status
```

These same commands work on native Windows with quoted native paths. Linux uses
a separate systemd user unit; Windows uses a separate Task Scheduler logon task
under the current user, without an account password or elevation. First installation
does not enable/start it; reinstallation preserves existing enablement. Linux user
units start with the user manager; unattended startup without login requires host
user-manager/linger configuration. The Windows task starts at that user's login.

The installer saves a private runtime beside the policy and starts it with the
base Python interpreter, outside the uv environment being replaced. Neither task
nor unit invokes `acpgw` to start the worker. Do not run it as a child lifecycle of
the connector/dispatcher service. The base interpreter must remain installed.
Reinstall the companion while stopped to refresh its helper code after upgrading.

`start`, `stop`, `restart`, `disable` and `uninstall` manage this independent
companion. Stop is cooperative and bound to the current worker instance. An active
installation/recovery finishes first; if the 30-second wait expires the command
reports failure and does not force-stop a process. Retry after inspecting status.
Uninstall removes autostart and keeps the policy, helpers and operation history.
Foreground use remains available: `acpgw updater --policy /absolute/private/updater.json`.

`acpgw setup --interactive` and `acpgw menu` expose an **updater** section for
local settings, policy initialization and service actions. Settings are opt-in;
they do not implicitly reinstall or restart the connector/gateway service.

The companion checks the current installation/version, invokes the existing
`update --version VERSION --with-services` coordinator, and follows only that
operation's tagged result. The coordinator reserves idle admission, handles service
state, package recovery and health. Active jobs/approvals and uncertain sessions do
not receive an implicit force/restart acknowledgment. A failed cycle is reported as
failed; it does not assert that every failure automatically restored a healthy service.

## Docker dispatcher

Run the companion **on the Docker host** with permission to manage that Compose
project. The dispatcher does not receive the Docker socket. The host and container
share only the private mailbox directory; their configured `spool_dir` paths differ
but must refer to the same mounted directory and user identity.

Example with the repository's `runtime` and `state` layout, container user matching
the host user, and `state/update-spool` owned by that user with mode 0700. Replace
all host paths and the image ID with your actual values:

```json
{
  "version": 1,
  "backend": "docker",
  "role": "dispatcher",
  "target": "dispatcher",
  "spool_dir": "/srv/acpgw/state/update-spool",
  "docker": "/usr/bin/docker",
  "project_dir": "/srv/acpgw",
  "compose_files": [
    "/srv/acpgw/compose.yaml",
    "/srv/acpgw/compose.proxy.yaml"
  ],
  "service": "gateway",
  "config": "/config/gateway.yaml",
  "env_file": "/config/gateway.env",
  "container_spool": "/data/update-spool",
  "override_file": "/srv/private/acpgw-update-image.json",
  "database": "/srv/acpgw/state/gateway.db",
  "work_dir": "/srv/private/acpgw-update-backups",
  "images": {
    "0.3.0": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

`0.3.0` and the image ID above are examples, not published artifacts. Prepare and
inspect the reviewed image on the host first, then map its exact version to a local
image ID or `repository@sha256:digest`. Mutable tags and arbitrary sources are rejected;
the companion never builds/pulls an unreviewed image automatically. It verifies the
running version after replacement. Keep the policy and backups outside container
writable mounts. Keep `override_file` as the last `-f` in later manual Compose commands
so an ordinary restart does not silently revert the selected image. The updater
uses the original project/files/networks/volumes and selects only the gateway service.

Install/enable on the host with the same `updater ... install` and `enable` commands
above (or run it in the foreground). In the container config
set `updates.enabled: true`; the default spool is `/data/update-spool` when `data_dir`
is `/data`. The original config/env mounts, enrollment and state volume stay in use.

Before replacement the companion reserves idle gateway admission and saves a SQLite
backup including committed WAL data. It pins the old image ID, writes the image
override, recreates the gateway and checks the installed version and owner API health.
On an acknowledged failure it stops the new container, restores the database and old
image, then checks recovery. A timeout or failed recovery becomes **unknown**, without
launching a competing automatic installation/rollback. Recovery files remain on the
host. Avoid concurrent manual changes to this Compose service during an operation.

To register another reviewed image without editing JSON manually, stop the companion
and supply its exact stable version and a local immutable image reference:

```sh
acpgw updater --policy /srv/private/updater.json stop
acpgw updater --policy /srv/private/updater.json allow-image \
  --version 0.3.0 --image sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
acpgw updater --policy /srv/private/updater.json start
```

The example is illustrative. This command only inspects an already present image
and atomically saves its content-addressed ID in the policy. It never pulls, builds
or publishes. A version cannot be silently retargeted to another image. The operator
reviews the version-to-image mapping; runtime version/health verification still
applies when updating. Restart publishes the changed allowed-version list.

Docker replacement commands follow the official
[Compose up reference](https://docs.docker.com/reference/cli/docker/compose/up/) and
[Compose exec reference](https://docs.docker.com/reference/cli/docker/compose/exec/).
Current automated evidence uses command fakes and a real temporary SQLite database;
it does not claim a live Docker-engine or native Windows update run.

## Status and recovery boundaries

- **Queued/running:** the existing operation is admitted; status refresh is read-only.
- **Succeeded:** the companion observed the requested version and completed health/result checks.
- **Failed:** inspect the local updater and service state. Docker `rolled_back` specifically
  means the old image/database passed the recovery check.
- **Unknown:** do not submit a replacement operation. Inspect the exact operation's result
  and recovery files. uv results can be reconciled after companion restart; an ambiguous
  Docker replacement needs local diagnosis. Unknown operations block another update of
  the same target until resolved, preventing duplicate mutations.

Local policy changes take effect when the companion is restarted. Disabling gateway
updates blocks new admissions; it does not cancel an already admitted installation.
No agent `/mode auto` setting grants update authority. Multi-user management remains
a separate ownership/scopes project.

### Local resolution of an executor's unknown operation

Run `acpgw updater --policy PATH diagnose` on the target host for saved worker,
operation and resolution records, the observed version and health. Docker reports
the backup and image-override locations too. Diagnosis does not install anything.

First stop the companion, inspect the exact result/recovery files, and verify
locally that all installer/rollback processes have finished. Repair the installation
and release any retained maintenance reservation using its original local recovery
procedure. Then explicitly record the outcome, for example:

```sh
acpgw updater --policy /absolute/private/updater.json resolve \
  --operation OPERATION_ID --outcome succeeded \
  --note "Inspected installation and recovered service" --acknowledge-idle
```

Resolution requires an existing `unknown` operation belonging to that policy and
exclusive access to the stopped worker's lease. `succeeded` requires the requested
version; `failed` requires the previous version. Both require healthy service:
the connector uses its local maintenance status, the dispatcher uses its owner API.
No installation, service restart or rollback is triggered by this command. The
outcome and audit note are saved atomically. After restarting the companion, refresh
the operation's status in Telegram to reconcile the saved terminal outcome.

This resolves an executor's retained unknown record. If dispatch was lost before
the target saved any record, or the target/grant is unavailable, it does not invent
an outcome or clear the dispatcher's conservative unknown fence.
