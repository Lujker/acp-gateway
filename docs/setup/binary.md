# Standalone Linux executable

Build on the target Linux architecture with Python 3.12 and uv:

```bash
uv sync --frozen --group build
uv run --frozen --group build python scripts/build_binary.py
uv run --frozen python scripts/smoke_binary.py dist/acpgw-linux-x86_64/acpgw
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
