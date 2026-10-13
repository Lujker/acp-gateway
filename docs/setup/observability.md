# Runtime diagnostics and logs

The owner API token is required for all health routes, including active
probes. `/health` reports daemon/database status, agent and channel snapshots,
and running job count. It does not initiate network connections.

| Route | Behavior |
|---|---|
| `/health/agents/ALIAS` | Passive agent connection snapshot |
| `/health/agents/ALIAS?check=true` | ACP connection/initialize probe, bounded to ten seconds |
| `/health/channels/NAME` | Whether the channel currently reports a reachable client/human |
| `/health/NAME` | Shortcut for an unambiguous agent or channel name |

Component routes return `200` when connected, `503` when disconnected or
unavailable, and `404` for unknown names. If an agent and channel share a
name, use the explicit route. An inactive CLI listener or idle, disconnected
agent does not mean the daemon has crashed. A probe checks transport/TLS/ACP
initialization; it creates no sessions, prompts or agent actions. It is not
an LLM/model health check.

Systemd captures stderr in its journal. To also keep bounded private JSON
files, configure:

```yaml
logging:
  level: INFO
  format: auto
  file: /absolute/path/to/gateway.log
  max_bytes: 5000000
  backup_count: 3
```

File logs always use JSON, independent of terminal format. Both handlers
redact registered credentials, sensitive fields and exception messages.
New directories use mode `700`; log files and rotated files use mode `600`
on Linux. The active file cannot be a symlink. `max_bytes` bounds each file
approximately to one record beyond the limit; `backup_count` bounds retained
backups. The systemd installer requires an absolute log path for unattended
startup. JSON log rotation is independent of SQLite history. Development
version 0.2.0rc2.dev0 adds bounded scheduled job/audit cleanup; audit expiry
is opt-in. See [retention configuration](retention.md). The frozen 0.2.0rc1
acceptance package keeps its existing behavior.

Agent connection recovery already uses bounded backoff and restores owned
sessions on demand. Authentication and certificate pin failures are not
retried. Interrupted agent prompts are not automatically replayed: command
execution may have begun before the connection failed. Systemd's
`Restart=on-failure` restarts a crashed gateway; running jobs left in SQLite
are marked interrupted at startup. Saved sessions/configuration survive.

Development version 0.2.0rc2.dev0 also adds a shared per-channel admission
budget for new sessions and prompts. HTTP 429 includes Retry-After; MCP/CLI
show the waiting time. Result polling, cancellation and human approval
remain available. See [channel work limits](rate-limits.md).
