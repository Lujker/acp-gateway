# Install, update and remove ACP Gateway

Linux/WSL is the verified first target. Manual native runner checks also cover
Windows/macOS and Linux ARM; their results are recorded in the roadmap.
Native Windows connectors support a per-user Task Scheduler logon task.
Native Windows gateway and macOS service adapters remain separate milestones.
All modes keep configuration, enrollment keys, pins and SQLite outside the
installed program. Stop jobs and processes before replacing an installation.

## Simple PyPI installation

For a new installation, install uv and run:

```bash
uv tool install --python 3.12 "acp-gateway==0.2.0rc8"
acpgw setup
```

On native Windows add `--managed-python` to the uv command. Setup first configures
the role; each further section is independent. Servers discover agent profiles
from authenticated rc4 connectors. Configure the executor and its project folder
on the PC; enroll its key on the server. See the [Ubuntu/Windows walkthrough](self-install-pypi.ru.md).
For an existing pinned tool use `uv tool install --upgrade --python 3.12
"acp-gateway==0.2.0rc8"` after stopping jobs/services, then restart the service.
Upgrade servers before connectors. Package replacement keeps config/keys/data;
it does not start or reconfigure services automatically.

## Release bundle: no checkout required

A release bundle contains a wheel, source distribution, locked requirements,
installer, documentation and an independent Docker deployment directory.
Download a reviewed `acpgw-VERSION-bundle.tar.gz` and `SHA256SUMS` from this
repository's GitHub Release, verify the archive's checksum, then extract it.
A release must actually be published before its download URL can be used.
Until then, the same bundle can be transferred from a local build over SSH/SCP.

```bash
# With the bundle and SHA256SUMS in the current directory; use the actual version:
grep '  acpgw-0.1.0-bundle.tar.gz$' SHA256SUMS | sha256sum -c -
tar -xzf acpgw-0.1.0-bundle.tar.gz
cd acpgw-0.1.0
sh install.sh
uv tool update-shell
acpgw version
acpgw config check
```

On Windows, extract with `tar -xzf acpgw-0.1.0-bundle.tar.gz`, enter the
`acpgw-0.1.0` directory and run `powershell -NoProfile -File .\install.ps1`.
Use `-ConfigDir C:\path\to\config` for a custom configuration directory.
The installer does not change the machine's PowerShell execution policy.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first.
The installer requests Python 3.12 through uv. It installs the adjacent wheel
with the release's locked dependencies, then runs `acpgw setup`. Existing config
and tokens are kept. Edit the generated config and env file before connecting
agents or enabling Telegram. The installed `acpgw` works from any directory;
the extracted bundle is not needed for day-to-day operation.

Direct installation of a reviewed wheel also works:

```bash
uv tool install --python 3.12 ./acp_gateway-0.1.0-py3-none-any.whl
acpgw setup
```

This direct command uses wheel dependency ranges. Use the bundle installer or
`--constraints requirements.lock.txt` for the release's tested dependency set.

