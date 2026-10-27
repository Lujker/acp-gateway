"""Durable Telegram shell intents. Recovery reads status; it never replays start."""

# Russian UI text.
# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from uuid import UUID, uuid4

from acp_gateway.agents.errors import AgentError, SessionBusy
from acp_gateway.connectors.protocol import ShellRequest, ShellResult
from acp_gateway.core import GatewayError
from acp_gateway.core.errors import (
    MaintenanceBlocked,
    PolicyDenied,
    SelectionChanged,
    UnknownSession,
)
from acp_gateway.log import redact_text
from acp_gateway.storage import Conversation

INDEX = "shell_operations"
PREFIX = "shell_operation:"
TERMINAL = {"completed", "failed", "cancelled", "timed_out", "refused", "expired"}


class ShellDispatcher:
    def __init__(self, core, dispatcher, namespace):
        self.core, self.dispatcher, self.namespace = core, dispatcher, namespace
        self.locks = defaultdict(asyncio.Lock)

    def rows(self):
        keys = self.core.store.channel_state(self.namespace, INDEX, [])
        return [
            row
            for key in keys
            if (row := self.core.store.channel_state(self.namespace, PREFIX + key))
        ]

    def save(self, row):
        self.core.store.set_channel_state(self.namespace, PREFIX + row["id"], row)

    def owned(self, operation, chat):
        try:
            key = UUID(str(operation)).hex
        except (ValueError, TypeError):
            raise GatewayError("Shell operation unavailable for this chat.") from None
        row = self.core.store.channel_state(self.namespace, PREFIX + key)
        if not row or row["chat"] != str(chat):
            raise GatewayError("Shell operation unavailable for this chat.")
        return row

    def recover(self):
        changes = {}
        for row in self.rows():
            dirty = False
            if row["state"] == "dispatching":
                row.update(state="unknown", reason="interrupted", remote_terminal=False)
                dirty = True
            if row["transfer_state"] == "sending":
                row["transfer_state"] = "unknown"
                dirty = True
            if row["card_state"] == "sending":
                row["card_state"] = "unknown"
                dirty = True
            if dirty:
                changes[PREFIX + row["id"]] = row
        if changes:
            self.core.store.set_channel_states(self.namespace, changes)

    def prune(self):
        rows = self.rows()
        keep, changes = [], {}
        for row in rows:
            lock = self.locks.get(row["id"])
            if (
                row["state"] in TERMINAL
                and row["transfer_state"] != "sending"
                and row["created"] < time.time() - 7 * 86400
                and not (lock and lock.locked())
            ):
                changes[PREFIX + row["id"]] = None
                self.locks.pop(row["id"], None)
            else:
                keep.append(row["id"])
        if changes:
            changes[INDEX] = keep
            self.core.store.set_channel_states(self.namespace, changes)
        return keep

    async def start(self, conv, command):
        if conv.channel != "telegram" or not conv.key.isascii() or not conv.key.isdecimal():
            raise GatewayError("Direct shell requires a private Telegram conversation.")
        if not command.strip() or len(command) > 4096 or "\x00" in command:
            raise GatewayError("Введите shell-команду после ! (до 4096 символов).")
        client = self.core.agent(conv.agent)
        profile = client.profile
        if self.dispatcher is None or profile.backend != "connector":
            raise GatewayError(
                "Прямой shell доступен через подключённый ПК с managed work-профилем."
            )
        pc = next(
            (p for p in self.dispatcher.status() if p["computer_id"] == profile.computer_id), None
        )
        if not pc or not pc["connected"] or not pc["enabled"]:
            raise GatewayError("ПК не подключён или доступ выключен. Команда не отправлена.")
        if not pc.get("shell_operations"):
            raise GatewayError(
                "Коннектор не поддерживает прямой shell. Обновите его; "
                "команда не отправлена агенту."
            )
        record = self.core.active_session(conv)
        if record is None:
            raise GatewayError("Выберите диалог перед выполнением shell-команды.")
        with self.core.control_operation():
            self.core.work_rate_limiter.check(conv.channel)
            keys = self.prune()
            if len(keys) >= 1000:
                raise GatewayError("Достигнут лимит записей shell. Новая команда не запускалась.")
            row = {
                "id": uuid4().hex,
                "chat": conv.key,
                "agent": conv.agent,
                "computer": profile.computer_id,
                "alias": profile.alias,
                "dialogue": record.id,
                "sid": record.acp_session_id,
                "cwd": record.cwd,
                "title": record.title or "Без названия",
                "command": redact_text(command),
                "created": time.time(),
                "updated": time.time(),
                "state": "dispatching",
                "reason": "",
                "interpreter": "",
                "output": "",
                "exit_code": None,
                "output_omitted": False,
                "cleanup_verified": False,
                "remote_terminal": False,
                "transfer_state": "available",
                "transfer_job": None,
                "message_id": None,
                "card_state": "new",
                "card_text": "",
                "page": 1,
            }
            self.core.store.set_channel_states(
                self.namespace, {INDEX: [*keys, row["id"]], PREFIX + row["id"]: row}
            )
            async with self.locks[row["id"]]:
                return await self._exchange(row, "start", command=command)

    async def refresh(self, chat, operation, *, cancel=False):
        row = self.owned(operation, chat)
        async with self.locks[row["id"]]:
            row = self.owned(operation, chat)
            if row["remote_terminal"]:
                return row
            if self.core.maintenance_status()["reserved"] and not cancel:
                return row
            with self.core.control_operation():
                return await self._exchange(row, "cancel" if cancel else "status")

    async def _rpc(self, row, action, *, command=None, offset=0):
        frame = ShellRequest(
            epoch=uuid4(),
            request=uuid4(),
            operation=UUID(row["id"]),
            actor="telegram:" + row["chat"],
            expires=time.time() + 30,
            action=action,
            offset=offset,
            **(
                {
                    "alias": row["alias"],
                    "session_id": row["sid"],
                    "cwd": row["cwd"],
                    "command": command,
                }
                if action == "start"
                else {}
            ),
        )
        result = await self.dispatcher.shell(row["computer"], frame)
        if not isinstance(result, ShellResult) or result.operation != frame.operation:
            raise ValueError("shell result identity mismatch")
        return result

    async def _exchange(self, row, action, *, command=None):
        try:
            async with asyncio.timeout(30):
                result = await self._rpc(row, action, command=command)
                if result.state == "refused":
                    row.update(
                        state="refused" if action == "start" else "unknown",
                        reason=result.reason,
                        remote_terminal=True,
                        output="",
                    )
                elif result.state in {"reserved", "running", "expired"}:
                    row.update(
                        state=result.state,
                        reason=result.reason,
                        interpreter=result.interpreter,
                        output="",
                        remote_terminal=result.state == "expired",
                    )
                else:
                    if result.offset != 0:
                        raise ValueError("shell result starts at a nonzero offset")
                    output = result.output
                    signature = (
                        result.state,
                        result.exit_code,
                        result.total,
                        result.interpreter,
                        result.cleanup_verified,
                        result.output_omitted,
                    )
                    while len(output) < result.total:
                        part = await self._rpc(row, "status", offset=len(output))
                        if (
                            (
                                part.state,
                                part.exit_code,
                                part.total,
                                part.interpreter,
                                part.cleanup_verified,
                                part.output_omitted,
                            )
                            != signature
                            or not part.output
                            or part.offset != len(output)
                        ):
                            raise ValueError("shell result changed while reading")
                        output += part.output
                    # No raw partial output is persisted or shown. The connector
                    # may not know a secret registered only on the dispatcher.
                    output = redact_text(output)
                    omitted = result.output_omitted or len(output) > 65_536
                    row.update(
                        state=result.state,
                        reason=result.reason,
                        interpreter=result.interpreter,
                        output="" if omitted else output,
                        exit_code=result.exit_code,
                        output_omitted=omitted,
                        cleanup_verified=result.cleanup_verified,
                        remote_terminal=True,
                    )
        except asyncio.CancelledError:
            row.update(state="unknown", reason="interrupted", remote_terminal=False, output="")
            self.save(row)
            raise
        except (AgentError, ConnectionError, TimeoutError, BufferError, OSError):
            row.update(state="unknown", reason="connection_lost", remote_terminal=False, output="")
        except (ValueError, TypeError):
            row.update(state="unknown", reason="result_changed", remote_terminal=True, output="")
        row["updated"] = time.time()
        self.save(row)
        return row

    async def transfer(self, chat, operation, conv, *, admission):
        row = self.owned(operation, chat)
        async with self.locks[row["id"]]:
            row = self.owned(operation, chat)
            if row["transfer_state"] == "sent":
                return self.core.owned_job(row["transfer_job"], channel="telegram", key=str(chat))
            if row["transfer_state"] != "available":
                raise GatewayError(
                    "Исход передачи неизвестен. Проверьте историю задач; "
                    "автоматического повтора нет."
                )
            if (
                row["state"] not in {"completed", "failed", "cancelled", "timed_out"}
                or not row["cleanup_verified"]
            ):
                raise GatewayError(
                    "Дождитесь подтверждённого результата shell перед передачей агенту."
                )
            target = Conversation("telegram", row["chat"], row["agent"])
            active = self.core.active_session(target)
            if (
                conv != target
                or active is None
                or active.id != row["dialogue"]
                or active.acp_session_id != row["sid"]
            ):
                raise SelectionChanged("Сначала сделайте исходный диалог активным.")
            revision = self.core.store.active_revision(target)

            def guard():
                admission()
                current = self.core.active_session(target)
                if (
                    self.core.store.active_revision(target) != revision
                    or current is None
                    or current.id != row["dialogue"]
                    or current.acp_session_id != row["sid"]
                    or current.cwd != row["cwd"]
                ):
                    raise SelectionChanged("Диалог изменился; передача shell отменена.")

            payload = (
                f"Результат shell-операции {row['id']}\nКоманда: {row['command']}\n"
                f"Папка: {row['cwd']}\nСостояние: {row['state']} · код {row['exit_code']}\n\n"
                + (
                    "[Вывод пропущен из-за ограничения размера]"
                    if row["output_omitted"]
                    else row["output"]
                )
            )
            self.core.policy.check_prompt(payload)
            with self.core.control_operation():
                guard()
                row["transfer_state"] = "sending"
                self.save(row)
                try:
                    job = await self.core.submit(
                        target,
                        payload,
                        acp_session_id=row["sid"],
                        create_session=False,
                        delivery_namespace=self.namespace,
                        admission=guard,
                    )
                except (
                    SessionBusy,
                    PolicyDenied,
                    SelectionChanged,
                    MaintenanceBlocked,
                    UnknownSession,
                ):
                    row["transfer_state"] = "available"
                    self.save(row)
                    raise
                except BaseException:
                    row["transfer_state"] = "unknown"
                    self.save(row)
                    raise
                row.update(transfer_state="sent", transfer_job=job.id)
                self.save(row)
                return job
