"""P4.8.2 dedicated driver smoke test; run in the existing agent's OS.

Creates one dispatcher conversation, verifies observer attach and detaches.
No model call unless --prompt is explicitly selected. Never resumes or interrupts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acp_gateway.adapters.hermes import HermesDedicatedClient
from acp_gateway.discovery.hermes import HermesDiscoveryError, default_lock_dir, read_endpoint
from acp_gateway.discovery.hermes_rpc import HermesRpcClient, stored_session_id


async def scenario(args):
    endpoint = read_endpoint(args.lock_dir or default_lock_dir(), args.role)
    async with (
        await HermesRpcClient.connect(endpoint, timeout=args.timeout) as observer,
        await HermesDedicatedClient.connect(
            endpoint, profile=args.profile, timeout=args.timeout
        ) as owner,
    ):
        chat = await owner.create(cwd=args.cwd)
        activated = await observer.call(
            "session.activate",
            {"profile": args.profile, "session_id": chat.runtime_id, "omit_messages": True},
        )
        if (
            activated.get("session_id") != chat.runtime_id
            or stored_session_id(activated) != chat.stored_id
        ):
            raise HermesDiscoveryError("observer did not attach to the dedicated conversation")
        report = {
            "mode": "dedicated-dispatcher",
            "version": observer.version,
            "runtime_id": chat.runtime_id,
            "stored_id": chat.stored_id,
            "same_live_and_stored_identity": True,
            "inference": "not_requested",
            "cancel_supported": False,
            "cold_resume_supported": False,
        }
        if args.prompt:
            await owner.submit(
                chat.handle,
                "Reply with exactly HERMES_DEDICATED_OK. Do not use tools or change any files.",
            )
            async with asyncio.timeout(args.completion_timeout):
                while True:
                    event = await owner.events.get()
                    if event["type"] == "gateway.uncertain":
                        raise HermesDiscoveryError("dedicated turn lost ownership certainty")
                    if event["type"] == "message.complete":
                        payload = event["payload"]
                        report["inference"] = "one_fixed_prompt"
                        report["completion_status"] = payload.get("status")
                        report["text_received"] = bool(payload.get("text"))
                        break
        await owner.close()
        report["observer_pong_after_detach"] = bool((await observer.call("ping")).get("pong"))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-dir", type=Path)
    parser.add_argument("--role", choices=("desktop-serve", "serve"), default="desktop-serve")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--cwd", required=True, help="Existing native directory on the agent host")
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--completion-timeout", type=float, default=120)
    parser.add_argument(
        "--prompt", action="store_true", help="Send one fixed text-only model prompt"
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(scenario(args))
    except HermesDiscoveryError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "reason": "dedicated smoke test failed"}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
