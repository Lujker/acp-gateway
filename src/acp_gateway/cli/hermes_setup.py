"""Read-only Hermes selection shared by setup and doctor. No agent mutations."""

import asyncio
import os
import platform
from pathlib import Path

from acp_gateway.cli.probe import probe_hermes
from acp_gateway.discovery.hermes import ROLES, default_lock_dir


def inspect(directory: Path, *, role=None, profile=None, timeout=5):
    rows = asyncio.run(probe_hermes(directory, role=role, timeout=timeout))
    if profile is not None:
        for row in rows:
            if row["status"] == "ready" and profile not in row["profiles"]:
                row.update(
                    status="profile-missing", detail="Configured Hermes profile is not advertised"
                )
    return rows


def select(prompts, old):
    environment = "WSL" if os.environ.get("WSL_DISTRO_NAME") else platform.system()
    print(f"Find existing Hermes in {environment}, under the current user.")
    print("Windows and WSL are separate environments; run the connector beside the agent.")
    directory = Path(
        prompts.ask(
            "Hermes discovery directory",
            old.get("lock_dir") or str(default_lock_dir()),
            lambda v: str(Path(v).expanduser().absolute()),
        )
    )
    rows = inspect(directory)
    for row in rows:
        if row["status"] == "ready":
            print(
                f"Hermes {row['role']}: {row['version']}; "
                f"profiles: {', '.join(row['profiles'])}; live sessions: {row['active_sessions']}"
            )
        else:
            print(f"Hermes {row['role']}: {row['status']}; {row['detail']}")
    ready = [row for row in rows if row["status"] == "ready"]
    if ready:
        roles = tuple(row["role"] for row in ready)
        role = prompts.choice(
            "Existing Hermes instance",
            roles,
            old.get("role") if old.get("role") in roles else roles[0],
        )
        row = next(row for row in ready if row["role"] == role)
        profiles = tuple(row["profiles"])
        profile = prompts.choice(
            "Hermes profile",
            profiles,
            old.get("profile") if old.get("profile") in profiles else profiles[0],
        )
    else:
        if not prompts.yes("Save this Hermes attachment without a live verification?"):
            raise ValueError("No verified Hermes instance; start Hermes in this OS and rerun setup")
        role = prompts.choice("Hermes role", ROLES, old.get("role", "desktop-serve"))
        profile = prompts.ask("Hermes profile name", old.get("profile", "default"))
    print("Dispatcher creates separate conversations in this running Hermes.")
    print("Keep those conversations for the dispatcher; Desktop may read them.")
    print(
        "Safe cancel and restore after disconnect are unavailable. Hermes tool policy is unchanged."
    )
    return {"lock_dir": str(directory), "role": role, "profile": profile}
