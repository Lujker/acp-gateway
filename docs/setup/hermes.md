# Hermes MCP channel

Start the gateway as described in [Gateway setup](gateway.md). Set a separate
random `ACPGW_MCP_TOKEN` in the gateway's `.env`, then restart `acpgw serve`.
The MCP endpoint is enabled only when this token is present. It listens at
`http://127.0.0.1:8765/mcp` on the same loopback port as the owner API.
The MCP token must differ from the owner token and every agent credential.

Add this entry to `mcp_servers` in your Hermes `config.yaml`:

```yaml
mcp_servers:
  acpgw:
    url: http://127.0.0.1:8765/mcp
    headers:
      Authorization: "Bearer ${ACPGW_MCP_TOKEN}"
    tool_timeout: 45
```

Provide `ACPGW_MCP_TOKEN` in Hermes' own environment or `.env`, using the same
value as the gateway. Hermes needs only this token: keep the owner API token
and the agent secret out of Hermes' configuration. Restart Hermes to discover
the tools. Its [MCP configuration loader](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool_config.py)
expands environment references; its [HTTP transport](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool_transport.py)
forwards the configured headers.

## Tools and conversations

Every configured agent exports six tools, prefixed with its alias. For `work`:

| Tool | Arguments | Result |
|---|---|---|
| `work_status` | `thread` | Cached connection status and running job ids |
| `work_sessions` | `thread` | Owned sessions and active gateway row id |
| `work_new_session` | `thread`, optional `cwd` | New active session |
| `work_ask` | `text`, `thread`, `wait`, optional `session_id` | Final answer or running job id |
| `work_result` | `job_id`, `thread`, `wait` | Final answer or running status |
| `work_cancel` | `thread`, optional `job_id` | Cancel one owned job, or every job in this thread |

`thread` defaults to `default`, is 1–256 characters, and should remain stable
through a conversation. Different threads and agents have separate active
sessions; CLI conversations are separate too. A `session_id` is the gateway's
integer row id from `sessions` or `new_session`. Targeting an older owned
session leaves the active session unchanged.

`wait` is 0–300 seconds, defaults to 30 in `ask` and 0 in `result`.
Keep it below Hermes' `tool_timeout` (45 seconds in the snippet), allowing
time for connection and session creation. For long tasks use `wait: 0`, then
poll `result` with the same `thread` and `job_id`. A running response contains
`job_id`, `session_id`, `status: running`, and null `answer`, `error` and
`stop_reason`. A final response fills those fields. No partial text, reasoning,
tool events, raw tool arguments or usage are forwarded through MCP.

Disconnecting Hermes leaves a submitted job running. Reconnect and poll its
id. The gateway persists final answers and conversation mappings; a daemon
restart marks unfinished jobs interrupted rather than resubmitting prompts.
The default result retention is seven days.

## Human approvals

Open `uv run acpgw approvals watch` before asking Hermes to request an agent
action. The CLI displays the tool request and offers allow once or reject.
The MCP channel is never a human approver and exports no decision tools.
Without a connected eligible human, the core rejects the request and the
final MCP result explains `no human approver is connected` in `error`.
Cancellation also settles pending permissions. Decisions are audited with
the originating `mcp` conversation and the deciding `cli` identity.

For example, ask Hermes to use `work_ask` with thread `research` to remember
a code word, then retrieve it in the same thread. Repeat in another thread
to verify session isolation. Ask for a harmless command with the CLI watcher
open and reject it there; repeat without the watcher to verify automatic
rejection. This full model-driven smoke against your Work Goose is a manual
check; the automated compatibility probe invokes Hermes' real MCP discovery
and tool registry against the recorded Goose mock without calling a Hermes
model.

## Verification

The implementation uses the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x),
with stateless Streamable HTTP and JSON responses. HTTP authentication,
loopback Host validation, body bounds and credential separation apply before
MCP parsing. The owner API continues to require its own token.

```bash
uv run pytest tests/integration/test_mcp.py tests/unit/test_mcp.py
HERMES_TEST_REPO=/path/to/hermes-agent \
HERMES_TEST_PYTHON=/path/to/hermes-dependency-venv/bin/python \
  uv run pytest tests/integration/test_hermes_compat.py
```

The optional probe uses the installed Hermes dependency interpreter and a
temporary `HERMES_HOME`; it changes no persistent Hermes configuration. It
checks discovery, multiple threads, multi-turn memory, asynchronous jobs,
CLI approve/reject, and rejection with no human. Without these environment
variables its three tests are skipped.
