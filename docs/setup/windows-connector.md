# Native Windows connector logon task (development)

P4.8.4 includes a development Task Scheduler adapter behind `service --role connector`.
On 2026-10-12, native Windows 10 build 19045 / CPython 3.12.13 acceptance passed:
8 disposable Scheduler/ACL/ownership/locking/maintenance cases and 25 unit cases.
Hermes 0.21.6 observer and dedicated conversation smoke checks passed without inference.
On 2026-10-13 the final installed-wheel acceptance passed: 221 tests, 3 POSIX-only
skips, all 13 Scheduler cases executed. Discovery ACL, graceful stop and force/
instance fencing passed; Hermes Desktop stayed ready on the same endpoint.
Reboot/logon, physical sleep/wake/network changes and a full package update
lifecycle remain open. This remains a development path.
See the [Russian handoff](../items/windows-acceptance-handoff.ru.md) and
[development journal](../../road-notes.md) for commands and evidence.

Use a separate native Windows Python environment; `uv sync --frozen --group dev
--python 3.12 --managed-python` avoids accidentally reusing a Linux `.venv` when
UV_PROJECT_ENVIRONMENT is set to an absolute Windows directory. The child Windows
PowerShell process uses its own built-in modules to avoid inherited PS7 conflicts.

Run Gateway in native Windows, as the same user as the existing Hermes Desktop.
An installation inside WSL manages WSL/systemd; it cannot install this native
task or attach to native Hermes through WSL loopback. See [Hermes attachment](hermes-attach.md).

## Prepare persistent files

Use a native Windows Gateway Python/uv environment containing this change.
Prepare a connector config with local agent profiles, an environment file
(empty is valid when no local agent secrets are needed), and the computer key
exported by the VPS. Persist all required credentials in the selected environment
file; shell-only secrets are refused.

The current interactive connector wizard defaults `data_dir` to `data/` and
`logging.file` to `logs/connector.log` beside its absolute config path. After
save confirmation it prepares missing private directories. Existing custom
absolute paths are preserved; relative paths are rejected. For manual configs,
prepare these paths yourself. For example, adapting the user name and paths:

```yaml
data_dir: 'C:\Users\you\AppData\Local\acpgw-connector\data'
logging:
  file: 'C:\Users\you\AppData\Local\acpgw-connector\connector.log'
```

On repeat setup, an existing config with implicit platform `data_dir` keeps that
location rather than moving its data beside the config. New defaults apply to
fresh configurations. Setup does not migrate existing data or rotate credentials.

Protect config, environment and key files with Windows ACLs. Only the current
user, SYSTEM and Administrators may have Allow entries. OWNER RIGHTS entries are
also accepted after validating the object's trusted owner. Their immediate parent
directories and the log directory must not grant other identities write/delete
or permission-changing access. Existing log files must also be private. Reparse
points at checked paths are refused. Installation only inspects ACLs; it does
not change permissions or copy credentials automatically. The wizard validates
existing config/env/state and credential ACLs before reading them, and checks
temporary file ACLs before writing secret bytes. It creates missing own
directories privately and rejects public read access on its runtime/config
directories, including inheritance that could expose future files. It does not
repair existing ACLs or change external Hermes files. Do not relax the check to
make installation pass; prepare a private location or correct its ACLs explicitly.

Service installation preflight covers connector files. A separate discovery
preflight now checks Hermes rendezvous records/tokens and their directory before
reading credentials. Its native acceptance passed within P4.8.4; see
[Hermes attachment](hermes-attach.md). The graceful task stop passed the repeat
native run, including explicit force followed by a fresh instance.

## Install and control

Interactive `setup --role connector` now offers **Install and start the Windows
connector logon task?** before the save confirmation. Choosing yes saves the
validated configuration, installs a disabled task, then enables logon startup
and requests its first start. No additional URL/key input is needed. Declining
leaves task registration to the CLI below; an existing disabled definition is
not refreshed until `service --role connector install` is run.

