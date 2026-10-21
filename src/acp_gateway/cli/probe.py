"""Read-only discovery and protocol checks, independent of Gateway config."""

import asyncio
import json
import math
import os
import re
from pathlib import Path

from acp_gateway.cli.setup_asyncio import run as run_setup
from acp_gateway.discovery.hermes import (
    ROLES,
    HermesDiscoveryError,
    default_lock_dir,
    read_endpoint,
)
from acp_gateway.discovery.hermes_rpc import HermesRpcClient


async def probe_hermes(
    directory: Path, *, role: str | None = None, timeout: float = 5
) -> list[dict]:
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise HermesDiscoveryError("probe timeout must be greater than zero and at most 60 seconds")
    rows = []
    for selected in (role,) if role else ROLES:
        row = {"agent": "hermes", "role": selected}
        try:
            endpoint = await asyncio.to_thread(read_endpoint, directory, selected)
            async with await HermesRpcClient.connect(endpoint, timeout=timeout) as client:
                ping = await client.call("ping")
                capabilities = await client.call("gateway.capabilities")
                active = await client.call("session.active_list")
                if ping.get("pong") is not True or not isinstance(active.get("sessions"), list):
                    raise HermesDiscoveryError("Hermes returned an incompatible probe response")
                row.update(
                    status="ready",
                    version=client.version,
                    endpoint=endpoint.base_url,
                    profiles=list(endpoint.profiles),
                    active_sessions=len(active["sessions"]),
                    exclusive_submit=capabilities.get("per_session_exclusive_submit") is True,
                    access={
                        "mode": "observe",
                        "shared_prompt": False,
                        "shared_cancel": False,
                        "approval_decisions": False,
                        "cold_resume": False,
                        "reason": "upstream_turn_guard_unavailable",
                    },
                )
        except FileNotFoundError:
            row.update(status="missing", detail="No Hermes rendezvous record for this role")
        except HermesDiscoveryError as exc:
            row.update(status=exc.status, detail="Hermes discovery or authenticated probe failed")
        except OSError:
            row.update(
                status="unavailable", detail="Hermes discovery or authenticated probe failed"
            )
        rows.append(row)
    return rows


def run(args) -> int:
    try:
        if args.kind == "goose":
            from acp_gateway.discovery.goose import probe_goose

            if args.lock_dir or args.role:
                raise ValueError("Hermes discovery options cannot be used with Goose")
            secret = None
            if args.secret_env:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.secret_env):
                    raise ValueError("invalid secret environment variable name")
                secret = os.environ.get(args.secret_env)
                if not secret:
                    raise ValueError("the selected secret environment variable is empty")
            rows = [
                run_setup(
                    probe_goose(
                        args.url,
                        secret=secret,
                        fingerprint=args.tls_fingerprint,
                        timeout=args.timeout,
                    ),
                    operation="goose-probe",
                )
            ]
        else:
            if args.url or args.secret_env or args.tls_fingerprint:
                raise ValueError("Goose endpoint options cannot be used with Hermes")
            rows = run_setup(
                probe_hermes(
                    args.lock_dir or default_lock_dir(), role=args.role, timeout=args.timeout
                ),
                operation="hermes-probe",
            )
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    if args.json:
        print(json.dumps({"agents": rows}, indent=2))
    else:
        for row in rows:
            label = (
                f"Hermes ({row['role']})"
                if row["agent"] == "hermes"
                else "Goose (explicit endpoint)"
            )
            print(f"{label}: {row['status']}")
            if row["status"] == "ready":
                print(f"  Version: {row['version']}; endpoint: {row['endpoint']}")
                if row["agent"] == "hermes":
                    print(
                        f"  Profiles: {', '.join(row['profiles'])}; "
                        f"live sessions: {row['active_sessions']}"
                    )
                    print("  Shared-dialogue control: unavailable (turn ownership is not guarded)")
                else:
                    print(
                        "  Initialization only; session ownership and shared control "
                        "are not verified"
                    )
            else:
                print(f"  {row['detail']}")
        print("Read-only probe: no sessions created, prompts sent or approvals accepted.")
    return 0 if any(row["status"] == "ready" for row in rows) else 1
