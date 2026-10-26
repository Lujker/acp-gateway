# Windows connector from an editable Git checkout

This development installation uses a normal Python 3.12+ installation and a
checkout-local venv. The connector task runs `.venv\Scripts\pythonw.exe` directly;
neither startup nor service control requires `uv run` or `uv tool`.

The older `scripts/install.ps1`, `run.ps1` and `update.ps1` manage a **uv tool**
installation built from a checkout. Use `run-source.ps1` for this editable route.

## Prepare the checkout

From PowerShell, choose the reviewed branch and an ordinary installed Python:

```powershell
git clone --branch feature/p4.13-codex-managed https://github.com/Lujker/acp-gateway.git
cd acp-gateway
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\scripts\run-source.ps1 version --json
```

To install exact locked dependencies, first install `requirements.lock.txt`
exported from the reviewed `uv.lock` with
`python -m pip install --require-hashes -r requirements.lock.txt`, then install
the checkout with `python -m pip install --no-deps -e .`. The lock can be exported
on another development machine; uv is not a runtime requirement.

## Move an existing connector

Keep the existing absolute config, env, credential and data paths. Back up these
private files and the owned task definition, verify there is no active work, and
reserve dispatcher admission during the switch. If live connector activity is
unavailable, inspect actual dispatcher streams and jobs; unavailable status is
not proof of idle.

Stop/disable the connector cooperatively using its **old** interpreter. Disable
its owned updater companion: editable installations use Git/pip maintenance and
cannot receive registry updates. The companion can remain disabled for rollback.
Do not modify another installation's tasks.

Service ownership checks refuse to replace another interpreter's task. Uninstall
the stopped old connector task using the old CLI, then install and enable the new
task with the checkout CLI:

```powershell
.\scripts\run-source.ps1 --config C:\path\config.yaml --env-file C:\path\.env config check
.\scripts\run-source.ps1 --config C:\path\config.yaml --env-file C:\path\.env service --role connector install
.\scripts\run-source.ps1 service --role connector enable
.\scripts\run-source.ps1 service --role connector status
```

Installation reuses the connection saved in `setup.json`. Otherwise pass the
original dispatcher URL, computer ID, credential path and TLS pin explicitly.
Removing only the task preserves enrollment, config, history and data. Verify
the task uses this checkout's Python, activity reports idle and the same computer
reconnects. Release dispatcher admission after verification.

Retain the old package until the new service is verified. Rollback removes the
stopped new task with its CLI and reinstalls the old task with the original
config/env/data paths. Reenable its updater only when returning to that package.
Restore the original config if it now contains kinds unsupported by the old
package. Dispatcher downgrade also requires a compatible pre-upgrade database.

## Update and test

Stop before pulling, to avoid mixing imported code revisions:

```powershell
.\scripts\run-source.ps1 service --role connector disable
git pull --ff-only
# Refresh package metadata; install changed dependencies separately if needed.
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\scripts\run-source.ps1 --config C:\path\config.yaml --env-file C:\path\.env config check
.\scripts\run-source.ps1 service --role connector enable
```

Python imports use the Git sources directly. Tests and development tools may be
installed in this venv or a separate test venv. This connector stays on the Git
branch; other machines can continue using releases.

Development builds after 0.2.3rc1 also support opt-in
[profile management through Telegram](telegram-agent-profiles.md).
