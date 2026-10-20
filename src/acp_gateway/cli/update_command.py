"""Ordinary ``acpgw update`` frontend: artifact gates and local authorization.

The normal commands (``--check``, ``--stable`` and exact ``--version``) share the
release-artifact gate with Telegram. A uv installation automatically selects its
managed service coordinator; a Docker gateway submits to the shared updater spool
instead of installing packages or touching the container runtime. Explicit
advanced switches (``--with-services``, ``--from``, ``--rollback``, coordinator
and handoff options) keep the pre-existing manual paths unchanged.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from uuid import uuid4

from packaging.version import Version

from acp_gateway.updates import (
    available_exact_release,
    available_stable_release,
    installation_report,
)

WARNING = (
    "Warning: the update restarts the service; an active dialogue and any "
    "attachment or partial answer can be lost and are not resumed automatically."
)

_ABSENT_SERVICE = (
    "not installed",
    "persistent config and environment",
    "Invalid connector task selection",
    "no supported config/env selection",
)


def load_config(config_path=None, env_file=None):
    """Import configuration lazily; read-only commands keep the caller's process light."""
    from acp_gateway.config import load_config as load

    return load(config_path, env_file)


def _explicit(value):
    return value not in {None, False, "", ()}


def _advanced(args):
    """Any explicit advanced switch keeps the documented manual path."""
    for name in (
        "with_services",
        "service_role",
        "restart_uncertain",
        "handoff_file",
        "service_coordinator_file",
        "operation_id",
        "source",
        "rollback",
        "constraints",
    ):
        if _explicit(getattr(args, name, None)):
            return True
    return getattr(args, "service_coordinator_fd", None) is not None


class _NoSpool:
    """Docker artifact gate without a local updater: nothing is prepared."""

    @staticmethod
    def version_allowed(version):
        return False


def _config(args):
    return load_config(getattr(args, "config", None), getattr(args, "env_file", None))


def _spool_path(config):
    root = config.settings.updates.spool_dir or (
        config.settings.resolved_data_dir() / "update-spool"
    )
    return root / "operations.db"


def _worker_scope(config):
    from acp_gateway.update_journal import Journal

    journal = Journal(_spool_path(config))
    try:
        return journal.worker()
    finally:
        journal.close()


def _local_view(args, report):
    """Return the local updater view for Docker; never creates a spool."""
    if report.manager != "docker":
        return None
    config = _config(args)
    if not _spool_path(config).is_file():
        return _NoSpool()
    from acp_gateway.update_service import LocalUpdates

    worker = _worker_scope(config) or {}
    role = worker.get("role") if worker.get("role") in {"dispatcher", "connector"} else "dispatcher"
    target = worker.get("target") if isinstance(worker.get("target"), str) else "dispatcher"
    return LocalUpdates(config, role=role, target=target)


def _announced(lookup):
    """Resolve once so refusal output can name the announced version."""
    seen = []

    def resolve():
        value = lookup()
        seen.append(value)
        return value

    return resolve, seen


def _check(args, report):
    from acp_gateway import installation

    local = _local_view(args, report)
    lookup, seen = _announced(installation.latest_stable_release)
    version, state = available_stable_release(
        report,
        release_lookup=lookup,
        image_lookup=local.version_allowed if local is not None else None,
    )
    announced = str(seen[0]) if seen and seen[0] is not None else None
    print(
        f"Installed: {report.installed_version} "
        f"(manager: {report.manager}, source: {report.source})"
    )
    if announced is None:
        print("No stable release is published yet.")
        return 0
    print(f"Latest stable release: {announced}")
    if state == "published" and version is not None:
        newer = Version(version) > Version(report.installed_version)
        print("Update available." if newer else "No newer stable release.")
    elif state == "unpublished" and isinstance(local, _NoSpool):
        print(
            "The local updater is not ready; Docker artifact availability cannot be "
            "verified, so no update is offered."
        )
    elif state == "unpublished":
        print("No supported artifact for this installation (unpublished); no update is offered.")
    elif state == "unsupported":
        print(
            "This installation is not supported by the built-in updater "
            f"({report.manager}/{report.source}); use its original package manager."
        )
    return 0


def _uv(args, report):
    """Select the managed coordinator automatically, or keep the local uv path."""
    if report.source != "registry":
        # The manual path documents the exact refusal for file/git/editable installs.
        return None
    from acp_gateway import installation, service_update

    try:
        selection = service_update.resolve_selection(
            getattr(args, "config", None), getattr(args, "env_file", None), None
        )
        inspected = service_update.inspect_service(*selection)
    except ValueError as exc:
        if any(marker in str(exc) for marker in _ABSENT_SERVICE):
            return None
        raise
    exact = getattr(args, "version", None)
    if exact:
        target = str(exact)
    else:
        latest = installation.latest_stable_release()
        if latest is None:
            print("No stable PyPI release is available.")
            return 0
        target = str(latest)
        if Version(target) <= Version(report.installed_version):
            print("No newer stable release. Package and services are unchanged.")
            return 0
    version, state = available_exact_release(report, target)
    if version is None:
        raise ValueError(
            f"Release {target} has no supported artifact for this installation ({state}); "
            "the managed service was not stopped"
        )
    args.with_services = True
    args.service_role = inspected["role"]
    args.version = version
    args.stable = False
    if not getattr(args, "dry_run", False):
        print(WARNING, file=sys.stderr)
    return None


