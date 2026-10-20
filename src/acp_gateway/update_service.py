"""Confirmed human update operations and a local, durable executor mailbox."""

import re
import sqlite3
import time
from uuid import UUID, uuid4

from acp_gateway.agents.errors import AgentError
from acp_gateway.connectors.protocol import UpdateOperation, UpdateRequest
from acp_gateway.core import GatewayError
from acp_gateway.update_delivery import official_image
from acp_gateway.update_journal import ACTIVE, TERMINAL, Journal
from acp_gateway.updates import installation_report


def public_operation(row):
    return UpdateOperation(operation=UUID(row["id"]), status=row["status"], reason=row["reason"])


class LocalUpdates:
    def __init__(self, config, *, role, target, idle_check=None):
        self.config, self.role, self.target = config, role, target
        self.idle_check = idle_check
        root = config.settings.updates.spool_dir or (
            config.settings.resolved_data_dir() / "update-spool"
        )
        self.path = root / "operations.db"

    def available(self, backend=None):
        if not self.config.settings.updates.enabled or not self.path.is_file():
            return False
        journal = None
        try:
            journal = Journal(self.path)
            worker = journal.worker()
            return bool(
                isinstance(worker, dict)
                and worker.get("role") == self.role
                and worker.get("target") == self.target
                and worker.get("backend") in {"uv", "docker"}
                and (backend is None or worker.get("backend") == backend)
                and type(worker.get("published")) in {int, float}
                and 0 <= time.time() - worker.get("published", 0) <= 10
            )
        except (OSError, ValueError, sqlite3.Error):
            return False
        finally:
            if journal is not None:
                journal.close()

    def report(self):
        report = installation_report(role=self.role)
        supported = report.platform in {"linux", "win32"} and (
            report.manager == "docker" or (report.manager == "uv" and report.source == "registry")
        )
        backend = "docker" if report.manager == "docker" else "uv"
        return report.model_copy(update={"can_update": supported and self.available(backend)})

    def version_allowed(self, version):
        journal = Journal(self.path)
        try:
            worker = journal.worker()
            return bool(
                isinstance(worker, dict)
                and (
                    worker.get("backend") == "uv"
                    or (
                        worker.get("backend") == "docker"
                        and (
                            version in worker.get("versions", [])
                            or (
                                worker.get("delivery") == "official"
                                and official_image(version) is not None
                            )
                        )
                    )
                )
            )
        except (OSError, ValueError, sqlite3.Error):
            return False
        finally:
            journal.close()

    def request(self, request):
        journal = Journal(self.path)
        try:
            try:
                row = journal.get(request.operation)
            except ValueError:
                row = None
            if row is not None:
                if (row["actor"], row["version"], row["current_version"]) != (
                    request.actor,
                    request.target_version,
                    request.current_version,
                ):
                    return UpdateOperation(
                        operation=request.operation, status="failed", reason="conflict"
                    )
                return public_operation(row)
            if request.action == "status":
                return UpdateOperation(operation=request.operation, status="not_found")
            reason = ""
            if not self.config.settings.updates.enabled:
                reason = "disabled"
            elif not 0 < request.expires - time.time() <= 300:
                reason = "expired"
            elif not self.available():
                reason = "worker_stopped"
            elif self.idle_check is not None and not self.idle_check():
                reason = "busy"
            elif not self.version_allowed(request.target_version):
                reason = "version_not_allowed"
            try:
                row = journal.create(
                    key=request.operation,
                    actor=request.actor,
                    role=self.role,
                    target=self.target,
                    version=request.target_version,
                    current_version=request.current_version,
                    binding=str(request.epoch),
                    expires=request.expires,
                    status="failed" if reason else "queued",
                )
            except sqlite3.IntegrityError:
                try:
                    existing = journal.get(request.operation)
                except ValueError:
                    existing = journal.create(
                        key=request.operation,
                        actor=request.actor,
                        role=self.role,
                        target=self.target,
                        version=request.target_version,
                        current_version=request.current_version,
                        binding=str(request.epoch),
                        expires=request.expires,
                        status="failed",
                    )
                    journal.transition(existing["id"], ("failed",), "failed", "busy")
                if (existing["actor"], existing["version"], existing["current_version"]) != (
                    request.actor,
                    request.target_version,
                    request.current_version,
                ):
                    return UpdateOperation(
                        operation=request.operation, status="failed", reason="conflict"
                    )
                return public_operation(journal.get(existing["id"]))
            if reason:
                journal.transition(row["id"], ("failed",), "failed", reason)
            return public_operation(journal.get(row["id"]))
        finally:
            journal.close()


