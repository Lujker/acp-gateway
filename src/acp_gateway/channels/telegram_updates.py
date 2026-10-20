"""Human-only update inventory; no agent tool can execute management actions."""

# ruff: noqa: RUF001

import asyncio

from acp_gateway.core import GatewayError
from acp_gateway.updates import RELEASE_ERRORS


class Updates:
    async def _update_notifications(self):
        while True:
            try:
                await self._update_notifications_once()
            except Exception as exc:
                self._log.warning(
                    "Telegram update notification unavailable", error_type=type(exc).__name__
                )
            await asyncio.sleep(3)

    async def _update_notifications_once(self):
        service = self.update_service
        for chat in sorted(self._chats & set(self.settings.allowed_user_ids)):
            actor = f"{self.namespace}:{chat}"
            for candidate in service.journal.notification_candidates(actor):
                row = await service.status(actor, candidate["id"])
                status = row["status"]
                if status not in {"succeeded", "failed", "unknown"}:
                    continue
                if service.journal.notice(row["id"], status) is not None:
                    continue
                # An interrupted or ambiguous Telegram send must not generate automatic duplicates.
                service.journal.notice(row["id"], status, "unknown")
                label = "диспетчер" if row["role"] == "dispatcher" else row["target"]
                state = {
                    "succeeded": "завершено",
                    "failed": "не завершено",
                    "unknown": "исход неизвестен",
                }[status]
                await self._send(
                    chat,
                    f"Обновление {label} до {row['version']}: {state}.\n"
                    f"Проверить: /update_status {row['id']}",
                )
                service.journal.notice(row["id"], status, "delivered")

    async def _updates(self, chat, command, argument):
        if command in {
            "/update_prepare",
            "/update_confirm",
            "/update_cancel",
            "/update_status",
            "/update_history",
        }:
            await self._update_action(chat, command, argument)
            return True
        if command not in {"/updates", "/updates_check"}:
            return False
        if self.update_inventory is None:
            raise GatewayError("Панель обновлений недоступна в этой конфигурации.")
        try:
            page = int(argument or "0")
            if not 0 <= page <= 100:
                raise ValueError
        except ValueError:
            raise GatewayError("Откройте /updates для просмотра обновлений.") from None
        lines = ["Обновления диспетчера и компьютеров"]
        if command == "/updates_check":
            try:
                await self.update_inventory.check_release()
            except RELEASE_ERRORS:
                lines += ["", "Не удалось проверить новую версию. Повторите проверку позже."]
        try:
            inventory = await self.update_inventory.targets(page=page)
        except (OSError, ValueError):
            raise GatewayError("Не удалось прочитать версии. Повторите /updates позже.") from None
        latest = inventory["latest"]
        lines += [
            "",
            f"Последняя проверенная stable версия: {latest}"
            if latest
            else "Доступная stable версия ещё не проверена.",
        ]
        for target in inventory["targets"]:
            label = "Диспетчер" if target["role"] == "dispatcher" else f"ПК {target['target']}"
            report = target["report"]
            if report is None:
                state = {
                    "offline": "не в сети",
                    "unsupported": "старому коннектору требуется локальное обновление",
                }.get(target["state"], "версия временно недоступна; обновите список")
                lines += ["", f"{label}: {state}"]
                continue
            lines += [
                "",
                f"{label}: {report.installed_version} ({report.manager}, {report.platform})",
            ]
            if target["newer_stable"]:
                lines += ["Доступна более новая stable версия."]
            if target.get("artifact_state") == "unpublished":
                lines += ["Артефакт этой stable версии для данного backend не опубликован."]
            elif target.get("artifact_state") == "unavailable":
                lines += ["Доступность артефакта временно не удалось проверить."]
            if report.manager == "docker":
                lines += [
                    "Docker обслуживает внешний updater на host, без Docker socket в gateway."
                ]
            if report.platform == "darwin":
                lines += ["Обновление macOS пока не поддерживается."]
        lines += [
            "",
            "Updater подготавливается автоматически при обычной настройке/запуске. "
            "Если цель недоступна, проверьте локальный opt-out и готовность host companion.",
        ]
        rows = [
            [
                ("🔎 Проверить версии", f"/updates_check {page}"),
                ("↻ Обновить список", f"/updates {page}"),
            ]
        ]
        if self.update_service is not None:
            for target in inventory["targets"]:
                report = target["report"]
                if report is not None and report.can_update and target["newer_stable"]:
                    label = "диспетчер" if target["role"] == "dispatcher" else target["target"]
                    rows += [
                        [
                            (
                                f"⬆ Обновить {label}",
                                f"/update_prepare {target['role']} {target['target']}",
                            )
                        ]
                    ]
            rows += [[("📋 История обновлений", "/update_history")]]
        if page:
            rows += [[("← Назад", f"/updates {page - 1}")]]
        if inventory["more"]:
            rows += [[("Далее →", f"/updates {page + 1}")]]
        await self._panel(chat, "\n".join(lines), rows + self._back_rows())
        return True

    async def _update_action(self, chat, command, argument):
        service = self.update_service
        if service is None:
            raise GatewayError("Управление обновлениями недоступно в этой конфигурации.")
        actor = f"{self.namespace}:{chat}"
        if command == "/update_history":
            rows = service.journal.recent(actor=actor)
            await self._panel(
                chat,
                "История обновлений вашего чата" + ("" if rows else " пуста."),
                [
                    [
                        (
                            f"{row['role']} {row['target']} → {row['version']}",
                            f"/update_status {row['id']}",
                        )
                    ]
                    for row in rows
                ]
                + self._back_rows(),
            )
            return
        if command == "/update_prepare":
            parts = argument.split()
            if len(parts) != 2:
                raise GatewayError("Выберите компьютер в /updates.")
            row = await service.prepare(actor, *parts)
        elif command == "/update_confirm":
            row = await service.confirm(actor, argument)
        elif command == "/update_cancel":
            row = service.cancel(actor, argument)
        else:
            row = await service.status(actor, argument)
        await self._update_operation_panel(chat, row)

    async def _update_operation_panel(self, chat, row):
        labels = {
            "awaiting_confirmation": "Ожидает вашего подтверждения",
            "dispatching": "Передаётся локальному updater",
            "queued": "Принято updater",
            "running": "Обновление выполняется",
            "succeeded": "Обновление завершено",
            "failed": "Обновление не завершено",
            "unknown": "Исход пока неизвестен",
            "cancelled": "Подтверждение закрыто",
        }
        reasons = {
            "disabled": "Обновление отключено локально.",
            "unsupported": "Способ установки не поддерживается.",
            "busy": (
                "Компьютер занят. После завершения работы нужна новая явная команда; "
                "отложенной установки нет."
            ),
            "expired": "Срок подтверждения истёк.",
            "stale": "Компьютер, подключение или версия изменились.",
            "conflict": "Параметры операции не совпали.",
            "version_not_allowed": "Версия не разрешена локальной политикой updater.",
            "failed": "Проверьте локальное состояние updater и службы.",
            "unknown": (
                "Не запускайте установку повторно. Обновление статуса не повторяет установку."
            ),
            "rolled_back": "Восстановлены предыдущий образ и база данных.",
            "worker_stopped": "Отдельный updater не запущен или недоступен.",
        }
        target = "Диспетчер" if row["role"] == "dispatcher" else f"Компьютер {row['target']}"
        text = (
            f"Обновление: {target}\n{row['current_version']} → {row['version']}\n"
            f"{labels[row['status']]}\nОперация: {row['id']}"
        )
        if row["reason"]:
            text += "\n" + reasons[row["reason"]]
        rows = []
        if row["status"] == "awaiting_confirmation":
            text += (
                "\n\nПодтвердите обновление именно этого компьютера до указанной версии. "
                "Активные задачи не прерываются: при занятости потребуется новая явная команда. "
                "После перезапуска продолжение диалога или attachment может быть потеряно; "
                "при необходимости начните новый диалог. Результат обновления сохранится в истории."
            )
            rows += [
                [
                    ("✅ Подтвердить", f"/update_confirm {row['id']}"),
                    ("Отмена", f"/update_cancel {row['id']}"),
                ]
            ]
        rows += [
            [
                ("↻ Обновить статус", f"/update_status {row['id']}"),
                ("📋 История", "/update_history"),
            ]
        ]
        await self._panel(chat, text, rows + self._back_rows())
