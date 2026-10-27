"""Source-bound shell cards, independent of the editable navigation panel."""

# Russian UI labels.
# ruff: noqa: RUF001

import asyncio
import time

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from acp_gateway.channels.telegram_dashboard import label
from acp_gateway.channels.telegram_format import system_chunks
from acp_gateway.core import GatewayError
from acp_gateway.core.errors import SelectionChanged
from acp_gateway.log import redact_text
from acp_gateway.shell_dispatcher import ShellDispatcher
from acp_gateway.storage import Conversation

STATES = {
    "dispatching": "Отправляется",
    "reserved": "Принята",
    "running": "Выполняется",
    "completed": "Завершена",
    "failed": "Ошибка",
    "cancelled": "Остановлена",
    "timed_out": "Время истекло",
    "refused": "Отклонена",
    "unknown": "Исход неизвестен",
    "expired": "Результат удалён по сроку хранения",
}
REASONS = {
    "safe_profile": "В safe-профиле прямой shell выключен.",
    "unsupported_platform": "Прямой shell пока не поддерживается на этой платформе.",
    "profile_unavailable": "Нужен доступный managed work-профиль Codex или Claude.",
    "session_unavailable": "Коннектор не подтвердил принадлежность диалога.",
    "cwd_not_allowed": "Папка не разрешена локальной политикой ПК.",
    "maintenance": "На ПК зарезервировано обслуживание.",
    "busy": "Достигнут предел параллельных shell-операций.",
    "not_found": "На ПК нет доступной записи с этим ID. Повторного запуска не было.",
    "connection_lost": "Связь потеряна. Можно запросить статус; повторного запуска нет.",
    "interrupted": "Выполнение прервано; результат требует проверки.",
    "cleanup_unverified": "Завершение дерева процессов не подтверждено.",
    "result_changed": "Не удалось получить согласованный полный результат.",
}


