# Telegram delivery and status recovery

Implemented in development version **0.2.0rc2.dev0** (SQLite schema 8).
The frozen **0.2.0rc1** installation candidate keeps its original in-memory
reply queue and working/final messages.

Plain-text bot submissions atomically create a job and an outbound reference
in SQLite. Completion, including startup interruption of a previous running
job, makes that reference ready in the same SQLite statement. A worker
checks ready rows at startup and once per second while idle; bus notifications
only reduce latency. A lost event therefore does not lose the saved delivery.

| State | Meaning |
|---|---|
| waiting | Job has not finished |
| pending | Next chunk has not been sent, or was explicitly rejected with 429 |
| sending | A chunk has been reserved before the network call |
| delivered | All chunks have acknowledged sends |
| unknown | Remote delivery cannot be proved; automatic retry is fenced |
| failed | Definite rejection or recipient no longer authorized |

Only the original bot ID and an allowlisted, previously contacted private
chat can receive a saved delivery. The queue stores a job reference, an
acknowledged chunk cursor and a rendered-content digest, rather than an
additional copy of the answer. Chunks remain bounded to 2000 codepoints
and are redacted before splitting and HTML escaping.

After each accepted send, the cursor advances. A normal restart resumes
pending chunks without repeating the acknowledged prefix. A vanished sender,
ambiguous network failure, failed local ACK commit, or changed rendered
content becomes unknown. Telegram has no send idempotency key, so this
does not claim exactly-once delivery. Explicit 429 rejection permits a
delayed retry; bad requests/forbidden recipients fail. Other failures are
treated conservatively as unknown. A rollback/restored database marks
unfinished delivery records unknown because an older cursor can predate
a remotely accepted send.

Use /delivery JOB_ID to inspect automatic delivery state and acknowledged
chunk count. Use /result JOB_ID to explicitly request the whole saved answer,
including when automatic delivery is unknown or failed. Both commands require
the job to belong to the selected agent and this private chat. /result is an
explicit new retrieval and may repeat parts the user already received. Its
32-entry in-memory queue remains separate and does not survive restart.

Queue history expires with finished job retention (seven days by default);
deleting a job also deletes its delivery reference. Pending sends can expire.
Programmatic core callers must supply delivery_namespace to request durable
delivery; legacy submissions without it keep the event-based reply behavior.
The gateway never replays an agent prompt or restores pending approvals.

## Status edits

~~~yaml
telegram:
  status_enabled: true
  status_interval_seconds: 2
~~~

The working message is edited with up to 1500 redacted characters of the
answer received so far, then completed/cancelled/failed/interrupted status.
Tool arguments and reasoning are excluded. The edit worker is separate
from answer delivery, polling and approval processing. The interval is
1–60 seconds; unchanged text is not edited. Explicit 429 defers another
edit of the same message; other edit failures drop that advisory status
and leave durable answer delivery intact.

At most 32 active status messages are tracked. Status tracking is in memory;
old working messages are not rediscovered after restart. Saved job results
and the outbound queue remain available. Disable status_enabled to retain
working/final behavior without partial answer edits.

## Development acceptance

Run the source suites and native Windows tests against temporary databases.
The extended installed-wheel lifecycle smoke is:

~~~bash
uv run python scripts/smoke_installation.py /path/to/development.whl --verify-delivery-state
~~~

It isolates tool/config/data directories and tests real update, explicit
update --rollback, failed update, selected SQLite state/integrity, enrollment
keys, tokens, daemon/API/ACP/MCP and uninstall. Offline execution requires
pointing UV_PYTHON_INSTALL_DIR and UV_CACHE_DIR at existing local caches,
because the smoke uses a private HOME. The additional fault tests kill an
installer subprocess after partial filesystem/WAL mutation, preserve recovery
files if restore fails, and remove a database first created by a failed
release's health check. They do not prove recovery after killing the helper
itself or power loss. Real systemd/Task Scheduler update/reboot and live
Telegram acceptance remain separate checks.
