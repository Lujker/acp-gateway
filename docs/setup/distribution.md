# Install, update and remove ACP Gateway

Linux/WSL is the verified first target. Python package installation on native
Windows/macOS does not yet imply tested service or internal updater support.
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
uv tool upgrade acp-gateway
uv tool uninstall acp-gateway
```

Publication is a separate release step. Do not assume a PyPI package or Docker
image exists because its name appears in an example. pipx can install the same
wheel/package; with pip, use a dedicated virtual environment. Internal mutation
commands currently manage only installations owned by `uv tool` on Linux/WSL.

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

The updater hands over to uv before replacing its own environment. It preserves
user files, does not automatically start services, and does not promise an
atomic upgrade or automatic rollback. After success, verify the version and
configuration; start the services that were previously running:

```bash
acpgw version
acpgw config check
acpgw service start
acpgw service --role connector start
```

If uv reports failure, inspect `acpgw version` before restarting. Keep the previous
release bundle available for explicit reinstall. Copy private configuration and
back up SQLite while stopped before a version change. A database migrated by a
newer version may not work with an older program: restore a matching backup when
necessary. uv commands run directly outside `acpgw` bypass its runtime guard.

Remove the stopped tool and its managed units:

```bash
acpgw uninstall
```

Both units are checked before any removal. Configuration, secrets, keys, pins
and SQLite remain. There is no implicit purge. For a non-uv installation, use
`service uninstall` for its managed units and the original package manager for
the program. Native Windows/macOS self-replacement and binary auto-update are
separate milestones; ordinary package-manager installation/removal remains possible.

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
```

To produce one bundle that can be transferred to a VPS before registry
publication, first build the selected local image, then add `--include-image`
to `build_release.py`. The archive will contain `gateway-image.tar` for
`docker load`; only the build's current platform is included.

The build creates `dist/release/`: wheel, sdist, deployment bundle and SHA256SUMS.
`release.json` records version, source commit, dirty state and intended image.
The build does not publish anything. Use a clean checkout for the final release.
The smoke installs into temporary uv directories, tests a second fixture version,
failure and active-runtime refusal, and preserves data through removal. Its
optional Git path clones the selected local commit and runs the checkout installer.

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
