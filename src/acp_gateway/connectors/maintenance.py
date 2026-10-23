"""Instance-fenced local admission reservation; data-directory ACLs authorize it."""

import asyncio
import re
import secrets
import time

import structlog

from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.cli.wizard import _atomic
from acp_gateway.connectors.lifecycle import RUNTIME_FILE, _read

STATUS = "connector-activity.json"
REQUEST = "connector-maintenance-request.json"
RESPONSE = "connector-maintenance-response.json"
FIELDS = (
    "pending_requests",
    "pending_approvals",
    "running_sessions",
    "uncertain_sessions",
    "opening",
)
log = structlog.get_logger(__name__)


def can_reserve(status, *, restart_uncertain=False):
    """An explicit restart acknowledges uncertainty, never active or unsupported work."""
    if status.get("reserved") or status.get("unsupported_agents", 0):
        return False
    if any(status.get(key, 0) for key in FIELDS if key != "uncertain_sessions"):
        return False
    return not status.get("uncertain_sessions", 0) or restart_uncertain


class ConnectorMaintenance:
    def __init__(self, profiles, managers):
        self.profiles, self.managers = profiles, managers
        self.token = None

    def check_admission(self):
        if self.token is not None:
            raise AgentUnavailable("Connector reserved for maintenance")

    def status(self):
        counts = dict.fromkeys(FIELDS, 0)
        unsupported = 0
        for profile in self.profiles:
            if profile.backend not in {"hermes", "codex", "claude_code"}:
                unsupported += 1
            elif profile.alias in self.managers:
                for key, value in self.managers[profile.alias].maintenance_status().items():
                    counts[key] += value
        return {
            **counts,
            "unsupported_agents": unsupported,
            "reserved": self.token is not None,
            "idle_observed": not unsupported and not any(counts.values()),
        }

    def session_control(self, operation, alias, handle=None):
        manager = self.managers.get(alias)
        profile = next((p for p in self.profiles if p.alias == alias), None)
        if manager is None or profile is None or profile.backend != "hermes":
            raise ValueError("Unknown local Hermes alias or no live manager")
        if operation == "sessions":
            return {"agent": alias, "sessions": manager.sessions()}
        if not isinstance(handle, str) or not re.fullmatch(r"[0-9a-f]{32}", handle):
            raise ValueError("Invalid local Hermes handle")
        if operation == "journal":
            return manager.journal(handle)
        if operation == "retire":
            if self.token is not None:
                raise ValueError("Connector is reserved for maintenance")
            return manager.retire(handle)
        raise ValueError("Unknown local session operation")

    def apply(self, operation, token=None, *, force=False, restart_uncertain=False):
        if operation == "prepare":
            self.check_admission()
            if not can_reserve(self.status(), restart_uncertain=restart_uncertain):
                raise ValueError("Connector busy, uncertain or unsupported; maintenance refused")
            self.token = secrets.token_hex(16)
            for manager in self.managers.values():
                manager.maintenance_reserved = True
            return {"token": self.token}
        if operation != "resume":
            raise ValueError("Unknown connector maintenance operation")
        if not force and (
            self.token is None
            or not isinstance(token, str)
            or not token.isascii()
            or not secrets.compare_digest(token, self.token)
        ):
            raise ValueError("Connector maintenance reservation does not match")
        self.token = None
        for manager in self.managers.values():
            manager.maintenance_reserved = False
        return self.status()


async def watch(directory, instance, controller, *, interval=0.2):
    import json

    last = None
    pending_response = None
    publish_failed = False
    try:
        while True:
            status = {
                "version": 1,
                "instance": instance,
                "published": time.time(),
                **controller.status(),
            }
            try:
                await asyncio.to_thread(_atomic, directory / STATUS, json.dumps(status).encode())
            except OSError as exc:
                if not publish_failed:
                    log.warning("connector_activity_publish_failed", error_type=type(exc).__name__)
                publish_failed = True
                await asyncio.sleep(interval)
                continue
            if publish_failed:
                log.info("connector_activity_publish_recovered")
                publish_failed = False
            try:
                request = await asyncio.to_thread(_read, directory / REQUEST)
                valid = (
                    isinstance(request, dict)
                    and type(request.get("version")) is int
                    and request["version"] == 1
                    and request.get("instance") == instance
                    and isinstance(request.get("request"), str)
                    and re.fullmatch(r"[a-f0-9]{32}", request["request"])
                    and type(request.get("expires")) in (int, float)
                    and time.time() <= request["expires"] <= time.time() + 30
                    and request.get("operation")
                    in {"prepare", "resume", "sessions", "journal", "retire"}
                    and type(request.get("force", False)) is bool
                    and type(request.get("restart_uncertain", False)) is bool
                )
                if valid and (request["request"] != last or pending_response is not None):
                    if request["request"] != last:
                        last = request["request"]
                        # No await between the activity check and closing admission.
                        try:
                            if request["operation"] in {"sessions", "journal", "retire"}:
                                result = controller.session_control(
                                    request["operation"],
                                    request.get("agent"),
                                    request.get("handle"),
                                )
                            else:
                                result = controller.apply(
                                    request["operation"],
                                    request.get("token"),
                                    force=request.get("force", False),
                                    restart_uncertain=request.get("restart_uncertain", False),
                                )
                            pending_response = {"status": 200, "result": result}
                        except (ValueError, AgentUnavailable, KeyError, TypeError):
                            pending_response = {
                                "status": 409,
                                "result": {
                                    "detail": (
                                        "Connector maintenance refused; inspect activity status"
                                    )
                                },
                            }
                        pending_response.update(instance=instance, request=last)
                    # Preserve the result if publication fails. Replaying the
                    # operation could reserve twice or lose the original token.
                    await asyncio.to_thread(
                        _atomic, directory / RESPONSE, json.dumps(pending_response).encode()
                    )
                    pending_response = None
            except (OSError, ValueError, TypeError, RecursionError):
                pass
            await asyncio.sleep(interval)
    finally:
        try:
            (directory / STATUS).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("connector_activity_cleanup_failed", error_type=type(exc).__name__)


def read_status(directory):
    from filelock import FileLock, Timeout

    if not (directory / "connector.lock").exists():
        return {"state": "stopped", "idle_observed": False}
    lock = FileLock(directory / "connector.lock", timeout=0)
    try:
        lock.acquire()
    except Timeout:
        pass
    else:
        lock.release()
        return {"state": "stopped", "idle_observed": False}
    try:
        runtime = _read(directory / RUNTIME_FILE)
        record = _read(directory / STATUS)
        if (
            type(record.get("version")) is not int
            or record["version"] != 1
            or record.get("instance") != runtime["instance"]
            or type(record.get("published")) not in (float, int)
            or not 0 <= time.time() - record["published"] <= 5
            or any(
                type(record.get(key)) is not int or record[key] < 0
                for key in (*FIELDS, "unsupported_agents")
            )
            or type(record.get("reserved")) is not bool
            or type(record.get("idle_observed")) is not bool
        ):
            raise ValueError("Invalid activity record")
        return {"state": "running", **record}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"state": "unknown", "idle_observed": False}
