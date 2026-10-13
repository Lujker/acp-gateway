# Channel work admission limits

Development version **0.2.0rc2.dev0** and newer supports a shared core budget
for prompt submissions and explicit session creation. The frozen
**0.2.0rc1** acceptance candidate does not accept this configuration section.

~~~yaml
rate_limits:
  work_requests: 60
  window_seconds: 60
~~~

The dispatcher allows at most work_requests admitted attempts per channel
within a rolling window_seconds interval. Bounds are 1–10000 attempts and
1–3600 seconds. Environment overrides follow the standard nested format,
for example ACPGW_RATE_LIMITS__WORK_REQUESTS=20. Restart the dispatcher after
changing configuration.

CLI and the owner API use the cli budget; MCP uses mcp; Telegram uses
telegram. A budget is shared across every thread, configured agent and
allowlisted user in that channel. Changing the thread or agent does not
reset it. Separate channels have independent budgets. Telegram's existing
per-user message throttle remains active in addition to this core limit.
This is the current single-owner policy; account-specific quotas belong
to P4.10.

Each explicit new-session or submit attempt that reaches work admission
uses one slot. Implicit session creation during a prompt uses the same
slot. Unknown agents and policy-invalid prompts are rejected before
charging. Later failures, including a busy session or failed agent
connection, still consume the admitted slot. Rejected over-limit attempts
consume no slot and do not create a job or session.

The owner API returns HTTP 429 with an integer Retry-After header.
The CLI shows the error and waiting time. MCP returns isError with a safe
message containing the waiting time. Telegram shows the same core error.
The caller must decide whether to try again; the gateway never queues or
automatically resubmits the rejected prompt.

Result polling, cancellation, approval listing/decisions, health, model
control and local session selection do not consume this work budget.
They remain accessible when new work is throttled. This budget governs
new work, rather than every HTTP request or concurrent active job.

Accounting uses monotonic time and is process-local. Restart resets the
window; it is not a persistent billing quota. Each channel retains at most
work_requests timestamps; user-controlled thread names are not limiter
keys. Core adapters supply the trusted channel name.