class ShellCards:
    def _shells(self):
        if not hasattr(self, "_shell_coordinator"):
            self._shell_coordinator = ShellDispatcher(
                self.core, self.connector_dispatcher, self.namespace
            )
        return self._shell_coordinator

    def _shell_selected(self, chat, row):
        try:
            conv = self._conversation(chat)
        except GatewayError:
            return False
        active = self.core.active_session(conv)
        return bool(
            conv.agent == row["agent"]
            and active
            and active.id == row["dialogue"]
            and active.acp_session_id == row["sid"]
        )

    def _shell_content(self, chat, row):
        output = row["output"]
        pages = max(1, (len(output) + 599) // 600)
        page = max(1, min(row["page"], pages))
        lines = [
            f"⌨ Shell · {STATES[row['state']]}",
            f"{row['agent']} @ {row['computer']} · #{row['dialogue']} · "
            f"{redact_text(row['title'])[:60]}",
            f"Папка: {redact_text(row['cwd'])[:160]}",
            f"Интерпретатор: {row['interpreter'] or 'не подтверждён'}",
            f"Команда: {row['command'][:160]}",
            f"ID: {row['id']}",
        ]
        if row["exit_code"] is not None:
            lines.append(f"Код выхода: {row['exit_code']}")
        if row["cleanup_verified"]:
            lines.append("Завершение процессов подтверждено.")
        if row["reason"]:
            lines.append(REASONS.get(row["reason"], "Причина: " + row["reason"]))
        if row["output_omitted"]:
            lines.append("Вывод пропущен из-за ограничения размера.")
        elif output:
            lines.extend([f"\nВывод · {page}/{pages}", output[(page - 1) * 600 : page * 600]])
        if row["transfer_state"] == "sent":
            lines.append(f"\nПередано агенту · задача {row['transfer_job']}")
        elif row["transfer_state"] in {"sending", "unknown"}:
            lines.append("\nИсход передачи агенту неизвестен. Проверьте историю задач.")
        else:
            lines.append("\nРезультат не отправлен агенту. Передача — отдельной кнопкой.")
        selected = self._shell_selected(chat, row)
        lines.append("Исходный диалог активен." if selected else "Исходный диалог не активен.")
        lines.extend(label(line, 100) for line in self._destination_text(chat).splitlines()[:4])
        op = row["id"]
        rows = [[("↻ Статус", f"/shell {op} {page}")]]
        if not row["remote_terminal"]:
            rows[0].append(("⏹ Остановить shell", f"/shell_cancel {op}"))
        navigation = []
        if page > 1:
            navigation.append(("← Вывод", f"/shell {op} {page - 1}"))
        if page < pages:
            navigation.append(("Вывод →", f"/shell {op} {page + 1}"))
        if navigation:
            rows.append(navigation)
        if not selected:
            rows.append([("💬 Сделать активным", f"/shell_select {op}")])
        elif row["cleanup_verified"] and row["transfer_state"] == "available":
            rows.append([("Передать агенту", f"/shell_send {op}")])
        if row["transfer_state"] == "sent":
            rows.append([("Задача агента", f"/work {row['transfer_job']}")])
        return "\n".join(lines), [*rows, [("Текущий контекст", "/context")], *self._back_rows()]

    async def _shell_card(self, chat, operation, *, explicit=False, page=None):
        coordinator = self._shells()
        row = coordinator.owned(operation, chat)
        async with coordinator.locks[row["id"]]:
            row = coordinator.owned(operation, chat)
            if not explicit and (row["card_state"] == "unknown" or not row["message_id"]):
                return
            if page is not None:
                row["page"] = page
            text, rows = self._shell_content(chat, row)
            # Redact the complete card before HTML escaping or slicing output.
            chunks = system_chunks(text)
            if len(chunks) != 1:
                raise GatewayError("Карточка shell слишком велика; откройте /shells.")
            text = chunks[0]
            bound = getattr(self, "_shell_bound", set())
            if not explicit and row["card_text"] == text and row["id"] in bound:
                return
            markup, tokens = self._panels.create(chat, rows, self._ui_context(chat))
            previous_state = row["card_state"]
            row["card_state"] = "sending"
            coordinator.save(row)
            try:
                if row["message_id"]:
                    try:
                        await self.bot.edit_message_text(
                            chat_id=chat,
                            message_id=row["message_id"],
                            text=text,
                            parse_mode="HTML",
                            reply_markup=markup,
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                    except TelegramBadRequest as exc:
                        if "message is not modified" in exc.message.lower():
                            pass
                        elif explicit and any(
                            reason in exc.message.lower()
                            for reason in (
                                "message to edit not found",
                                "message can't be edited",
                                "message cannot be edited",
                            )
                        ):
                            message = await self._send_payload(chat, text, markup=markup)
                            self._panels.retire_message(chat, row["message_id"])
                            row["message_id"] = message.message_id
                        else:
                            raise
                else:
                    message = await self._send_payload(chat, text, markup=markup)
                    row["message_id"] = message.message_id
            except TelegramRetryAfter:
                self._panels.discard(tokens)
                row["card_state"] = previous_state
                coordinator.save(row)
                raise
            except BaseException:
                self._panels.discard(tokens)
                row["card_state"] = "unknown"
                coordinator.save(row)
                raise
            self._panels.retire_message(chat, row["message_id"])
            self._panels.bind(tokens, row["message_id"])
            row.update(card_state="ready", card_text=text)
            coordinator.save(row)
            self._shell_bound = bound
            bound.add(row["id"])

    async def _shell_command(self, chat, text):
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0]
        coordinator = self._shells()
        if text.startswith("!"):
            row = await coordinator.start(self._conversation(chat), text[1:])
            await self._shell_card(chat, row["id"], explicit=True)
            return True
        if command == "/shells":
            try:
                page = max(1, int(argument or "1"))
            except ValueError:
                raise GatewayError("Укажите номер страницы shell-операций.") from None
            all_rows = [r for r in reversed(coordinator.rows()) if r["chat"] == str(chat)]
            pages = max(1, (len(all_rows) + 7) // 8)
            page = min(page, pages)
            rows = [
                [
                    (
                        f"{STATES[r['state']]} · #{r['dialogue']} · {r['command'][:25]}",
                        f"/shell {r['id']}",
                    )
                ]
                for r in all_rows[(page - 1) * 8 : page * 8]
            ]
            nav = []
            if page > 1:
                nav.append(("←", f"/shells {page - 1}"))
            if page < pages:
                nav.append(("→", f"/shells {page + 1}"))
            if nav:
                rows.append(nav)
            await self._panel(chat, f"Shell-операции · {page}/{pages}", rows + self._back_rows())
            return True
        if command not in {"/shell", "/shell_cancel", "/shell_select", "/shell_send"}:
            return False
        parts = argument.split()
        if not parts or len(parts) > (2 if command == "/shell" else 1):
            raise GatewayError("Выберите shell-операцию через /shells.")
        row = coordinator.owned(parts[0], chat)
        page = None
        if len(parts) == 2:
            try:
                page = max(1, int(parts[1]))
            except ValueError:
                raise GatewayError("Укажите номер страницы вывода.") from None
        if command == "/shell_select":
            conv = Conversation("telegram", str(chat), row["agent"])
            original = self.core.store.session(row["dialogue"])
            if (
                not original
                or original.conversation != conv
                or original.acp_session_id != row["sid"]
            ):
                raise GatewayError("Исходный диалог больше недоступен.")
            self.core.switch_dialogue(conv, original.id)
            self._set_route(chat, conv.agent)
        elif command == "/shell_send":
            context = self._ui_context(chat)

            def admission():
                if self._ui_context(chat) != context:
                    raise SelectionChanged("Диалог изменился; передача shell отменена.")

            await coordinator.transfer(
                chat, row["id"], self._conversation(chat), admission=admission
            )
        else:
            await coordinator.refresh(chat, row["id"], cancel=command == "/shell_cancel")
        await self._shell_card(chat, row["id"], explicit=True, page=page)
        return True

    async def _shell_updates(self):
        while True:
            delay = 3
            try:
                coordinator = self._shells()
                rows = coordinator.rows()
                if hasattr(self, "_shell_bound"):
                    self._shell_bound.intersection_update(row["id"] for row in rows)
                position = getattr(self, "_shell_scan_position", 0)
                batch = (rows[position:] + rows[:position])[:16]
                self._shell_scan_position = (position + len(batch)) % max(1, len(rows))
                for row in batch:
                    chat = int(row["chat"])
                    if chat not in self.settings.allowed_user_ids:
                        continue
                    if not row["remote_terminal"] and time.time() - row["updated"] >= (
                        10 if row["state"] == "unknown" else 2
                    ):
                        await coordinator.refresh(chat, row["id"])
                    await self._shell_card(chat, row["id"])
            except TelegramRetryAfter as exc:
                delay = min(60, max(3, exc.retry_after))
            except (TelegramAPIError, GatewayError, ConnectionError, OSError):
                delay = 10
                self._log.warning("Shell card refresh deferred")
            await asyncio.sleep(delay)
