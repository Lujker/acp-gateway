"""Synthetic model-free peer using the official Claude stream-json envelope."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCENARIO = "happy"
SID = (
    sys.argv[sys.argv.index("--resume") + 1]
    if "--resume" in sys.argv
    else next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--session-id="))
)


def emit(frame):
    print(json.dumps(frame), flush=True)


def record(frame):
    with Path("claude-peer.log").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"pid": os.getpid(), "sid": SID, "frame": frame}) + "\n")


history = []
history_file = Path("claude-history-" + SID + ".json")
if "--resume" in sys.argv:
    history = json.loads(history_file.read_text())
record({"argv": sys.argv[1:]})
for line in sys.stdin:
    frame = json.loads(line)
    record(frame)
    if frame["type"] == "control_request":
        if frame["request"]["subtype"] == "interrupt":
            if SCENARIO == "interrupt-no-ack":
                emit(
                    {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "session_id": SID,
                        "errors": ["interrupted"],
                    }
                )
                continue
            emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": frame["request_id"],
                        "response": {},
                    },
                }
            )
            if SCENARIO == "interrupt-no-result":
                continue
            emit(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "session_id": SID,
                    "errors": ["interrupted"],
                }
            )
            continue
        assert frame["request"] == {"subtype": "initialize", "hooks": None}
        if SCENARIO == "bad-init":
            emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "error",
                        "request_id": frame["request_id"],
                        "error": "native-secret",
                    },
                }
            )
            continue
        if SCENARIO == "hang-init":
            continue
        if SCENARIO == "eof-init":
            sys.exit(0)
        if SCENARIO == "slow-init":
            time.sleep(0.15)
        emit(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": frame["request_id"],
                    "response": {},
                },
            }
        )
        if SCENARIO in {"descendant-live", "descendant-exited"}:
            descendant = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL
            )
            record({"descendant": descendant.pid})
            if SCENARIO == "descendant-exited":
                sys.exit(0)
    elif frame["type"] == "user":
        assert frame["session_id"] == SID
        assert frame["parent_tool_use_id"] is None
        assert frame["message"]["role"] == "user"
        history.append(frame["message"]["content"])
        history_file.write_text(json.dumps(history))
        if SCENARIO.startswith("observation"):
            emit(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "foreign",
                    "model": "foreign-model",
                }
            )
            emit(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": SID,
                    "parent_tool_use_id": "subagent",
                    "model": "subagent-model",
                }
            )
            if SCENARIO not in {"observation-foreign-only", "observation-late"}:
                model = (
                    ["invalid"]
                    if SCENARIO == "observation-invalid"
                    else frame["message"]["content"]
                    if SCENARIO == "observation-secret"
                    else "native-" + SID
                )
                emit(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": SID,
                        "model": model,
                        "provider": "do-not-infer",
                        "effort": "high",
                    }
                )
        if SCENARIO == "hang" or SCENARIO.startswith("interrupt"):
            continue
        if SCENARIO in {"error-result", "error-subtype", "foreign-result", "observation-error"}:
            emit(
                {
                    "type": "result",
                    "subtype": "error_during_execution"
                    if SCENARIO == "error-subtype"
                    else "success",
                    "is_error": SCENARIO in {"error-result", "observation-error"},
                    "session_id": "foreign" if SCENARIO == "foreign-result" else SID,
                    "result": "native-secret",
                }
            )
            continue
        if SCENARIO == "disconnect":
            sys.exit(0)
        if SCENARIO == "malformed":
            print("native-secret malformed-json", flush=True)
            continue
        if SCENARIO == "oversize":
            print("x" * 300_000, flush=True)
            continue
        if SCENARIO == "truncated":
            sys.stdout.write('{"type":"native-secret"}')
            sys.stdout.flush()
            sys.exit(0)
        if SCENARIO == "flood":
            for i in range(256):
                emit(
                    {
                        "type": "assistant",
                        "session_id": SID,
                        "message": {"id": str(i), "content": [{"type": "text", "text": "chunk"}]},
                    }
                )
            continue
        if SCENARIO == "environment":
            for key in (
                "TELEGRAM_BOT_TOKEN",
                "ACPGW_GATEWAY__API_TOKEN",
                "ANTHROPIC_API_KEY",
                "CLAUDE_CODE_BASH_ENV_FILE",
                "REMOTE_AGENT_SECRET",
            ):
                assert key not in os.environ
            for _ in range(100):
                os.write(2, b"native-secret" * 1024)
        if SCENARIO == "unknown-control":
            emit(
                {
                    "type": "control_request",
                    "request_id": "unknown-1",
                    "request": {"subtype": "set_permission_mode", "mode": "bypassPermissions"},
                }
            )
            record(json.loads(next(sys.stdin)))
        if SCENARIO.startswith("permission-"):
            tool = SCENARIO.split("-", 1)[1]
            inputs = {
                "Bash": {"command": "echo reviewed", "description": "test command"},
                "Edit": {"file_path": "/review.txt", "old_string": "old", "new_string": "new"},
                "Write": {"file_path": "/review.txt", "content": "reviewed"},
                "Read": {"file_path": "/review.txt"},
                "Glob": {"pattern": "*.py"},
                "Grep": {"pattern": "reviewed"},
                "WebFetch": {"url": "https://example.com", "prompt": "read"},
            }
            emit(
                {
                    "type": "control_request",
                    "request_id": "permission-1",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": tool,
                        "input": inputs[tool],
                        "tool_use_id": "tool-1",
                    },
                }
            )
            response = json.loads(next(sys.stdin))
            record(response)
            assert response["response"]["request_id"] == "permission-1"
        if SCENARIO.startswith("unsafe-"):
            request = {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "echo reviewed"},
                "tool_use_id": "tool-1",
            }
            if SCENARIO == "unsafe-background":
                request["input"]["run_in_background"] = True
            elif SCENARIO == "unsafe-missing":
                request["input"] = {}
            elif SCENARIO == "unsafe-unknown":
                request["tool_name"] = "Agent"
            elif SCENARIO == "unsafe-agent":
                request["agent_id"] = "subagent"
            elif SCENARIO == "unsafe-foreign":
                request["session_id"] = "foreign"
            elif SCENARIO == "unsafe-empty-command":
                request["input"] = {"command": " "}
            elif SCENARIO == "unsafe-incomplete-edit":
                request["tool_name"], request["input"] = "Edit", {"file_path": "/review.txt"}
            emit({"type": "control_request", "request_id": "permission-1", "request": request})
            record(json.loads(next(sys.stdin)))
        if SCENARIO in {"cancel-permission", "result-permission", "duplicate-permission"}:
            emit(
                {
                    "type": "control_request",
                    "request_id": "permission-1",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": "Bash",
                        "input": {"command": "echo review"},
                        "tool_use_id": "tool-1",
                    },
                }
            )
            if SCENARIO == "duplicate-permission":
                emit(
                    {
                        "type": "control_request",
                        "request_id": "permission-1",
                        "request": {
                            "subtype": "can_use_tool",
                            "tool_name": "Bash",
                            "input": {"command": "echo review"},
                            "tool_use_id": "tool-1",
                        },
                    }
                )
            if SCENARIO == "cancel-permission":
                emit({"type": "control_cancel_request", "request_id": "permission-1"})
        if SCENARIO == "stream":
            emit(
                {
                    "type": "stream_event",
                    "session_id": "foreign",
                    "parent_tool_use_id": None,
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "LEAK"},
                    },
                }
            )
            emit(
                {
                    "type": "assistant",
                    "session_id": SID,
                    "parent_tool_use_id": "sub-tool",
                    "message": {"content": [{"type": "text", "text": "SUBAGENT"}]},
                }
            )
            emit(
                {
                    "type": "user",
                    "session_id": SID,
                    "message": {"content": [{"type": "tool_result", "content": "TOOL OUTPUT"}]},
                }
            )
            emit(
                {
                    "type": "stream_event",
                    "session_id": SID,
                    "parent_tool_use_id": None,
                    "event": {"type": "message_start", "message": {"id": "m-1"}},
                }
            )
            for text in ("streamed ", "reply"):
                emit(
                    {
                        "type": "stream_event",
                        "session_id": SID,
                        "parent_tool_use_id": None,
                        "event": {
                            "type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": text},
                        },
                    }
                )
        if SCENARIO.startswith("native-blocks"):
            # Real Claude Code: one `assistant` frame per content block, all sharing
            # the API message id; partial events precede each block's frame.
            partial = SCENARIO != "native-blocks-plain"
            messages = [
                ("m-0", [("text", "Plan."), ("tool_use", None)]),
                ("m-1", [("thinking", "hmm"), ("text", "Checking."), ("tool_use", None)]),
                ("m-2", [("thinking", "done"), ("text", "Final answer.")]),
            ]
            if SCENARIO == "native-blocks-max-turns":
                messages[2] = ("m-2", [("thinking", "more"), ("tool_use", None)])
            for mid, blocks in messages:

                def event(payload):
                    emit(
                        {
                            "type": "stream_event",
                            "session_id": SID,
                            "parent_tool_use_id": None,
                            "event": payload,
                        }
                    )

                if partial:
                    event({"type": "message_start", "message": {"id": mid}})
                for index, (kind, text) in enumerate(blocks):
                    block = {"type": kind}
                    if kind == "tool_use":
                        block.update(id=f"tool-{mid}", name="Bash", input={"command": "ls"})
                    else:
                        block[kind] = text
                    if partial:
                        start = {**block, kind: ""} if kind != "tool_use" else block
                        event(
                            {"type": "content_block_start", "index": index, "content_block": start}
                        )
                        if kind == "text":
                            for piece in (text[:3], text[3:]):
                                event(
                                    {
                                        "type": "content_block_delta",
                                        "index": index,
                                        "delta": {"type": "text_delta", "text": piece},
                                    }
                                )
                        event({"type": "content_block_stop", "index": index})
                    emit(
                        {
                            "type": "assistant",
                            "session_id": SID,
                            "parent_tool_use_id": None,
                            "message": {"id": mid, "role": "assistant", "content": [block]},
                        }
                    )
                if partial:
                    stop = "tool_use" if blocks[-1][0] == "tool_use" else "end_turn"
                    event({"type": "message_delta", "delta": {"stop_reason": stop}})
                    event({"type": "message_stop"})
            if SCENARIO == "native-blocks-max-turns":
                emit(
                    {
                        "type": "result",
                        "subtype": "error_max_turns",
                        "is_error": True,
                        "session_id": SID,
                    }
                )
            else:
                emit(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "session_id": SID,
                        "result": "Final answer.",
                    }
                )
            continue
        if SCENARIO != "result-permission":
            emit(
                {
                    "type": "assistant",
                    "session_id": SID,
                    "parent_tool_use_id": None,
                    "message": {
                        "id": "m-" + str(len(history)),
                        "role": "assistant",
                        "content": [{"type": "text", "text": "|".join(history)}],
                    },
                }
            )
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": SID,
                "result": "|".join(history),
            }
        )
        if SCENARIO.startswith("observation"):
            emit(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": SID,
                    "model": "late-duplicate-model",
                }
            )
