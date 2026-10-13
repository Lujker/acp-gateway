# Docker CLI laboratory

The laboratory runs a dispatcher and two executor computers (`home`, `work`)
on an isolated Docker network. Each computer has its own private enrollment
volume, a local loopback ACP listener and an outgoing connector. It needs Docker
Compose and a development checkout with `uv`.

```bash
uv run python scripts/lab.py run
```

This builds the lab image, exercises the scenario, then removes containers,
network and private key volumes, including on a test failure. The image remains
available for reuse (`--no-build`). A run uses a random `acpgw-lab-*` Compose
project. No host ports, user config directories, provider keys or Docker socket
are mounted into containers. Runtime network access stays inside the lab. Each
node is limited to one CPU, 512 MiB memory and 128 processes. This development
image runs as root inside its disposable containers and is not a production image.

The scenario exercises:

- The actual installed CLI through a PTY, including hidden secret input and
  setup of routed server profiles/enrollment.
- Local executor onboarding with managed Goose profile preparation, followed
  by external dispatcher connection directly from the same wizard.
- `agents provider` and `agents serve`, using a clearly labelled lab-only Goose
  CLI stand-in backed by recorded Goose ACP traffic.
- `config check`, offline/live JSON diagnostics, targeted profile editing and
  retention of credentials on repeated setup.
- Wrong ACP credentials and a missing enrollment key: diagnostics fail without
  exposing credentials, then the lab restores its files.
- Actual connector registration, routed prompts, session isolation, denial
  without a human, disconnect/reconnect and continued sibling availability.

No real model is called and no Telegram message is sent. The stand-in validates
the CLI/process/environment/protocol integration; it does not prove an upstream
Goose binary installs on a target OS, that provider authorization works, or that
native systemd/Windows/macOS services start. Installer/service behavior also has
unit coverage, including PowerShell dispatch, ownership checks and maintenance
guards. Validate real Goose/provider and host service managers separately.

The lab uses WS inside its private network. Dispatcher WSS pin/CA behavior has
diagnostic tests; the existing [Docker smoke](../../scripts/smoke_docker.py)
separately covers nginx/WSS and the L4 mirror. Neither replaces live VPS/DNS/
firewall/Telegram acceptance.

For manual exploration:

```bash
uv run python scripts/lab.py up
uv run python scripts/lab.py test
docker compose -p acpgw-lab-cli -f deploy/lab/compose.yaml exec home \
  acpgw --config /lab/config/config.yaml --env-file /lab/config/.env menu
uv run python scripts/lab.py down
```

The `up` command prints the exact exploration and cleanup commands. With a
custom name, pass the same `--project acpgw-lab-mytest` to up/test/down. `down`
deletes that lab's private key volumes. Use the required prefix to avoid
accidentally targeting a production Compose project.

If WSL's Docker CLI reports an executable-format error for
`docker-credential-desktop.exe`, public image testing can bypass the helper with
`--anonymous-docker`. It uses a temporary Docker config and leaves your existing
Docker configuration and credentials untouched:

```bash
uv run python scripts/lab.py run --anonymous-docker
```