Before editing an existing connector, disable its task. The wizard refuses an
enabled, queued or running task. Its task snapshot also checks this installation
and the selected config/env paths. Cancel before Save changes neither files nor
tasks. Successful scheduling does not prove a VPS connection: check `computers
status` on the VPS and local logs.

If registration or startup raises an error, the wizard gracefully quiesces only
the task created by that setup attempt, restores the previous disabled task XML
(or removes the new task), then restores config/env/state. It never forces a
kill or settles an external agent approval. Task/control-ID or file changes
during setup stop automatic recovery; the error directs you to service status
and private setup backups. Empty directories, diagnostic logs and backups can
remain after a failed attempt. Keep them until recovery is understood.

After `acpgw setup --role connector` or `acpgw connect` has saved your connection,
service installation can reuse it without repeating URL/computer ID/key options:

```powershell
acpgw --config C:\private\config.yaml --env-file C:\private\.env service --role connector install
```

This reads `setup.json` beside the selected config and applies the same launch
validation as foreground startup. Configure absolute persistent data/log paths
and private ACLs as described above first. If any launch option is supplied
explicitly, provide the complete URL/computer ID/key set; it is never merged
with another saved server's key or TLS pin. The task stores validated launch
arguments, so later edits require reinstalling its definition while stopped.
Missing/invalid saved state gives an error before task registration.

From native PowerShell, with your own absolute paths and dispatcher URL:

```powershell
acpgw --config C:\private\computer.yaml --env-file C:\private\computer.env service --role connector install --dispatcher-url wss://dispatcher.example/connect --computer-id home --token-file C:\private\home.key
acpgw service --role connector enable
acpgw service --role connector status
acpgw service --role connector logs
acpgw service --role connector restart
acpgw service --role connector disable
acpgw service --role connector uninstall
```

The task is named `ACP Gateway Connector-<current-user-SID>`. Only one managed
connector task is installed per Windows user. Each other Windows PC has its own
task and enrollment identity. Multiple agent profiles live in the same connector
config. Independently running connectors need separate data directories.

| Command | Behavior |
| --- | --- |
| `install` | Validate files/ACLs and register a disabled logon task; refuse a running task |
| `enable` | Enable logon startup and start now |
| `disable` | Disable logon startup and stop the task |
| `start` / `stop` | Start/stop now; start requires an enabled task |
| `restart` | Request graceful stop, wait up to twenty seconds for exit, start again |
| `status` | JSON with installed/enabled, Scheduler state and last result; no app config load |
| `logs` | Last 50 lines from at most 64 KiB of the configured log, redacted |
| `uninstall` | Disable, stop, remove the owned task; retain config, keys, logs and data |

Scheduler state values: 0 unknown, 1 disabled, 2 queued, 3 ready, 4 running.
Running means the process is running; it does not prove that the VPS or agent is
connected. Check VPS `computers status` and a deliberate dispatcher conversation.

Task ownership requires the expected marker, current-user principal, interactive
logon type, least privilege and this installation's executable. Management
refuses an unrelated task or a different installation's executable. Status/logs
also enforce ownership. The task definition stores paths and launch options,
including the dispatcher URL and TLS pin, but no secret contents. Values travel
to a fixed PowerShell script as JSON stdin; no command shell is used.

The task uses an interactive user token and a logon trigger delayed ten seconds.
It stores no Windows password and does not promise operation before user login.
The Scheduler ignores duplicate triggers. The connector also holds an OS file
lock in its data directory across reconnects, including manual versus task starts.
Network failures retry inside the process. Scheduler crash recovery is bounded
to three retries a minute apart; unlike systemd it cannot filter retries by
application exit code, so configuration/authentication failures may also get
these bounded retries. Inspect logs and fix the cause before enabling again.

Windows `stop` requests graceful shutdown through local control files in the
configured data directory. The connector publishes a fresh instance ID, closes
its relay streams and Hermes attachments, removes its runtime record and exits
with code 0. A stop request from a previous process cannot stop its replacement.
The task's separate control ID prevents stopping a foreground connector with
the same data directory. These IDs contain no credentials; directory write ACLs
control access. Install now creates/checks the data directory as well.

