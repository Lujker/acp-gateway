# P3.3: SQLite retention

Available in development version **0.2.0rc2.dev0** and newer. The frozen
**0.2.0rc1** self-install candidate does not accept the retention section.
Keep using its [installation guide](self-install-0.2.0rc1.ru.md) for live acceptance.

Configure the dispatcher, not the connector:

~~~yaml
retention:
  jobs_days: 7
  approval_audit_days: null
  sweep_interval_seconds: 3600
  batch_size: 1000
~~~

Finished job results expire after seven days by default. Audit decisions
remain indefinitely unless approval_audit_days is explicitly set to a
positive number, for example 90. Environment overrides use
ACPGW_RETENTION__APPROVAL_AUDIT_DAYS=90. Restart the dispatcher after
changing configuration. Removing the override and setting null disables
future audit expiry; already deleted decisions cannot be recovered.

A sweep runs at startup and every configured interval while serving.
Each sweep deletes at most batch_size jobs and batch_size settled audit
rows whose finish/decision timestamp is strictly older than its cutoff.
A backlog drains over multiple sweeps; expiry is not an exact deletion
deadline. Defaults allow up to 1000 rows per table per hour. Valid bounds:
days 1–36500, interval 60–86400 seconds, batch size 1–10000.

Running jobs, pending approvals, session bindings, active conversation
selection and model state are preserved. Deleting a finished job clears
its job reference in the audit without deleting the decision. Audit expiry
works independently of job expiry. Retention neither changes Hermes
conversations nor removes upstream history.

Successful sweeps log pruned_jobs and pruned_approvals. Failures log
"retention sweep failed; will retry on next interval"; serving continues
and the next interval retries. Cleanup is stopped before closing SQLite.
Deletion releases SQLite pages for reuse; it does not guarantee shrinking
the database file or removing copies from backups. No automatic VACUUM runs.

Migration 0007 adds timestamp indexes and does not delete rows by itself.
An older binary, including 0.2.0rc1, refuses a database migrated to a newer schema (currently 8).
Keep the live acceptance installation separate from development databases.
Outbound references expire with their jobs. Development now also supports
[channel limits](rate-limits.md) and [Telegram delivery recovery](telegram-recovery.md).
