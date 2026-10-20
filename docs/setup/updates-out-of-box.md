# Updates out of the box

[Documentation](../README.md) · [Updater operations and recovery](remote-updates.md)

Included in stable **0.2.2**. Published 0.2.1 retains the manual setup in the
companion guide. These updates replace ACP Gateway dispatcher/connector code,
not Hermes, Goose, Codex or other harness binaries.

## Normal setup and first start

Supported registry `uv tool` installations prepare a private policy, independent
helper runtime and autostart during normal setup/actual component start. The selected managed application service must already exist with matching
config/env paths; setup calls bootstrap after service installation, and an actual
first start provides the fallback. A foreground/unmanaged lifecycle is not
reported as a ready managed updater. The
selected role alone is prepared: Linux dispatcher or connector, Windows connector.
The helper runs with the base Python interpreter outside the replaced uv tool
environment. It is not a child whose lifetime follows the gateway/connector.
Existing config/env paths and enrollment/binding stay selected; bootstrap refuses
a conflicting existing policy rather than rewriting it.

Bootstrap is not called by version, status, inventory, `update --check`, history,
or other read-only commands. Preparing a helper does not install a new application
version. A package/container update still requires an explicit command or an exact
target/version Telegram confirmation. Worker readiness means a recent heartbeat
with the matching role, target and backend, not merely a successful service command.
Preparation without observed readiness is reported separately from success.

The normal commands are `acpgw update --check`, `acpgw update --stable` and
`acpgw update --version X.Y.Z`. CLI and Telegram use the same artifact-availability
function. uv availability additionally checks release-specific PyPI metadata
(repository identity, exact version, non-yanked universal wheel/matching sdist
and Python 3.12 compatibility) before service replacement. Release metadata is
bounded to 256 KiB and 100 file entries. A PyPI version without a corresponding supported Docker artifact is not
offered as an available Docker update. A registry/network error is not evidence
that an update exists or succeeded. Unsupported source/Git/file/binary installs
must use their original package manager; macOS and Windows dispatcher services are
not silently treated as supported.

### Explicit opt-out

```yaml
updates:
  enabled: false
```

An existing false value is kept. Normal bootstrap does not provision/start a
helper in this case, and new remote admissions are denied. Disabling updates does
not cancel or kill an already admitted installer/recovery. Advanced helper controls
remain available for diagnosis; they are not normal installation steps.

Linux uses a systemd **user** unit; unattended boot still depends on the host's
user manager/linger configuration, like the existing managed application service.
Windows uses the current user's Task Scheduler logon task without elevation or a
stored account password. The base Python installation must remain installed.

## Standard Linux Docker deployment

Run the bundle's ordinary preparation on the **Docker host**, with `uv`, Docker
Compose and a working systemd user manager available. Select the same Compose
overlays that will be used for the gateway:

```sh
python3 deploy/docker/prepare.py --compose-file compose.proxy.yaml
```

Paths above are relative to the bundle root. With a source checkout, preparation
uses `uv run --project ... --frozen`; a release bundle uses its exact stable package
version in an isolated `uv tool run` environment. Preparation now includes the
host companion; no hand-written JSON or `updater install/enable` is necessary.
`--no-updater` explicitly skips host preparation. A configured `updates.enabled:
false` also prevents host bootstrap. Missing prerequisites fail explicitly.

The standard layout mounts `runtime` at `/config` read-only and `state` at `/data`.
Only `state/update-spool` carries requests/results. Private host policy, helpers,
image override and backups live in `private-updater`, **outside both mounts**. The
gateway receives neither a Docker socket nor Docker-host command/path authority.
A custom data/mailbox mount is rejected by automatic bootstrap; keep an explicit
reviewed advanced policy for a nonstandard deployment.

Before the gateway is started, host autostart can be prepared but its heartbeat is
not yet ready. The bootstrap command distinguishes this pending state (exit 2)
from ready (exit 0) and failure (exit 1). Start the selected Compose gateway, then
inspect updater readiness. The systemd helper retries a failed initial startup.
Use the same overlays on subsequent deployment preparation; conflicting existing
policy is not silently retargeted.

### Artifact delivery

The host worker resolves only exact stable `major.minor.patch` versions in
`ghcr.io/lujker/acp-gateway`. It obtains public metadata over fixed HTTPS endpoints;
redirects, arbitrary registries/URLs/tags and malformed or oversized manifests are
rejected. The manifest body is verified against its SHA-256 digest, and an index
must contain a single matching Linux platform descriptor whose child manifest is
also verified. Legacy single-manifest publication is supported on Linux x86_64;
other architectures require a matching platform index.

Metadata is bounded to 64 KiB, indexes/layer lists to 128 entries, individual layers
to 512 MiB, and declared compressed image size to 2 GiB. Metadata timeouts are
10 seconds per request; the exact digest pull has a 300-second subprocess bound.
The worker pulls and replaces by the resolved immutable digest, never by a mutable
tag supplied through a management request. New published stable versions need no
per-release `allow-image`. Explicit advanced local image mappings remain supported.

The selected image remains in the private Compose override and the deployment's
`ACPGW_IMAGE` pointer. Other `.env` bytes/values are preserved; subsequent ordinary
Compose recreates read this pointer rather than reverting to the bundle default.
Existing volumes, networks, config/env mounts and enrollment are reused.

## Results, busy refusal and helper refresh

The existing P4.15 durable journal, claim-before-execute, idle reservation,
backup/health/recovery and unknown fences remain the execution mechanism. Busy
admission is a saved refusal, not a delayed update: finish the work and submit a
**new explicit command**. A second check at actual execution protects races.
Read-only status/reconnect never repeats an installer, and an unknown outcome
blocks another operation until its original result is reconciled or locally resolved.

Before confirmation, Telegram warns that restart can lose the active attachment
or dialogue continuation. Cold resume is not promised. Results remain in the
separate control/mailbox journals, outside rollback of `gateway.db`.

An updated package stages its own helper sources in a private adjacent directory,
without overwriting a running helper. After saving a successful operation, the
worker closes its journal/heartbeat, releases its lease, validates every staged
file digest, swaps the fixed helper files under the lease and re-execs the base
interpreter. uv refresh uses the newly installed entry point; Docker host refresh
obtains the exact same stable package through the locally selected uv executable.
Failure to refresh never replays the completed application update or invents a
successful helper refresh. Inspect local readiness if that warning is reported.

The automated source evidence uses fake service/Docker commands, real temporary
SQLite, and safe real subprocess helper export/worker tests. No release, production
service change, real image pull, native Windows upgrade or reboot acceptance is
claimed. Live acceptance still needs native Linux/Windows uv upgrades, Docker
engine replacement/recovery, Telegram round trips, and reboot/logon persistence.

## Primary interface references

Implementation follows the official Registry HTTP API v2 manifest endpoint and
Docker digest pull contract, public GHCR anonymous-image access, and uv isolated
tool execution. See the upstream references when changing transport semantics:

- PyPI release metadata: `https://docs.pypi.org/api/json/`
- Registry API: `https://docs.docker.com/reference/api/registry/latest/operations/GetImageManifest/`
- Registry content digest verification: `https://distribution.github.io/distribution/spec/api/`
- GHCR access: `https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry`
- Immutable pull: `https://docs.docker.com/reference/cli/docker/image/pull/`
- uv tools: `https://docs.astral.sh/uv/concepts/tools/`
