"""Read-only dispatcher dashboards, scoped to the requesting private chat."""

# ruff: noqa: RUF001
from acp_gateway.connectors.diagnostics import computer_diagnostics
from acp_gateway.core import GatewayError
from acp_gateway.log import redact_text


def label(value, limit=80):
    return " ".join(redact_text(str(value)).split())[:limit]


def stamp(value):
    return str(value).replace("T", " ").replace("+00:00", " UTC") if value else "—"


def state(job):
    return {
        "running": "⏳ Выполняется",
        "completed": "✅ Завершена",
        "failed": "❌ Ошибка",
        "cancelled": "⏹ Отменена",
        "interrupted": "⚠ Прервана",
    }[job.status.value]


class Dashboard:
    async def _dashboard(self, chat, command, argument):
        if command == "/jobs":
            parts = argument.split()
            active = bool(parts and parts[0] == "active")
            if active:
                parts.pop(0)
            before = None
            if parts:
                if len(parts) != 2 or parts[0] != "before":
                    raise GatewayError("Используйте /jobs [active] [before JOB_ID].")
                before = parts[1]
            result = self.core.job_history("telegram", str(chat), active=active, before=before)
            lines = ["Задачи этого чата" + (" · активные" if active else " · последние")]
            rows = []
            for job in result["jobs"]:
                lines.append(
                    f"{state(job)} · {label(job.conversation.agent, 35)} · #{job.session_id}\n"
                    f"{stamp(job.created_at)} · {job.id}"
                )
                rows.append(
                    [
                        (
                            f"{state(job)} · {label(job.conversation.agent, 25)} · {job.id[:8]}",
                            f"/job {job.id}",
                        )
                    ]
                )
            if not rows:
                lines.append("Задач пока нет." if not active else "Активных задач нет.")
            prefix = "/jobs active" if active else "/jobs"
            if result["next_before"]:
                rows.append([("Еще →", f"{prefix} before {result['next_before']}")])
            rows += [
                [
                    ("↻ К новым", prefix),
                    ("Все задачи" if active else "Активные", "/jobs" if active else "/jobs active"),
                ],
                *self._back_rows(),
            ]
            await self._panel(chat, "\n\n".join(lines), rows)
        elif command == "/job":
            await self._job_panel(chat, argument)
        elif command == "/computers":
            await self._computers_panel(chat, argument)
        elif command in {"/result", "/delivery"}:
            job = self.core.owned_job(argument, channel="telegram", key=str(chat))
            if command == "/result":
                if not self._queue_delivery(chat, job.answer or job.error or state(job), job):
                    raise GatewayError("Доставка занята. Повторите позже.")
            else:
                delivery = self.core.store.deliveries.get(self.namespace, job.id)
                text = (
                    f"Доставка · {job.id}\nСостояние: {delivery['state']}\n"
                    f"Подтверждено фрагментов: {delivery['next_chunk']}"
                    if delivery
                    else "Записи автоматической доставки нет."
                )
                await self._panel(
                    chat, text, [[("← Задача", f"/job {job.id}")], *self._back_rows()]
                )
        else:
            return False
        return True

    async def _job_panel(self, chat, job_id):
        job = self.core.owned_job(job_id, channel="telegram", key=str(chat))
        lines = [
            f"Задача {job.id}",
            state(job),
            f"Агент: {label(job.conversation.agent)}",
            f"Диалог: #{job.session_id}",
            f"Начало: {stamp(job.created_at)}",
        ]
        if job.finished_at:
            lines.append(f"Завершение: {stamp(job.finished_at)}")
        if job.error:
            lines.append(f"Ошибка: {label(job.error, 300)}")
        rows = [
            [("↻ Обновить", f"/job {job.id}"), ("📄 Результат", f"result:{job.id}")],
            [("Доставка", f"/delivery {job.id}")],
        ]
        try:
            client = self.core.agent(job.conversation.agent)
        except GatewayError:
            client = None
        if not job.status.finished and client and getattr(client, "supports_cancel", True):
            rows.append([("⏹ Остановить задачу", f"cancel:{job.id}")])
        rows += [[("← Задачи", "/jobs")], *self._back_rows()]
        await self._panel(chat, "\n".join(lines), rows)

    async def _computers_panel(self, chat, argument):
        if self.connector_dispatcher is None:
            await self._panel(
                chat,
                "Подключение компьютеров не настроено.",
                [[("Агенты", "/agent")], *self._back_rows()],
            )
            return
        snapshot = computer_diagnostics(self.core, self.connector_dispatcher)
        computers = sorted(snapshot["computers"], key=lambda row: row["computer_id"])
        parts = argument.split()
        if parts and parts[0] == "id":
            if len(parts) != 2:
                raise GatewayError("Используйте /computers [PAGE | COMPUTER_ID].")
            computer = next((c for c in computers if c["computer_id"] == parts[1]), None)
            if computer is None:
                raise GatewayError("Компьютер не найден. Обновите список.")
            cid = computer["computer_id"]
            lines = [
                f"Компьютер · {label(computer['display_name'])}",
                f"ID: {label(cid)}",
                self._computer_state(computer),
                f"Подключение: {stamp(computer.get('connected_at'))}",
                f"Отключение: {stamp(computer.get('disconnected_at'))}",
            ]
            rows = []
            for route in snapshot["routes"]:
                if route["computer_id"] != cid:
                    continue
                readiness = {
                    "ready": "готов",
                    "offline": "компьютер не подключен",
                    "unadvertised": "коннектор не объявил агента",
                    "not_initialized": "на связи; готовность ещё не проверена",
                }[route["status"]]
                if route.get("last_stream_error") and not route["agent_ready"]:
                    readiness = "на связи; последняя попытка ACP завершилась ошибкой"
                lines.append(f"{label(route['address'])}: {readiness}")
                if computer["connected"] and route["advertised"]:
                    rows.append(
                        [(f"Выбрать {label(route['alias'], 35)}", f"/agent {route['address']}")]
                    )
            if not any(r["computer_id"] == cid for r in snapshot["routes"]):
                lines.append("Агенты пока не обнаружены.")
            rows += [
                [("↻ Обновить", f"/computers id {cid}"), ("← Компьютеры", "/computers")],
                *self._back_rows(),
            ]
        else:
            if len(parts) > 1 or (parts and len(parts[0]) > 6):
                raise GatewayError("Используйте /computers [PAGE].")
            if parts and (not parts[0].isascii() or not parts[0].isdecimal()):
                raise GatewayError("Используйте /computers [PAGE] или /computers id COMPUTER_ID.")
            page = int(argument or "1")
            pages = max(1, (len(computers) + 9) // 10)
            if not 1 <= page <= pages:
                raise GatewayError(f"Выберите страницу от 1 до {pages}.")
            lines = [f"Компьютеры · {len(computers)} · страница {page}/{pages}"]
            rows = []
            for computer in computers[(page - 1) * 10 : page * 10]:
                cid = computer["computer_id"]
                lines.append(
                    f"{label(computer['display_name'], 45)} ({label(cid, 35)}) · "
                    f"{self._computer_state(computer)}"
                )
                rows.append([(label(computer["display_name"], 45), f"/computers id {cid}")])
            if not computers:
                lines.append("Компьютеры пока не зарегистрированы.")
            navigation = []
            if page > 1:
                navigation.append(("← Назад", f"/computers {page - 1}"))
            if page < pages:
                navigation.append(("Далее →", f"/computers {page + 1}"))
            rows += ([navigation] if navigation else []) + [
                [("↻ Обновить", f"/computers {page}")],
                *self._back_rows(),
            ]
        await self._panel(chat, "\n".join(lines), rows)

    @staticmethod
    def _computer_state(computer):
        if not computer["enabled"]:
            return "⛔ Доступ выключен"
        return "🟢 На связи" if computer["connected"] else "⚪ Не подключен"