Version [0.1.0 is published in PyPI](https://pypi.org/project/acp-gateway/0.1.0/).
Install directly from the registry without cloning:

```bash
uv tool install --python 3.12 acp-gateway==0.1.0
uv tool update-shell
acpgw setup
acpgw update
acpgw uninstall
```

The matching public Docker image is `ghcr.io/lujker/acp-gateway:0.1.0`
(Linux x86_64); the deployment bundle is attached to the
[GitHub Release](https://github.com/Lujker/acp-gateway/releases/tag/v0.1.0).
pipx can install the same wheel/package; with pip, use a dedicated virtual environment. Internal mutation
commands manage only installations owned by `uv tool`.

## Internal commands

```bash
acpgw version
acpgw version --json
acpgw update --check
acpgw update --dry-run
acpgw update --status
acpgw uninstall --dry-run
```

`version` shows package version, manager and source type without printing private
source URLs. These commands work even if configuration is missing or invalid.
`update --check` checks this project's stable, non-yanked PyPI releases; it does
not check the latest Git commit, release wheel or binary. A missing PyPI release
is reported explicitly. This check is advisory; installation constraints and
Python/platform requirements still determine what uv can install.

Since 0.2.0rc6, `acpgw update --stable --with-services`
selects a newer stable, non-yanked PyPI version and updates the selected managed
service through the existing stop/health/restart cycle. It resolves an exact
version before stopping anything, so a saved uv `==version` constraint cannot
prevent the upgrade. With no newer stable release it changes neither package
nor services, including when the installed RC is newer than the stable release.
This flag requires a registry installation; explicit `--version` still selects
an RC. Published rc5 does not include `--stable`.

Before updating, stop both services if installed and any foreground Gateway,
connector or dispatcher from this installation. First complete/cancel active
jobs; an update does not recover an interrupted tool action.

Current development sources provide [admission reservation](maintenance.md)
through `maintenance prepare/resume/status`. Linux systemd installations can
use the opt-in managed cycle below. Plain `update` retains its stopped-process
precondition. Current Windows connector sources also support a managed Task
Scheduler cycle; fresh installed-package Windows acceptance remains open.

### Linux systemd update with service restoration (development)

```bash
acpgw --config /absolute/path/config.yaml --env-file /absolute/path/.env \
  update --with-services --from /absolute/path/reviewed-checkout
# Select a connector service explicitly when needed:
acpgw --config /absolute/path/computer.yaml --env-file /absolute/path/computer.env \
  update --with-services --service-role connector --from /absolute/path/reviewed-checkout
# With a checkout launcher:
sh scripts/update.sh --config-dir /absolute/path/config-dir --with-services
```

The selected managed unit must use this installation and those exact config/env
paths. Without `--service-role`, saved connector setup selects connector; other
setups select gateway. The cycle handles one service. Other active processes or
services still block package replacement through the existing exclusive lease
and preflight; stop them explicitly or use separate tool installations.

An external stdlib coordinator snapshots enabled/running state, reserves admission
if running, disables autostart and stops the selected service. Only after confirmed
stop does it invoke the existing updater with recovery snapshot and health checks.
It restores the original enabled/running combination, including enabled-but-stopped
and disabled-but-running. A previously running service is checked after restart.
Owned unit contents are retained; changed definitions/processes refuse automatic
recovery. Interpreter paths remain the same uv tool paths; this does not migrate
services between installations. No setup/enrollment is repeated.

Busy/uncertain work refuses reservation before stop. An installer or package
health failure uses the existing program/database rollback, then attempts to
restore service state. A stop failure attempts to resume the original process.
It never forces a kill. A service restoration failure is reported as failure,
with private recovery files retained. A newly started process is not silently
rolled back again: that could discard new work. Inspect status/logs before retrying.

`update --status` reports the whole service cycle, including restoration; an inner
package success is not the final result. A coordinator lock serializes it with
other internal package mutations. External uv/systemctl operations bypass this
coordination. `--dry-run` changes nothing; it is a plan, not full live validation.
`update --rollback --with-services` explicitly restores the previous package and
database with the same service lifecycle; the database consequences below apply.
Linux uses a synchronous coordinator; Windows has the handoff below. It does not reboot
hosts, configure lingering, or prove remote Hermes/VPS connectivity.

### Windows connector update with task restoration (development)

```powershell
acpgw --config C:\private\acpgw\config.yaml --env-file C:\private\acpgw\.env update --with-services --from C:\reviewed\acp-gateway
acpgw update --status
# From a checkout, wait for the verified result:
powershell -NoProfile -File .\scripts\update.ps1 --config-dir C:\private\acpgw --with-services
```

The task must belong to this Windows user and installation and use the exact
config/env/data paths. Windows supports role `connector`; gateway service updates
use Linux/systemd. The coordinator captures the stable task definition, control
ID, live instance and enabled/running state. It reserves an idle Hermes connector,
disables and gracefully stops the task, waits for the existing package helper's
actual update/health/rollback result, then restores the original state. All four
enabled/running combinations are handled. Definition/instance changes,
busy/uncertain work and unsupported profiles refuse automatic action.

Both helpers run outside the tool environment after the original CLI exits.
Direct Windows CLI exit 0 means queued; wait for `update --status` or use the
synchronous checkout script. Status covers task restoration. A private pending
marker and coordinator lock block unrelated internal maintenance; only its own
inner package helper can pass. Unexpected process/power loss can leave a pending
result; inspect the recovery plan and task status before repairing it.

Task XML is retained apart from enabled state; interpreter/installation migration
is not performed. A restarted task must publish its matching local activity
record; VPS/Hermes connectivity remains separate. Package failure invokes the
existing package/database rollback, then restores the task. Task restoration
failure retains private recovery files. There is no force kill or second silent
rollback after a new process has started. Fresh installed-package Windows
acceptance remains a separate P4.8.5 step.

```bash
acpgw service stop
acpgw service --role connector stop
# For a registry installation:
acpgw update
# To change a registry version constraint intentionally:
acpgw update --version 0.1.0
# For a downloaded/extracted release:
acpgw update --from /absolute/path/acp_gateway-0.1.0-py3-none-any.whl --constraints /absolute/path/requirements.lock.txt
```

Run only the stop commands for services you have installed. New versions hold
runtime leases that block internal maintenance while any foreground process
uses the same environment. Older versions cannot provide that lease: stop their
processes yourself. All participating commands must use the same platform data
root (HOME/XDG environment). Managed units from another installation are refused.

The updater copies a standard-library helper outside the tool environment and
runs it with the base Python interpreter. Before invoking uv it creates a private
snapshot of the environment, entry point and selected SQLite database (including
committed WAL data). It checks the new entry point, version, configuration and
SQL migrations before reporting success. Installation or health-check failure
restores the snapshot. Configuration, secrets and pins remain in place.
An abrupt power loss or external interference is not a transactional guarantee.
Services stay stopped; after success, start the services that were running:

```bash
acpgw version
acpgw config check
acpgw service start
acpgw service --role connector start
```

On native Windows an internal `update`, `update --rollback` or `uninstall`
starts a separate helper and returns after queuing it. The helper waits until
the original CLI exits, acquires the installation lock again, and then performs
the operation. This lets Windows release the running interpreter before uv
replaces its files. **Exit code 0 from the initial command means queued, not
verified success.** Wait for the helper's completion message or run
`acpgw update --status`: 0 means succeeded, 1 failed, 2 queued/running.
The initial command prints the durable JSON result file path; read it directly
if the entry point is temporarily being replaced or has been uninstalled.
Do not restart processes while the operation is queued/running.

For scripted Windows installation/updating, prefer `install.ps1`: it starts
the helper after the CLI has exited and waits for its verified exit code.
Linux/macOS internal maintenance commands wait until the operation finishes.

One recovery snapshot is retained under the platform data directory's
`updates/<installation-id>/`; a later successful operation replaces it. A failed
recovery keeps files for inspection. These files can include private source URLs
and database contents; treat them like other private application data.

To explicitly restore the previous program **and its matching database snapshot**:

```bash
acpgw update --rollback
```

Stop processes first and use the same `--config`/`--env-file` as during the update.
Rollback restores database contents from before that update: later sessions,
enrollments and other database changes are replaced. Back up the current database
while stopped before choosing this operation. The rollback retains a snapshot of
the state it replaced. Only the selected database is backed up; if several configs
share one installation, back up their other databases separately.

Release and checkout installers use this guarded updater when an `acpgw` entry
point already exists. Direct `uv tool install/upgrade/uninstall` bypass these
checks. An older version lacking these commands requires a one-time package-manager
replacement after stopping its processes and backing up its data.

Remove the stopped tool and its managed units:

```bash
acpgw uninstall
```

Both units are checked before any removal. Configuration, secrets, keys, pins
and SQLite remain. There is no implicit purge. For a non-uv installation, use
`service uninstall` for its managed units and the original package manager for
the program. Binary self-update, signed release verification and opt-in background
updates remain separate work; the current updater runs only on an explicit command.

## Git installation: separate acceptance path

Without a manual clone, uv can install a reviewed Git tag/commit (Git is required):

```bash
uv tool install --python 3.12 'git+https://github.com/Lujker/acp-gateway@REVIEWED_REF'
acpgw setup
# Stop its services/foreground processes before changing the ref:
acpgw update --from 'git+https://github.com/Lujker/acp-gateway@NEW_REVIEWED_REF'
```

Git refs above are placeholders. File/Git installations require explicit
`update --from`; they do not silently switch to PyPI. For a development checkout,
use the checkout scripts below. An editable `uv run acpgw` environment is not
mistaken for an installed uv tool.

### Checkout scripts (Linux/WSL and Windows)

The checkout is a package source; execution uses the isolated `uv tool`
installation. Install uv first. The scripts request managed Python 3.12 and
export dependencies from the checkout's frozen `uv.lock`. They never pull,
reset or clean Git and never switch updates to PyPI. Select/review the desired
checkout before updating. There is one installation per uv tool directory.
Fresh checkout installs and explicit-source internal updates refresh the ACP
Gateway package build so uv cannot reuse stale source merely because its package
version is unchanged. Dependencies remain constrained by the selected lock.

From a Linux checkout:

```bash
sh scripts/install.sh --config-dir "$HOME/.config/acp-gateway"
sh scripts/run.sh version
sh scripts/run.sh --config "$HOME/.config/acp-gateway/config.yaml" status
# Finish active jobs; stop installed services and foreground processes first.
sh scripts/update.sh --config-dir "$HOME/.config/acp-gateway"
```

From a native Windows checkout:

```powershell
powershell -NoProfile -File .\scripts\install.ps1 --config-dir C:\private\acpgw
powershell -NoProfile -File .\scripts\run.ps1 version
powershell -NoProfile -File .\scripts\run.ps1 --config C:\private\acpgw\config.yaml status
# Finish active jobs; stop installed services and foreground processes first.
powershell -NoProfile -File .\scripts\update.ps1 --config-dir C:\private\acpgw
```

If a downloaded/UNC script is blocked by execution policy, use the per-process
`-ExecutionPolicy Bypass` option with a reviewed script. The scripts do not
change persistent policy. Quote paths containing spaces in either shell.

Only a fresh installation opens `setup`; its extra arguments (for example
`--role connector`) are forwarded to the wizard. Repeating `install` on an
existing managed installation performs the same guarded update as `update`,
without setup. Extra setup arguments on an existing installation are rejected;
use `run setup --config-dir ...` to reconfigure explicitly. `update` accepts only
`--config-dir` and refuses a missing installation.

`run` forwards all arguments unchanged and defaults to `start` without arguments.
It locates the entry point through `uv tool dir --bin` and checks uv ownership,
so a different `acpgw` on PATH is not launched. It retains the caller's working
directory and normal config discovery; pass explicit `--config` and `--env-file`
when using custom paths. Invoke scripts by absolute path from outside the checkout.
Both update scripts wait for the updater's verified result, including the
external Windows helper, and propagate failures. Settings, keys and the selected
database follow the existing updater's preservation/rollback contract.
Checkout updates accept `--with-services` and optional `--service-role` for the
managed cycles above. Windows selects the connector task.

## VPS Docker installation without Git

Extract the bundle on the VPS, then:

```bash
python3 deploy/docker/prepare.py
cd deploy/docker
# Set ACPGW_IMAGE in .env if using another reviewed registry/tag/digest.
# Edit runtime/gateway.yaml and runtime/gateway.env before starting.
# With an image-inclusive bundle:
docker load -i ../../gateway-image.tar
# Otherwise, for an image already published in your registry: docker compose pull
# Continue with the IP or nginx override in the VPS acceptance runbook.
```

Bundle Compose uses a versioned image and contains no source-build requirement.
The image must be published, included in the bundle with --include-image, transferred with docker save/load, or overridden
with an already available local image. No owner API/MCP ports are published.
Follow [VPS acceptance](vps-acceptance.md)
([на русском](vps-acceptance.ru.md)) for two computers and Telegram.

For image updates, finish active jobs, back up private runtime/state while the
Gateway is stopped, change `ACPGW_IMAGE` in `.env`, then pull and recreate using
exactly the active override files. Example for the HTTPS stage:

```bash
docker compose -f compose.yaml -f compose.nginx.yaml stop gateway
docker compose -f compose.yaml -f compose.nginx.yaml pull gateway
docker compose -f compose.yaml -f compose.nginx.yaml up -d
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env status
```

A Git checkout retains `docker compose build` as its independent source-build
route. Never run the Python updater inside the Gateway container.

## Build and publish manually

From a clean reviewed checkout:

```bash
uv run --frozen python scripts/build_release.py --image ghcr.io/lujker/acp-gateway:0.1.0
uv run --frozen python scripts/smoke_installation.py dist/release/acp_gateway-0.1.0-py3-none-any.whl
uv run --frozen python scripts/smoke_installation.py dist/release/acp_gateway-0.1.0-py3-none-any.whl --git-ref HEAD --bundle dist/release/acpgw-0.1.0-bundle.tar.gz
uv run --frozen python scripts/smoke_platform.py dist/release/acp_gateway-0.1.0-py3-none-any.whl
```

To produce one bundle that can be transferred to a VPS before registry
publication, first build the selected local image, then add `--include-image`
to `build_release.py`. The archive will contain `gateway-image.tar` for
`docker load`; only the build's current platform is included.

The build creates `dist/release/`: wheel, sdist, deployment bundle and SHA256SUMS.
`release.json` records version, source commit, dirty state and intended image.
The build does not publish anything and refuses a dirty checkout. Development
previews require `--allow-dirty` and are marked in `release.json`.
The smoke installs into temporary uv directories, tests a second fixture version,
failure and active-runtime refusal, and preserves data through removal. Its
optional Git path clones the selected local commit and runs the checkout installer.
The native platform smoke additionally exercises failed SQL health recovery,
explicit rollback and the Windows installer. The manually triggered
`Platform release checks` workflow runs the wheel lifecycle and frozen CLI on
native runners. No push/PR/tag/release trigger is configured.

### First PyPI publication

The owner needs a [PyPI account](https://pypi.org/account/register/) with verified
email and two-factor authentication. Prefer
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/):
in [account Publishing settings](https://pypi.org/manage/account/publishing/), add
a pending GitHub publisher with these exact values:

| Field | Value |
| --- | --- |
| PyPI project name | `acp-gateway` |
| GitHub owner | `Lujker` |
| Repository | `acp-gateway` |
| Workflow filename | `linux-binary.yml` |
| Environment | `pypi` |

The GitHub environment `pypi` is configured in the repository. No PyPI password
or permanent token is needed. A pending publisher creates the project on its
first successful upload; it does not reserve the package name beforehand.

The manual `Platform release checks` workflow accepts `publish_pypi=true` and
`release_tag=v0.1.0`. It downloads the exact wheel and sdist from the existing
public GitHub Release, verifies both against `SHA256SUMS`, and never rebuilds
them. `pypi_dry_run=true` is the default: files are checked without uploading.
After configuring the publisher, explicitly select `pypi_dry_run=false` to
publish. Only the publication job gets OIDC permission. This mode skips the
platform build matrix and Docker publication, which already ran for this release.
Use a workflow ref containing these inputs (`feat/vps-acceptance` until merged).

After upload, verify the project's owner and repository URL and install into a
fresh uv tool environment. Published versions cannot be replaced.

As an alternative, publish the exact reviewed files locally with `UV_PUBLISH_TOKEN`
supplied by a secret manager or hidden prompt outside chat and the repository:

```bash
uv publish dist/release/acp_gateway-0.1.0-py3-none-any.whl dist/release/acp_gateway-0.1.0.tar.gz
```

Authenticate separately to your container registry, then build, test and push:

```bash
docker build -t ghcr.io/lujker/acp-gateway:0.1.0 .
uv run --frozen python scripts/smoke_docker.py --image ghcr.io/lujker/acp-gateway:0.1.0
docker push ghcr.io/lujker/acp-gateway:0.1.0
```

The local image build targets the current architecture. Other architectures
need their own build and smoke before being advertised. Make the registry package
readable by intended users. Create a matching GitHub Release/tag manually and
attach the bundle, wheel, sdist and SHA256SUMS. Published version numbers are
immutable: increment the version for subsequent releases. No new automatic
build or publication triggers are introduced.

The manual `Platform release checks` workflow has an optional `publish_image`
input (off by default). When explicitly enabled, a separate job builds and
smoke-tests the image after the native matrix and Linux checks pass, then pushes
it using that job's short-lived GitHub token. It refuses an existing version tag.
This publishes only the current runner's Linux x86_64 image. GHCR packages can
initially be private: the owner must make the package public in GitHub package
settings before anonymous `docker compose pull` works. Check an unauthenticated
pull rather than inferring availability from a successful push.
