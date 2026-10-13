# Dispatcher admission during planned maintenance (development)

The current source provides an owner-only reservation that pauses new dispatcher
turns and session creation across Telegram, CLI and the owner API. This is the
first part of managed service updates. Linux/systemd now has an opt-in
[`update --with-services` coordinator](distribution.md#linux-systemd-update-with-service-restoration-development).
Plain `update` continues to require stopped processes. Current sources also
provide a [Windows task coordinator](distribution.md#windows-connector-update-with-task-restoration-development);
fresh installed-package Windows acceptance remains open.

Use the same global `--config` and `--env-file` as the running dispatcher:

```bash
acpgw maintenance status
acpgw maintenance prepare
# Perform the planned operation, or resume this same running process:
acpgw maintenance resume
```

`prepare` succeeds only when there are no running jobs, pending approvals or
session creations in the dispatcher core. Otherwise it refuses immediately and
leaves admission open. Finish the current work and retry. It does not cancel
turns, settle approvals, change the active dialogue or replay requests.

Preparation and admission checks run on the same core event loop. A request
waiting for a conversation lock checks admission again after acquiring it.
An in-flight backend session creation is counted until it returns, fails or is
cancelled. New messages and `/new` receive an explicit maintenance error while
reserved. History, results, status and explicit session selection remain available.

The owner API exposes `GET /maintenance`, `POST /maintenance/prepare` and
`POST /maintenance/resume`. All require the owner credential and a loopback Host;
MCP/agent credentials cannot reserve or resume. Prepare returns an opaque token.
Resume requires that token; another prepare or an incorrect/stale token returns
409. The status and `/health` maintenance fields never include the token.

The CLI saves it in a private `maintenance.json` in the selected data directory.
Use `--reservation-file /absolute/path/private.json` on both prepare and resume
for another location. Existing files are not overwritten. A failed resume keeps
the token for retry. If writing the token fails after preparation, the CLI tries
to release only its own reservation. CLI output never prints the token.

Reservation lasts for the current dispatcher process. A restart opens admission
in the new process; a previous token file then refers to the old process. A lost
HTTP response can also leave the running dispatcher reserved without a saved
token. After checking maintenance status, the authenticated owner can recover:

```bash
acpgw maintenance status
acpgw maintenance resume --force
```

This explicitly clears the current admission reservation and removes the selected
private token file. Coordinate it with anyone performing maintenance. Here force
applies to admission; it never kills a process or decides an agent permission.
Malformed/private files remain subject to path/ACL checks. Other configs/data
roots can have separate token files; always select the actual dispatcher config.

## Local connector reservation

On a computer running the current connector, select its actual config/data paths:

```bash
acpgw maintenance --role connector status
acpgw maintenance --role connector prepare
acpgw maintenance --role connector resume
```

This uses private files in the connector data directory, without a dispatcher
owner API credential. The same commands work in native Windows PowerShell.
The reservation file defaults to `connector-maintenance.json`; a custom
`--reservation-file` must be used consistently. Lost-token recovery uses
`maintenance --role connector resume --force` for the live connector instance.

Status reports pending RPCs, approvals, running/uncertain sessions, attachment
initialization and unsupported agent profiles. It checks the process lock,
instance identity and a fresh activity heartbeat. Missing, stale or mismatched
records yield `unknown`; an unlocked connector yields `stopped`. `idle_observed`
is a diagnostic snapshot. Only an acknowledged `prepare` reserves admission.

Prepare runs on the connector event loop, checks all Hermes managers together,
then blocks new local streams and new/load/prompt requests on existing streams.
Resume reopens them without replacing the retained chats. A pending prompt or
approval blocks prepare. Losing an attachment with owned conversations latches
uncertainty even after old request/session maps are cleared; a quarantined chat
also remains blocking after a late completion. Resume --force only clears the
admission token, not that uncertainty. Manual service stop retains its existing
explicit behavior; maintenance does not silently escalate to it.

Local requests/replies carry the current instance and unique request IDs, and
requests expire. A previous process's request cannot reserve its replacement.
CLI callers serialize writes; private atomic persistence keeps tokens out of
status and terminal output. A timeout can leave a reservation without its token;
inspect status and use explicit recovery. A restart resets admission and leaves
any old CLI reservation file to be handled explicitly, as for the dispatcher.

This first implementation supports Hermes profiles. Other profiles report
unsupported and prevent prepare, rather than claiming their remote engines are
idle. The dispatcher and connector reservations are independent. Their wiring
into updater stop/disable/restore is available for a selected Linux/systemd unit;
Windows connector tasks now have a corresponding managed cycle. The core is currently
single-owner; multi-account scopes are P4.10.

Installation and acceptance scenarios are listed separately in
[P4.8.5](../items/p48-delivery-plan.ru.md#отдельный-этап-установка-и-приёмка-p485).
Live VPS/Telegram/Hermes acceptance and service update/rollback remain open.
