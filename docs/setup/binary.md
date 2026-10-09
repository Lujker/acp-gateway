# Standalone executable

Local builds are the default and do not use GitHub Actions or its quotas.
The optional `Platform release checks` workflow runs only when you explicitly choose
**Actions → Platform release checks → Run workflow**. Pushes, pull requests, tags and
release creation do not start a build.

Build on the target OS and architecture with Python 3.12 and uv:

```bash
uv sync --frozen --group build
uv run --frozen --group build python scripts/build_binary.py
uv run --frozen python scripts/smoke_binary.py dist/acpgw-linux-x86_64/acpgw
# With a working Linux Docker engine, repeat the full smoke without host Python:
uv run --frozen python scripts/smoke_binary.py dist/acpgw-linux-x86_64/acpgw --container-image ubuntu:22.04
```

The output is a single `acpgw` executable with an embedded Python runtime,
application dependencies and SQL migrations. It does not contain the
checkout's config, credentials, pins or database. `dist/` also contains a
versioned `.tar.gz`, SHA-256 checksum and build metadata (Python, architecture,
libc and lockfile hash). Rebuilds use the locked dependency versions; byte-for-byte
reproducibility is not promised. The recipe uses [PyInstaller one-file mode](https://pyinstaller.org/en/stable/spec-files.html).

Download or copy the archive and checksum for your architecture into the
same directory. Verify and install:

```bash
sha256sum -c acpgw-0.1.0-linux-x86_64.tar.gz.sha256
mkdir -p unpacked
tar -xzf acpgw-0.1.0-linux-x86_64.tar.gz -C unpacked
mkdir -p ~/.local/bin
install -m 755 unpacked/acpgw ~/.local/bin/acpgw
acpgw --help
acpgw setup
```

Add `~/.local/bin` to PATH if needed. No Python or uv is required on the
runtime host. Configure an agent, then use `config check`, `serve`, or
`service install` / `enable` as described in
[service setup](https://github.com/Lujker/acp-gateway/blob/main/docs/setup/service.md).

Stop the service before replacing its executable, then run `service install`
and `service enable` afterward. Use `service uninstall` before removing the
binary; your config, secrets, pins and database remain in the platform dirs.

A binary is specific to its OS/architecture and the build host's native-library
baseline. Build on the oldest supported Linux distribution: PyInstaller does
not bundle glibc. The CI recipe targets Ubuntu 22.04 x86_64 (glibc 2.35), but
compatibility with other distros must be checked independently. Local builds
record their own libc version in the build metadata. One-file execution
extracts libraries into a temporary directory; its filesystem must allow
loading executable code. See [PyInstaller's platform notes](https://pyinstaller.org/en/stable/usage.html).

The automated smoke uses an empty working directory, a PATH containing no
Python, private temporary config/data, and the recorded ACP mock. It checks
help/version, all command families, setup preservation, migrations, API/SSE,
MCP tools, agent prompts, human approvals, cancellation, persistence and
frozen service installation. A real Windows reboot and native Windows binary
are separate milestones; this Linux executable does not implement Windows
service control.

The container smoke mounts only the executable and private temporary test
files, uses the current user's uid/gid and Linux host networking for the mock
agent, and forwards only temporary HOME/XDG/PATH settings to the container.
The checkout and the harness' Python environment are not mounted. CI runs
this full scenario in the official Ubuntu image, which contains no Python,
when the optional workflow is started manually.

## Publish a release manually

Build and smoke-test locally with the commands above. In GitHub's Releases
page, create a release for the intended commit/tag and attach these three
files from `dist/` before publishing:

- `acpgw-<version>-linux-<architecture>.tar.gz`
- `acpgw-<version>-linux-<architecture>.tar.gz.sha256`
- `acpgw-<version>-linux-<architecture>.tar.gz.build.json`

The archive version comes from the package; ensure it matches the release
tag. This route needs no Actions run. Releases and uploads are manual;
the workflow does not publish or change releases. If you choose to run the
manual workflow instead, download its artifact ZIP, extract it, and attach
the same three files to your release. CI artifacts are temporary downloads;
release assets are the distribution route for a published version.

## Native platforms

The build recipe supports Linux, macOS and Windows on x86_64 or ARM64 when its
dependencies support that target. It builds on the current host; it does not
cross-compile. Windows output is `acpgw.exe`; other hosts produce `acpgw`.
Archive names use `linux`, `macos` or `windows`, followed by the architecture.
The manual workflow checks Linux x86_64/ARM64, macOS Intel/ARM64 and Windows
x86_64. Windows ARM64 is not covered by that matrix.

Use `scripts/smoke_platform.py --wheel-dir dist/native --built-binary` after
`uv build --wheel --out-dir dist/native` and the binary build. The native check
exercises the installed wheel's daemon, runtime locks, updating, failed migration
recovery, rollback and uninstall; the frozen binary check covers version, setup
and configuration validation. It does not claim the full Linux ACP smoke,
native service integration, OS reboot, Gatekeeper signing or Windows signing.
Native service adapters are tracked separately in P3.2/P4.6. For routine install
and update commands, prefer the [uv release bundle](distribution.md).