def _owner_maintenance(config, *, client=None):
    import httpx

    from acp_gateway.cli.client import api_url
    from acp_gateway.daemon import owner_token

    token = owner_token(config)
    headers = {"Authorization": "Bearer " + token.get_secret_value()}
    url = api_url(config) + "/maintenance"
    if client is None:
        with httpx.Client(timeout=5, follow_redirects=False) as session:
            response = session.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
    response = client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def _dispatcher_idle(config, *, client=None):
    """Read the bounded local owner state; unknown means not idle, never guessed."""
    import httpx

    try:
        state = _owner_maintenance(config, client=client)
    except (OSError, ValueError, httpx.HTTPError, KeyError, TypeError):
        return False
    if not isinstance(state, dict) or not state:
        return False
    if any(type(value) not in {bool, int} for value in state.values()):
        return False
    return not any(state.values())


def _connector_idle(config):
    from acp_gateway.connectors.maintenance import can_reserve, read_status

    status = read_status(config.settings.resolved_data_dir())
    return status.get("state") == "running" and can_reserve(status)


def _idle_check(config, role):
    return _connector_idle(config) if role == "connector" else _dispatcher_idle(config)


def _docker_local(config):
    """Automatic role/target from the worker heartbeat; never a manual switch."""
    if not _spool_path(config).is_file():
        raise ValueError(
            "No local updater spool is present; prepare the host updater before updating"
        )
    worker = _worker_scope(config) or {}
    role, target, backend = worker.get("role"), worker.get("target"), worker.get("backend")
    if (
        role not in {"dispatcher", "connector"}
        or not isinstance(target, str)
        or backend != "docker"
    ):
        raise ValueError(
            "The local updater heartbeat is not ready for this Docker installation; "
            "inspect updater readiness"
        )
    from acp_gateway.update_service import LocalUpdates

    return LocalUpdates(
        config,
        role=role,
        target=target,
        idle_check=lambda: _idle_check(config, role),
    )


def _docker_target(args, report, local):
    from acp_gateway import installation

    exact = getattr(args, "version", None)
    if exact:
        version, state = available_exact_release(
            report, str(exact), image_lookup=local.version_allowed
        )
        if version is None:
            raise ValueError(
                f"Release {exact} has no supported artifact for this installation ({state}); "
                "the container was not replaced"
            )
        return version, None
    lookup, seen = _announced(installation.latest_stable_release)
    version, state = available_stable_release(
        report, release_lookup=lookup, image_lookup=local.version_allowed
    )
    announced = str(seen[0]) if seen and seen[0] is not None else None
    if version is None:
        if announced is None:
            print("No stable release is published yet.")
            return None, 0
        raise ValueError(
            f"Release {announced} has no supported artifact for this installation ({state}); "
            "no update is offered"
        )
    if Version(version) <= Version(report.installed_version):
        print("No newer stable release. Package and container are unchanged.")
        return None, 0
    return version, None


_REFUSALS = {
    "disabled": "Updates are disabled for this installation (updates.enabled is false).",
    "busy": "Update refused locally (busy): finish active work and submit a new explicit "
    "command. An unfinished or unknown operation also blocks new requests; inspect "
    "update --status.",
    "worker_stopped": "The local updater worker is not ready (no fresh heartbeat).",
    "expired": "The update request expired before admission; submit a new explicit command.",
    "version_not_allowed": "This exact version is not prepared in the local updater policy.",
    "conflict": "This update operation id conflicts with another request.",
}


def _refusal(reason):
    return _REFUSALS.get(reason, f"Update refused locally ({reason or 'failed'}).")


def _submit(local, report, version):
    from acp_gateway.connectors.protocol import UpdateRequest

    print(WARNING, file=sys.stderr)
    request = UpdateRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=uuid4(),
        action="execute",
        actor="cli",
        target_version=version,
        current_version=report.installed_version,
        expires=time.time() + 300,
    )
    try:
        result = local.request(request)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(
            f"Local updater spool is unavailable ({type(exc).__name__}); inspect update --status"
        ) from None
    if result.status == "failed":
        raise ValueError(_refusal(result.reason))
    print(f"Update operation {result.operation.hex}: {result.status}")
    print("ACPGW_UPDATE_OPERATION=" + result.operation.hex)
    return 0 if result.status in {"queued", "succeeded"} else 2


def _docker_status(config):
    path = _spool_path(config)
    if not path.is_file():
        print("No recorded maintenance operation.")
        return 0
    from acp_gateway.update_journal import Journal

    journal = Journal(path)
    try:
        rows = journal.recent(limit=1)
    finally:
        journal.close()
    if not rows:
        print("No recorded maintenance operation.")
        return 0
    row = rows[0]
    print(
        json.dumps(
            {
                key: row[key]
                for key in ("id", "status", "reason", "version", "current_version", "updated")
            }
        )
    )
    return 0 if row["status"] == "succeeded" else 1 if row["status"] == "failed" else 2


def _docker(args, report):
    if getattr(args, "check", False):
        return _check(args, report)
    config = _config(args)
    if getattr(args, "status", False):
        return _docker_status(config)
    if not config.settings.updates.enabled:
        raise ValueError(_REFUSALS["disabled"])
    local = _docker_local(config)
    version, refusal = _docker_target(args, report, local)
    if refusal is not None:
        return refusal
    if getattr(args, "dry_run", False):
        print(
            f"Would submit {version} to the local updater spool and recreate the managed container."
        )
        return 0
    return _submit(local, report, version)


def run(args, info):
    """Return an exit code for an ordinary command, or None for the manual path."""
    if getattr(args, "command", None) != "update" or _advanced(args):
        return None
    report = installation_report(role="dispatcher")
    if report.manager == "docker":
        return _docker(args, report)
    if report.manager != "uv" or getattr(args, "status", False):
        return None
    if getattr(args, "check", False):
        return _check(args, report)
    return _uv(args, report)
