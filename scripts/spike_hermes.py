"""P4.8.1: existing Hermes backend, two peers and a dedicated test conversation.

Default: read-only discovery. --sessions creates a test session and attaches a
second client without inference. --observe-session attaches an already live
test conversation without answering requests. --resume-session requires explicit
--allow-auto-continue: Hermes may restart an interrupted turn. --prompt sends one fixed text-only
model prompt in a new test conversation. No human approvals are ever accepted.
Run inside the agent's OS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

# Also usable through runpy.run_path in Hermes' existing environment,
# without installing Gateway or altering the agent's dependency environment.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acp_gateway.cli.probe import probe_hermes
from acp_gateway.discovery.hermes import (
    HermesDiscoveryError,
    default_lock_dir,
    read_endpoint,
)
from acp_gateway.discovery.hermes_rpc import HermesRpcClient, stored_session_id


async def completion(client, session_id, timeout):
    counts = Counter()
    async with asyncio.timeout(timeout):
        while True:
            frame = await client.events.get()
            params = frame.get("params", {})
            if params.get("session_id") != session_id:
                continue
            counts[params.get("type", "unknown")] += 1
            if params.get("type") == "message.complete":
                payload = params.get("payload", {})
                return {
                    "events": dict(counts),
                    "status": payload.get("status"),
                    "has_error": bool(payload.get("error") or payload.get("failure_reason")),
                    "text_received": bool(payload.get("text")),
                }


async def scenario(args):
    directory = args.lock_dir or default_lock_dir()
    if not args.sessions and not args.resume_session and not args.observe_session:
        return {
            "mode": "read-only",
            "agents": await probe_hermes(directory, role=args.role, timeout=args.timeout),
        }
    endpoint = read_endpoint(directory, args.role)
    if args.profile not in endpoint.profiles:
        raise HermesDiscoveryError("the requested test profile is not advertised")
    if args.resume_session and not args.allow_auto_continue:
        raise HermesDiscoveryError("cold resume requires explicit auto-continue acknowledgement")
    diagnostic = bool(args.sessions or args.resume_session)
    async with (
        await HermesRpcClient.connect(
            endpoint, timeout=args.timeout, diagnostic=diagnostic
        ) as first,
        await HermesRpcClient.connect(endpoint, timeout=args.timeout) as second,
    ):
        for client in (first, second):
            await client.call(
                "client.capabilities", {"server_requests": diagnostic and client is first}
            )
        if args.observe_session:
            created = await first.call(
                "session.activate",
                {
                    "profile": args.profile,
                    "session_id": args.observe_session,
                    "omit_messages": True,
                },
            )
        elif args.resume_session:
            created = await first.call(
                "session.resume",
                {
                    "profile": args.profile,
                    "session_id": args.resume_session,
                    "omit_messages": True,
                },
            )
        else:
            created = await first.call(
                "session.create",
                {
                    "profile": args.profile,
                    "title": "ACP Gateway attach probe",
                    "source": "desktop",
                    "close_on_disconnect": False,
                },
            )
        live_id, stored_id = created["session_id"], stored_session_id(created)
        if not stored_id:
            raise HermesDiscoveryError("Hermes did not return a stored session identity")
        if args.resume_session and stored_id != args.resume_session:
            raise HermesDiscoveryError("Hermes resumed a different stored session")
        if args.observe_session and live_id != args.observe_session:
            raise HermesDiscoveryError("Hermes activated a different live session")
        if diagnostic:
            first.select_test_session(live_id, profile=args.profile)
        activated = await second.call(
            "session.activate",
            {
                "profile": args.profile,
                "session_id": live_id,
                "omit_messages": True,
            },
        )
        report = {
            "mode": (
                "two-peer-observe"
                if args.observe_session
                else "two-peer-resume"
                if args.resume_session
                else "two-peer-session"
            ),
            "version": first.version,
            "second_peer_access": "observe",
            "live_id": live_id,
            "stored_id": stored_id,
            "same_live_session": activated.get("session_id") == live_id,
            "same_stored_session": stored_session_id(activated) == stored_id,
            "capabilities": await first.call("gateway.capabilities"),
            "inference": "not_requested",
        }
        if args.prompt:
            submitted = await first.call(
                "prompt.submit",
                {
                    "profile": args.profile,
                    "session_id": live_id,
                    "text": "Reply exactly ACPGW_ATTACH_OK. Do not use any tools.",
                },
            )
            report["submit"] = submitted.get("status")
            report["inference"] = await asyncio.gather(
                completion(first, live_id, args.turn_timeout),
                completion(second, live_id, args.turn_timeout),
            )
        await first.close()
        report["remaining_peer_pong"] = (await second.call("ping")).get("pong") is True
        # No delete/close RPC: even the test session can be open in Desktop.
        report["denied_server_requests"] = first.denied_requests + second.denied_requests
        report["ignored_server_requests"] = (
            first.ignored_request_count + second.ignored_request_count
        )
        report["session_cleanup"] = "left for owner; probe does not close or delete shared sessions"
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-dir", type=Path)
    parser.add_argument("--role", choices=("desktop-serve", "serve"), default="desktop-serve")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--turn-timeout", type=float, default=120)
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--sessions", action="store_true", help="create a dedicated test conversation"
    )
    target.add_argument(
        "--resume-session", help="stored test ID; Hermes may auto-continue a crashed turn"
    )
    target.add_argument("--observe-session", help="live test ID; no prompt or approval decisions")
    parser.add_argument(
        "--allow-auto-continue", action="store_true", help="acknowledge Hermes resume side effects"
    )
    parser.add_argument("--prompt", action="store_true", help="also make one fixed model call")
    args = parser.parse_args()
    if args.prompt and not args.sessions:
        parser.error("--prompt requires --sessions")
    if args.resume_session and not args.allow_auto_continue:
        parser.error("--resume-session requires --allow-auto-continue")
    if args.allow_auto_continue and not args.resume_session:
        parser.error("--allow-auto-continue requires --resume-session")
    if not 0 < args.timeout <= 60 or not 0 < args.turn_timeout <= 300:
        parser.error("timeout must be 0-60; turn-timeout must be 0-300 seconds")
    try:
        report = asyncio.run(scenario(args))
    except KeyboardInterrupt:
        return 130
    except HermesDiscoveryError as exc:
        print(json.dumps({"status": "failed", "detail": str(exc)}))
        return 1
    except (OSError, ValueError, TimeoutError, KeyError):
        print(json.dumps({"status": "failed", "detail": "Hermes probe failed or timed out"}))
        return 1
    print(json.dumps(report, indent=2))
    if not args.sessions and not args.resume_session and not args.observe_session:
        return 0 if any(row["status"] == "ready" for row in report["agents"]) else 1
    success = (
        report["same_live_session"]
        and report["same_stored_session"]
        and report["remaining_peer_pong"]
    )
    if args.prompt:
        success = success and all(
            result["status"] == "complete" and not result["has_error"] and result["text_received"]
            for result in report["inference"]
        )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
