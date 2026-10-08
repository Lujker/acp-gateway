# Gateway daemon and owner CLI

Configure an agent as described in [Work Goose setup](work-goose.md). Put
the agent secret and a separate `ACPGW_API_TOKEN` in `.env`; secrets never
belong in `config.yaml`. Generate an owner token with:

```bash
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Copy the output into `.env` as `ACPGW_API_TOKEN=<value>` and keep the file
private (`chmod 600 .env` on Linux/WSL). The owner token must differ from all
configured agent credentials and the MCP token. Hermes will use its own MCP
token for [the MCP channel](hermes.md). Setting `ACPGW_MCP_TOKEN` enables
`/mcp`; leaving it unset keeps a CLI-only daemon.

Run `uv run acpgw config check`, then `uv run acpgw serve`. The daemon runs
in the foreground and listens on the configured loopback host and port
(default `127.0.0.1:8765`). One daemon owns a data directory; a file lock
prevents a second process from opening it and interrupting the first
process's jobs. Ctrl+C shuts down the core, cancels outstanding requests,
closes agent connections and releases the lock. Jobs interrupted by a crash
are settled when the next daemon starts.

## Sessions and jobs

The CLI connects to the daemon using the owner token. Specify global config
options before the command: `acpgw --config /path/config.yaml --env-file
/path/.env status`. Agent selection is optional with one configured agent;
otherwise use `--agent work`. `--thread` names a conversation (default
`default`). CLI conversations are separate from MCP and other channels.

```bash
uv run acpgw status
uv run acpgw ask --agent work --thread research "Remember code word AMBER."
uv run acpgw ask --agent work --thread research "What was the code word?"
uv run acpgw sessions --agent work --thread research
uv run acpgw new --agent work --thread research --cwd /home/user/work
uv run acpgw switch --agent work --thread research 1
uv run acpgw ask --agent work --thread research --session 1 "Continue this session."
```

Session ids in CLI commands and API paths are **integer gateway row ids**,
shown by `sessions` and `new`. They differ from the ACP session id. A
targeted `ask --session` leaves the active session pointer unchanged and
checks that the selected conversation owns the session. Sessions restore
their original working directory when reconnecting to the agent.

`status` reports current connections; it does not probe the remote agent.
Agent connections open lazily on the first session operation. `ask` prints
message chunks and reconciles them with the final job answer. It does not
print thought chunks or tool events. `--json` prints one final job object;
`ask -` reads the prompt from stdin.

```bash
uv run acpgw ask --no-stream "A long task"     # returns job JSON immediately
uv run acpgw result <job_id> --wait 30 --json
uv run acpgw stop                            # cancel jobs in the selected conversation
```

Disconnecting an SSE reader leaves the job running. Retrieve it with
`result` after reconnecting. Ctrl+C during `ask` requests cancellation;
Ctrl+C during `approvals watch` disconnects the human channel. Failed,
interrupted or cancelled jobs return CLI exit code 1. An automatic approval
rejection also returns 1 and explains the reason in the job's `error`.

## Human approvals

Keep `uv run acpgw approvals watch` open in another terminal before
requesting an action. It displays the tool title and arguments, then asks
for allow once (`a`), reject (`r` or Enter), or skip (`s`). Skipping leaves
the request pending until another decision, cancellation or its deadline.

`acpgw approvals` lists pending requests without connecting a human.
`acpgw approvals approve <id>` and `reject <id>` use a short owner connection
to decide a request already pending, for example while another watcher is
open. The CLI exposes only once decisions. With no connected approver, the
core rejects a new request immediately. Running the daemon is insufficient.

The server issues a random lease to each authenticated watcher and revokes
it when that stream disconnects. Decisions require a live lease and are
audited as `local-api-owner`; clients cannot supply an actor identity.
Multiple watchers are supported, and the first valid decision wins. The
policy still controls approver eligibility, options and deadlines.

## Owner HTTP API

Every endpoint requires `Authorization: Bearer <ACPGW_API_TOKEN>` and a
loopback `Host` header. API responses are not cached. The API does not
accept agent or MCP credentials. Interactive docs are disabled.

| Method and path | Behavior |
|---|---|
| `GET /health` | Gateway, database schema, agents, channels, running job count |
| `GET /sessions?agent=work&thread=default` | Conversation sessions and active row id |
| `POST /sessions` | Create and activate; body: `agent`, `thread`, optional `cwd` |
| `POST /sessions/{id}/activate` | Switch active session; body: `agent`, `thread` |
| `POST /messages` | Submit to the active session; body: `agent`, `thread`, `text`, optional `wait` |
| `POST /sessions/{id}/messages` | Submit to an owned session without switching it |
| `GET /jobs/{job_id}?wait=30` | Current/final job, optionally wait up to 300 seconds |
| `POST /jobs/{job_id}/cancel` | Cancel one job |
| `POST /stop` | Cancel conversation jobs; body: `agent`, `thread` |
| `GET /jobs/{job_id}/events` | SSE: initial job snapshot, progress, final result |
| `GET /events` | Owner SSE of gateway bus events |
| `GET /approvals` | Pending approvals addressed to CLI |
| `GET /approvals/events` | Human SSE connection: lease and pending list, then events |
| `POST /approvals/{id}` | Decision body: `option_id`, `lease_id` |

`agent` is optional with one configured agent; `thread` defaults to
`default`. Submission returns HTTP 202 and a job (running or finished after
the requested wait); creating a session returns 201. Unknown ids/agents
return 404, busy sessions 409, policy denials 403, invalid credentials 401,
validation errors 422 and oversized bodies 413.

SSE uses JSON `data` with explicit event names. Job streams start with
`snapshot` and finish with `JobFinished` (or a finished snapshot).
`JobProgress.event_type` distinguishes `MessageChunk`, `ThoughtChunk` and
other normalized agent events. Bounded queues protect the core from slow
readers. A job stream that loses events discards its older queued events
and emits an authoritative `resync` job snapshot before continuing. Other
streams emit a `resync` dropped count; approval clients then fetch the
pending list again. Streams send keepalives during idle periods.

An approval stream starts with `connected`, whose data includes `lease_id`
and `approvals`. Subsequent `ApprovalRequested` and `ApprovalResolved`
events reflect routing and settlement. Closing a stream removes its lease
and subscription. Pending requests keep their deadlines and can be decided
by another connected watcher.
