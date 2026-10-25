"""Model-free Codex JSONL peer executed as a real, isolated child."""

import json
import os
import sys
import time
from pathlib import Path

SCENARIO = "happy"
LOG = Path(__file__).with_suffix(".log")


def send(frame):
    print(json.dumps(frame), flush=True)


def note(method, params):
    send({"method": method, "params": params})


def delta(text, thread="owned-thread", turn="owned-turn"):
    note(
        "item/agentMessage/delta",
        {"threadId": thread, "turnId": turn, "itemId": "answer", "delta": text},
    )


def complete(thread="owned-thread", turn="owned-turn", status="completed"):
    note(
        "turn/completed",
        {
            "threadId": thread,
            "turn": {
                "id": turn,
                "status": status,
                "items": [{"type": "agentMessage", "text": "Hello world"}],
            },
        },
    )


def finish():
    delta("Hello ")
    delta("world")
    complete()


for line in sys.stdin:
    frame = json.loads(line)
    with LOG.open("a") as log:
        log.write(json.dumps(frame) + "\n")
    method, rid, params = frame.get("method"), frame.get("id"), frame.get("params", {})
    if method == "initialize":
        send({"id": rid, "result": {"userAgent": "fake-codex/0.163.0"}})
    elif method == "thread/start":
        if SCENARIO == "new-timeout":
            continue
        assert params["approvalPolicy"] == ("on-request" if SCENARIO == "work" else "untrusted")
        assert params["approvalsReviewer"] == ("auto_review" if SCENARIO == "work" else "user")
        assert params["sandbox"] in {"read-only", "workspace-write"}
        assert params["cwd"] == str(Path.cwd())
        send({"id": rid, "result": {"thread": {"id": "owned-thread"}}})
    elif method == "turn/start":
        assert params["input"] == [{"type": "text", "text": "hello"}]
        assert params["threadId"] == "owned-thread"
        assert params["approvalPolicy"] == ("on-request" if SCENARIO == "work" else "untrusted")
        assert params["sandboxPolicy"]["networkAccess"] is (SCENARIO == "work")
        if SCENARIO == "early":
            finish()
        send({"id": rid, "result": {"turn": {"id": "owned-turn"}}})
        if SCENARIO == "items":
            note(
                "item/started",
                {
                    "threadId": "owned-thread",
                    "turnId": "owned-turn",
                    "item": {
                        "type": "commandExecution",
                        "id": "tool-1",
                        "command": "echo safe",
                        "cwd": str(Path.cwd()),
                        "status": "inProgress",
                    },
                },
            )
            note(
                "item/completed",
                {
                    "threadId": "owned-thread",
                    "turnId": "owned-turn",
                    "item": {"type": "commandExecution", "id": "tool-1", "status": "completed"},
                },
            )
            for _ in range(2):
                note(
                    "item/completed",
                    {
                        "threadId": "owned-thread",
                        "turnId": "owned-turn",
                        "item": {"type": "agentMessage", "id": "answer", "text": "Hello world"},
                    },
                )
            complete()
            continue
        if SCENARIO in {"hold", "early"}:
            continue
        if SCENARIO == "disconnect":
            sys.exit(0)
        if SCENARIO == "malformed":
            print("credential-secret-not-json", flush=True)
            continue
        if SCENARIO == "oversized":
            print("credential-secret" * 30_000, flush=True)
            continue
        if SCENARIO == "failed":
            complete(status="failed")
            continue
        if SCENARIO == "stderr":
            sys.stderr.write("credential-secret" * 100_000)
            sys.stderr.flush()
        if SCENARIO == "environment":
            assert "ACPGW_GATEWAY__API_TOKEN" not in os.environ
            assert "REMOTE_AGENT_SECRET" not in os.environ
            assert "TELEGRAM_BOT_TOKEN" not in os.environ
        if SCENARIO == "foreign":
            delta("FOREIGN", thread="another-thread")
            delta("FOREIGN", turn="another-turn")
            complete(thread="another-thread")
            complete(turn="another-turn")
        if SCENARIO in {
            "approval",
            "file-approval",
            "file-missing",
            "file-empty",
            "file-no-diff",
            "file-other-item",
            "file-other-thread",
            "file-other-turn",
            "file-wrong-type",
            "command-retained",
            "command-missing",
            "foreign-permission",
            "foreign-turn-permission",
            "grant-root",
            "unknown",
            "withdraw",
        }:
            approval_method = (
                "item/fileChange/requestApproval"
                if SCENARIO.startswith("file-") or SCENARIO == "grant-root"
                else "item/commandExecution/requestApproval"
            )
            if SCENARIO == "unknown":
                approval_method = "item/permissions/requestApproval"
            request = {
                "threadId": "foreign" if SCENARIO == "foreign-permission" else "owned-thread",
                "turnId": "foreign" if SCENARIO == "foreign-turn-permission" else "owned-turn",
                "itemId": "tool-1",
                "command": "echo safe",
                "cwd": str(Path.cwd()),
                "reason": "needs review",
            }
            if SCENARIO.startswith("file-") or SCENARIO.startswith("command-"):
                request.pop("command")
                request.pop("cwd")
                item = {
                    "type": "fileChange",
                    "id": "other-item" if SCENARIO == "file-other-item" else "tool-1",
                    "status": "inProgress",
                    "changes": [
                        {
                            "path": str(Path.cwd() / "review.txt"),
                            "kind": {"type": "update", "move_path": None},
                            "diff": "@@ -1 +1 @@\n-old\n+reviewed\n",
                        }
                    ],
                }
                if SCENARIO == "file-empty":
                    item["changes"] = []
                if SCENARIO == "file-no-diff":
                    item["changes"][0].pop("diff")
                if SCENARIO in {"file-wrong-type", "command-retained"}:
                    item = {
                        "id": "tool-1",
                        "type": "commandExecution",
                        "command": "echo retained",
                        "cwd": str(Path.cwd()),
                    }
                if SCENARIO not in {"file-missing", "command-missing"}:
                    note(
                        "item/started",
                        {
                            "threadId": (
                                "other-thread"
                                if SCENARIO == "file-other-thread"
                                else "owned-thread"
                            ),
                            "turnId": (
                                "other-turn" if SCENARIO == "file-other-turn" else "owned-turn"
                            ),
                            "item": item,
                        },
                    )
            if SCENARIO == "grant-root":
                request["grantRoot"] = "/"
            send({"id": "approval-1", "method": approval_method, "params": request})
            if SCENARIO == "withdraw":
                time.sleep(0.05)
                note(
                    "serverRequest/resolved",
                    {"threadId": "owned-thread", "requestId": "approval-1"},
                )
                finish()
            continue
        finish()
    elif method is None and rid == "approval-1":
        finish()
