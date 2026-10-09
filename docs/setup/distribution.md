# Install, update and remove ACP Gateway

Linux/WSL is the verified first target. Manual native runner checks also cover
Windows/macOS and Linux ARM; their results are recorded in the roadmap.
Native Windows/macOS service adapters remain separate milestones.
All modes keep configuration, enrollment keys, pins and SQLite outside the
installed program. Stop jobs and processes before replacing an installation.

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

After the project is published in PyPI, these registry commands become available:

```bash
uv tool install --python 3.12 acp-gateway
acpgw setup
acpgw update
acpgw uninstall
```

Publication is a separate release step. Do not assume a PyPI package or Docker
image exists because its name appears in an example. pipx can install the same
wheel/package; with pip, use a dedicated virtual environment. Internal mutation
commands manage only installations owned by `uv tool`.

## Internal commands

```bash
acpgw version
acpgw version --json
acpgw update --check
acpgw update --dry-run
acpgw uninstall --dry-run
```

`version` shows package version, manager and source type without printing private
source URLs. These commands work even if configuration is missing or invalid.
`update --check` checks this project's stable, non-yanked PyPI releases; it does
not check the latest Git commit, release wheel or binary. A missing PyPI release
is reported explicitly. This check is advisory; installation constraints and
Python/platform requirements still determine what uv can install.

Before updating, stop both services if installed and any foreground Gateway,
connector or dispatcher from this installation. First complete/cancel active
jobs; an update does not recover an interrupted tool action.

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
keep using `sh scripts/install.sh`, which exports constraints from its `uv.lock`.
After pulling, stop the installed processes and repeat that installer. An
editable `uv run acpgw` environment is not mistaken for an installed uv tool.

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
email and two-factor authentication. Create an API token in PyPI account settings
outside chat. For the first publication the project does not yet exist, so a
project-scoped token is not yet available. After publishing, revoke the initial
token and use a token scoped to `acp-gateway` for later releases. Never commit a
token, paste it in an issue, or include it in shell history.

Set `UV_PUBLISH_TOKEN` through your secret manager or a hidden terminal prompt,
then run the command below against the reviewed artifacts. Verify the resulting
project's owner and repository URL and install into a fresh uv tool environment.
Creating an account alone does not publish or reserve this package name.

After reviewing and testing the artifacts, publish the exact wheel/sdist using
PyPI credentials configured outside the repository:

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
