"""Local diagnosis and audited resolution; never repeat an installer command."""

import json
import subprocess
import time
from pathlib import Path

from acp_gateway.maintenance_worker import lease
from acp_gateway.remote_update_worker import (
    IMAGE,
    VERSION,
    cli,
    docker_health,
    invoke,
    load_policy,
    valid_intent,
    version_info,
)
from acp_gateway.update_journal import Journal, operation_id
from acp_gateway.updater_service import _atomic, _private_directory


def health(policy):
    if policy["role"] == "connector":
        value = json.loads(
            cli(policy, "maintenance", "--role", "connector", "status", capture=True)
        )
        if not isinstance(value, dict) or value.get("state") != "running" or value.get("reserved"):
            raise ValueError("Connector has not become ready; inspect its maintenance status")
    else:
        cli(policy, "status", capture=True)


def diagnose(path):
    policy = load_policy(path)
    result = {"role": policy["role"], "target": policy["target"], "backend": policy["backend"]}
    journal_path = Path(policy["spool_dir"]) / "operations.db"
    if journal_path.is_file():
        journal = Journal(journal_path)
        try:
            result["worker"] = journal.worker()
            result["operations"] = journal.recent()
            result["resolutions"] = {
                row["id"]: journal.resolution(row["id"]) for row in result["operations"]
            }
        finally:
            journal.close()
    try:
        result["installation"] = version_info(policy)
        health(policy)
        result["health"] = "healthy"
    except (OSError, ValueError, subprocess.SubprocessError):
        result["health"] = "unavailable"
    if policy["backend"] == "docker":
        result["allowed_versions"] = list(policy["images"])
        result["recovery_directory"] = policy["work_dir"]
        result["image_override"] = policy["override_file"]
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


def resolve(path, key, outcome, note, *, acknowledge_idle=False):
    if not acknowledge_idle:
        raise ValueError(
            "Verify locally that installers/rollback processes have finished, "
            "then supply --acknowledge-idle"
        )
    if outcome not in {"succeeded", "failed"} or not note or not note.strip() or len(note) > 500:
        raise ValueError("Resolution requires --outcome succeeded|failed and a note (1..500 chars)")
    key = operation_id(key)
    policy = load_policy(path)
    root = Path(policy["spool_dir"])
    _private_directory(root)
    with lease(root / "worker.lock", exclusive=True):
        journal = Journal(root / "operations.db")
        try:
            row = journal.get(key)
            if not valid_intent(policy, row) or row["status"] != "unknown":
                raise ValueError(
                    "Only an unknown operation belonging to this policy can be resolved"
                )
            observed = row["version"] if outcome == "succeeded" else row["current_version"]
            if policy["backend"] == "docker":
                docker_health(policy, observed)
            else:
                if version_info(policy).get("version") != observed:
                    raise ValueError("Observed version does not match the selected resolution")
                health(policy)
            journal.resolve(
                key,
                outcome,
                {
                    "outcome": outcome,
                    "note": note.strip(),
                    "observed_version": observed,
                    "recorded": time.time(),
                    "source": "local_operator",
                },
            )
        finally:
            journal.close()
    print(f"Operation {key}: {outcome}; audit saved. No installer was invoked.")
    return 0


def allow_image(path, version, image):
    """Record an explicitly reviewed local immutable artifact. Never pull or build."""
    if not isinstance(version, str) or len(version) > 64 or not VERSION.fullmatch(version):
        raise ValueError("--version requires stable major.minor.patch")
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError("--image requires an immutable local image ID or repository digest")
    path = path.expanduser().absolute()
    policy = load_policy(path)
    if policy["backend"] != "docker":
        raise ValueError("allow-image is for Docker host policies only")
    root = Path(policy["spool_dir"])
    _private_directory(root)
    with lease(root / "worker.lock", exclusive=True):
        if version not in policy["images"] and len(policy["images"]) >= 100:
            raise ValueError("Allowed image limit reached")
        # Inspect the exact local reference and persist its content-addressed image ID.
        value = json.loads(
            invoke([policy["docker"], "image", "inspect", image], capture=True, timeout=30)
        )
        if (
            not isinstance(value, list)
            or len(value) != 1
            or not isinstance(value[0], dict)
            or not isinstance(value[0].get("Id"), str)
            or not IMAGE.fullmatch(value[0]["Id"])
        ):
            raise ValueError("Docker did not report an immutable local image ID")
        pinned = value[0]["Id"]
        if version in policy["images"] and policy["images"][version] not in {image, pinned}:
            raise ValueError("A different image is already approved for this version")
        # Preserve existing digest aliases on idempotent registration.
        policy["images"].setdefault(version, pinned)
        _atomic(path, json.dumps(policy, indent=2) + "\n")
    print(f"Allowed {version}: {pinned}. Restart updater to publish the new version list.")
    return 0
