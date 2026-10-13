"""Exercise installed Hermes helpers in memory, without a server, model or tools.

This is an opt-in compatibility lab, not a live Desktop acceptance test. The
actual busy/interrupt/request handlers run against synthetic sessions, agents
and persistence hooks. Only the selected report file is written.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def busy_probe(rebind, module) -> dict:
    results = {}
    for label, queued in (
        ("queue", False),
        ("steer", False),
        ("interrupt", False),
        ("interrupt", True),
        ("interrupt_legacy", False),
    ):
        mode = "interrupt" if label == "interrupt_legacy" else label
        actions = []
        session = {"running": True, "history_lock": threading.Lock(), "attached_images": []}
        agent = SimpleNamespace(
            _supports_active_turn_redirect=True,
            steer=lambda text, sink=actions: sink.append("steer") or True,
            redirect=lambda text, sink=actions: sink.append("redirect") or True,
        )
        if label == "interrupt_legacy":
            agent = SimpleNamespace()
        session["agent"] = agent
        scope = {
            "time": time,
            "_load_busy_input_mode": lambda selected=mode: selected,
            "_session_compression_in_flight": lambda state: False,
            "_coerce_message_text": str,
            "_is_text_only_busy_payload": lambda text: isinstance(text, str),
            "_record_inflight_correction": lambda state, text: None,
            "_drop_queued_duplicates_of_inflight_user": lambda state: None,
            "_ok": lambda rid, result: {"jsonrpc": "2.0", "id": rid, "result": result},
            "_enqueue_prompt": lambda *args, sink=actions, **kwargs: sink.append("queue"),
            "_persist_queued_user_row": lambda *args: None,
            "_interrupt_busy_session": lambda *args, sink=actions: sink.append("interrupt"),
        }
        scope["_ac_try_correction"] = rebind(module._ac_try_correction, scope)
        handler = rebind(module._handle_busy_submit, scope)
        response = handler("busy-test", "live-test", session, "test input", None, queued=queued)
        label = "client_queued" if queued else label
        results[label] = {"response": response, "actions": actions}
    check(results["queue"]["actions"] == ["queue"], "queue behavior changed")
    check(results["steer"]["actions"] == ["steer"], "steer behavior changed")
    check(results["interrupt"]["actions"] == ["redirect"], "redirect behavior changed")
    check(results["client_queued"]["actions"] == ["queue"], "queued input interrupted a turn")
    check(
        results["interrupt_legacy"]["actions"] == ["queue", "interrupt"],
        "legacy interrupt behavior changed",
    )
    check(
        all(row["response"]["result"]["status"] != "busy" for row in results.values()),
        "upstream now refuses busy submits; reassess adapter policy",
    )
    return results


def interrupt_probe(module) -> dict:
    handlers, interrupted, speech = {}, [], []
    # Gateway's earlier turn ended; Desktop now owns this session's current turn.
    session = {"running": True, "history_lock": threading.Lock(), "turn_owner": "desktop-later"}
    server = SimpleNamespace(
        register_method=lambda name, handler: handlers.__setitem__(name, handler),
        _tts_stream_stop=lambda: speech.append("stop"),
        _sess_nowait=lambda params, rid: (session, None),
        _sess=lambda params, rid: (session, None),
        _str_param=lambda params, key: params.get(key, ""),
        _session_uses_compute_host=lambda state: False,
        _interrupt_session_turn=lambda sid, state: interrupted.append(state["turn_owner"]),
        _retire_turn_marker=lambda *args: None,
        _ok=lambda rid, result: {"jsonrpc": "2.0", "id": rid, "result": result},
        logger=logging.getLogger("hermes-semantics-lab"),
    )
    module.register(server)
    server._resume_wake_after_interrupt = lambda: None
    handler = handlers["session.interrupt"]
    unguarded = handler("late-cancel", {"session_id": "live-test"})
    guarded = handler(
        "hosted-cancel", {"session_id": "live-test", "expected_hosted_task_id": "old-gateway-task"}
    )
    check(interrupted == ["desktop-later"], "interrupt behavior changed")
    check(guarded["result"]["status"] == "not_interrupted", "hosted guard behavior changed")
    return {
        "late_session_cancel": unguarded,
        "interrupted_owner": interrupted,
        "hosted_guard_on_ordinary_session": guarded,
        "tts_stop_calls_including_refused_interrupt": len(speech),
    }


def approval_probe(module) -> dict:
    frames, events, settled = [], [], []
    module.reset_for_tests()
    module.bind_sinks(
        frames.append,
        lambda kind, sid, payload: events.append(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"type": kind, "session_id": sid, "payload": payload},
            }
        ),
        lambda sid: True,
    )
    params = {
        "request_id": "approval-test",
        "command": "synthetic; never executed",
        "description": "Compatibility lab",
        "choices": ["once", "deny"],
    }

    def request():
        return module.send_async("approval", "live-test", params, settled.append)

    request()
    first_id = frames[-1]["id"]
    first = {"jsonrpc": "2.0", "id": first_id, "result": {"choice": "deny"}}
    late = {"jsonrpc": "2.0", "id": first_id, "result": {"choice": "once"}}
    check(module.resolve_response(first), "first approval response was refused")
    check(not module.resolve_response(late), "late approval response was accepted")
    check(settled == [{"choice": "deny"}], "approval was settled more than once")
    frames.extend([first, late])

    request()
    cancelled_id = frames[-1]["id"]
    check(module.cancel("unrelated-session") == 0, "cancel crossed sessions")
    check(module.cancel("live-test", reason="interrupted") == 1, "request was not cancelled")
    check(
        not module.resolve_response({"id": cancelled_id, "result": {"choice": "once"}}),
        "cancelled approval was accepted",
    )
    check(settled[-1] is None, "cancel did not fail closed")

    check(
        module.send("approval", "live-test", params, timeout=0) is None,
        "expired approval did not fail closed",
    )
    expired_id = frames[-1]["id"]
    check(
        not module.resolve_response({"id": expired_id, "result": {"choice": "once"}}),
        "expired approval was accepted",
    )
    check(not module.open_requests("live-test"), "lab left pending approvals")

    # All content is synthetic. Replace random wire IDs for stable reviewable fixtures.
    aliases = {
        rid: f"server-request-{n}"
        for n, rid in enumerate((first_id, cancelled_id, expired_id), start=1)
    }

    def sanitize(value):
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return aliases.get(value, value) if isinstance(value, str) else value

    return {
        "first_response_wins": True,
        "duplicate_ignored": True,
        "cancel_scoped_to_session": True,
        "cancelled_and_expired_answers_ignored": True,
        "frames": sanitize(frames),
        "notifications": sanitize(events),
    }


def run(repo: Path) -> dict:
    repo = repo.resolve(strict=True)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repo))
    names = ("method_ctx", "session_auto_continue", "methods_session_interrupt", "server_requests")
    modules = {name: importlib.import_module(f"tui_gateway.{name}") for name in names}
    hashes = {}
    for name, module in modules.items():
        source = Path(module.__file__).resolve()
        check(source.is_relative_to(repo), "loaded module is outside the selected Hermes checkout")
        hashes[f"tui_gateway/{name}.py"] = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "kind": "installed_upstream_helpers_with_synthetic_state",
        "source_sha256": hashes,
        "busy": busy_probe(modules["method_ctx"].rebind, modules["session_auto_continue"]),
        "interrupt": interrupt_probe(modules["methods_session_interrupt"]),
        "approvals": approval_probe(modules["server_requests"]),
        "live_desktop_test": False,
        "model_or_tools_called": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.hermes_repo)
    data = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(data, encoding="utf-8")
    else:
        print(data, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
