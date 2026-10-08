"""Run with Hermes' own Python; exercise its real discovery and tool registry.

This is an opt-in compatibility probe, not an LLM turn. HERMES_HOME points at
a temporary directory so discovery cannot modify the user's configuration.
"""

import json
import os
import sys

sys.path.insert(0, os.environ["ACPGW_HERMES_REPO"])

from tools.mcp_tool_discovery import register_mcp_servers
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from tools.mcp_tool_schema import mcp_prefixed_tool_name
from tools.registry import registry


def call(suffix, **arguments):
    payload = registry.dispatch(mcp_prefixed_tool_name("acpgw", f"work_{suffix}"), arguments)
    if isinstance(payload, str):
        payload = json.loads(payload)
    if payload.get("is_error") or payload.get("error"):
        raise RuntimeError("Hermes MCP tool call failed")
    result = payload["result"]
    return json.loads(result) if isinstance(result, str) else result


try:
    tools = register_mcp_servers(
        {
            "acpgw": {
                "url": os.environ["ACPGW_HERMES_URL"],
                "headers": {"Authorization": "Bearer " + os.environ["ACPGW_HERMES_TOKEN"]},
                "tool_timeout": 45,
            }
        }
    )
    expected = {
        mcp_prefixed_tool_name("acpgw", "work_" + suffix)
        for suffix in ("status", "sessions", "new_session", "ask", "result", "cancel")
    }
    if not expected <= set(tools):
        raise RuntimeError("Hermes did not discover the six gateway tools")
    if sys.argv[1] == "dialogue":
        call("ask", text="remember code word peach", thread="hermes-a")
        assert call("ask", text="What was the code word", thread="hermes-a")["answer"] == "peach"
        assert call("ask", text="What was the code word", thread="hermes-b")["answer"] == "unknown"
        job = call("ask", text="pong", thread="hermes-a", wait=0)
        assert job["answer"] is None
        assert call("result", job_id=job["job_id"], thread="hermes-a", wait=5)["answer"] == "pong"
        denied = call("ask", text="run exactly: echo hi .", thread="hermes-a", wait=5)
        assert denied["error"] == "no human approver is connected"
    else:
        job = call("ask", text="run exactly: echo hi .", thread="hermes-action", wait=0)
        final = call("result", job_id=job["job_id"], thread="hermes-action", wait=10)
        assert final["answer"] == sys.argv[1]
    print(json.dumps({"status": "passed", "scenario": sys.argv[1]}))
finally:
    shutdown_mcp_servers()