The task waits up to twenty seconds. A timeout reports an error and leaves the
process running; it never silently converts a graceful stop into a kill.
For a stuck process, explicitly use `stop --force` (also supported by disable,
restart and uninstall). This invokes the Scheduler's immediate task stop:

```powershell
acpgw service --role connector stop --force
```

Older installed tasks lack the control ID: stop them once with `--force`, then
reinstall to enable graceful shutdown. Neither graceful nor forced stop waits
for an external model turn to finish. An interrupted model result may be uncertain
and is not replayed; shutdown never settles an outstanding Hermes approval.
The adapter never starts, stops or deletes the external Hermes Desktop process
or its conversations. See Microsoft's [interactive logon task example](https://learn.microsoft.com/en-us/windows/win32/taskschd/registration-trigger-example--xml-)
and [task stop contract](https://learn.microsoft.com/en-us/windows/win32/taskschd/registeredtask-stop).

Disable the task before changing settings or updating/uninstalling Gateway:

The current Hermes connector also supports local
[maintenance status/prepare/resume](maintenance.md#local-connector-reservation).
Use `maintenance --role connector prepare` with the connector config to reserve
an idle instance before a planned stop. Pending or uncertain work refuses the
reservation. This does not disable the task or replace the package. The source
implementation now supports `update --with-services` for the owned connector task:
it reserves the connector, stops it gracefully, waits for the external package
helper and restores the previous enabled/running state. See
[managed Windows update](distribution.md#windows-connector-update-with-task-restoration-development).
A queued result is not completion; use `update --status`. Full installed uv
package/service acceptance remains open.

```powershell
acpgw service --role connector disable
```

The wizard and internal package maintenance refuse an enabled, queued or running
task. After changing launch options or interpreter paths, reinstall from the
original installation before enabling. Manual `uv tool` operations cannot be
guarded by Gateway; disable the task first.

## Native acceptance

The source Scheduler profile now contains 23 cases, including setup transaction
restore, concurrent replacement, config mismatch and wizard rollback after a
real task start. Four additional cases verify managed-update inspection and
restoration flags for every enabled/running combination, including a disabled
but running task. The consolidated runner requires all 23 to run successfully.
The older 221-test installed-wheel result above remains historical; these new
cases require a current source environment or the next tested wheel.

For the final preview, use one consolidated native Windows run from the current
checkout with Windows uv installed:

```powershell
uv run --no-project --python 3.12 --managed-python python scripts/accept_windows_track.py
```

It verifies preview checksums, installs the tested wheel into a fresh environment,
runs the full track acceptance selection (including all 23 opt-in Scheduler
cases), and checks the existing Hermes Desktop before and after without model
calls. It also exercises the live setup selector without saving configuration.
Logs, JUnit and report.json remain in a fresh directory printed on completion.
Persistent dialogue bindings and refusal to restore/replay old Hermes handles
are covered by mock-backed tests. Real model approvals and physical lifecycle
checks remain separate. See the Russian handoff for the precise scope.

From a native Windows development checkout with Gateway and pytest installed:

```powershell
$env:ACPGW_TEST_WINDOWS_TASK = '1'
python -m pytest -q tests/integration/test_windows_connector_task.py
Remove-Item Env:\ACPGW_TEST_WINDOWS_TASK
```

This opt-in test creates a uniquely named disposable task and private temporary
files, exercises install/enable/restart/disable/logs/uninstall and cleans up.
It connects only to a closed loopback port; no model or Hermes endpoint is used.
The normal test suite skips it. The opt-in suite also covers ACL rejection, unrelated-task protection, a scheduled
versus foreground duplicate and setup/maintenance guards. Real model/approval traffic,
reboot/logon, sleep/wake, network changes and a complete package update still need
separate acceptance. A model-free live Hermes attach and continued backend access
after connector task stops were checked separately.

The updated opt-in suite also requires `stopMode=graceful` on normal restart/
disable, and checks explicit force followed by a new process ignoring the old
stop request. These cases passed the consolidated native run on 2026-10-13.