class UpdateService:
    def __init__(self, inventory, journal, local):
        self.inventory, self.journal, self.local = inventory, journal, local

    async def _target(self, role, target):
        import asyncio

        if role == "dispatcher" and target == "dispatcher":
            return await asyncio.to_thread(self.local.report), "local"
        dispatcher = self.inventory.dispatcher
        if role != "connector" or dispatcher is None:
            raise GatewayError("Компьютер недоступен для обновления.")
        report = await dispatcher.update_info(target)
        registration = dispatcher.active.get(target)
        if registration is None or not registration.relay.update_operations:
            raise GatewayError("Коннектор не поддерживает удалённое обновление.")
        return report, f"{registration.connection_id}:{registration.generation}"

    async def prepare(self, actor, role, target):
        version = self.inventory.latest
        if not version or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise GatewayError("Сначала проверьте доступную stable версию в /updates.")
        report, binding = await self._target(role, target)
        if report is None or not report.can_update:
            raise GatewayError("Обновление не включено локально или отдельный updater не запущен.")
        available, _ = await self.inventory.available_version(report)
        if available != version:
            raise GatewayError(
                "Опубликованный артефакт этой версии для выбранной установки недоступен."
            )
        if role == "dispatcher" and not self.local.version_allowed(version):
            raise GatewayError("Эта версия ещё не подготовлена в локальной политике updater.")
        from packaging.version import Version

        if Version(version) <= Version(report.installed_version):
            raise GatewayError("Более новой stable версии нет.")
        try:
            return self.journal.create(
                actor=actor,
                role=role,
                target=target,
                version=version,
                current_version=report.installed_version,
                binding=binding,
                expires=time.time() + 300,
            )
        except sqlite3.IntegrityError:
            raise GatewayError(
                "Для этого компьютера уже есть операция. Откройте историю обновлений."
            ) from None

    def owned(self, actor, key):
        try:
            return self.journal.get(key, actor=actor)
        except ValueError:
            raise GatewayError("Эта операция обновления недоступна вашему чату.") from None

    def cancel(self, actor, key):
        row = self.owned(actor, key)
        self.journal.transition(row["id"], ("awaiting_confirmation",), "cancelled")
        return self.owned(actor, key)

    def _request(self, row, action):
        return UpdateRequest(
            epoch=uuid4(),
            request=uuid4(),
            operation=UUID(row["id"]),
            action=action,
            actor=row["actor"],
            target_version=row["version"],
            current_version=row["current_version"],
            expires=float(row["expires"]),
        )

    async def _dispatch(self, row, action):
        request = self._request(row, action)
        if row["role"] == "dispatcher":
            return self.local.request(request)
        return await self.inventory.dispatcher.update_operation(
            row["target"], row["binding"], request
        )

    def _save_result(self, row, result):
        if result.operation.hex != row["id"]:
            raise ValueError("Update operation response mismatch")
        status, reason = result.status, result.reason
        if status in {"awaiting_confirmation", "dispatching"}:
            raise ValueError("Invalid executor operation state")
        if status == "not_found":
            status, reason = "unknown", "unknown"
        self.journal.transition(row["id"], ACTIVE, status, reason)

    async def confirm(self, actor, key):
        row = self.owned(actor, key)
        if row["status"] != "awaiting_confirmation":
            return row
        if row["expires"] <= time.time():
            self.journal.transition(row["id"], ("awaiting_confirmation",), "cancelled", "expired")
            return self.owned(actor, key)
        report, binding = await self._target(row["role"], row["target"])
        if row["expires"] <= time.time():
            self.journal.transition(row["id"], ("awaiting_confirmation",), "cancelled", "expired")
            return self.owned(actor, key)
        if (
            binding != row["binding"]
            or report is None
            or not report.can_update
            or report.installed_version != row["current_version"]
        ):
            self.journal.transition(row["id"], ("awaiting_confirmation",), "cancelled", "stale")
            return self.owned(actor, key)
        if self.journal.transition(row["id"], ("awaiting_confirmation",), "dispatching"):
            try:
                self._save_result(row, await self._dispatch(row, "execute"))
            except (AgentError, OSError, ValueError, TimeoutError, BufferError):
                self.journal.transition(row["id"], ("dispatching",), "unknown", "unknown")
        return self.owned(actor, key)

    async def status(self, actor, key):
        row = self.owned(actor, key)
        if row["status"] not in TERMINAL and row["status"] != "awaiting_confirmation":
            try:
                self._save_result(row, await self._dispatch(row, "status"))
            except (AgentError, OSError, ValueError, TimeoutError, BufferError):
                self.journal.transition(row["id"], ACTIVE, "unknown", "unknown")
        return self.owned(actor, key)
