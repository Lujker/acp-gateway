"""One-button "wrap up and save a return point" for a dialogue; nothing is replayed."""

# ruff: noqa: RUF001
from acp_gateway.agents import SessionBusy
from acp_gateway.channels.telegram_dashboard import label, stamp
from acp_gateway.core import GatewayError

COMMANDS = {"/checkpoint", "/checkpoint_show", "/checkpoint_resume", "/checkpoint_new"}
RESUME_PROMPT = (
    "Продолжи работу с сохранённой точки возврата. Сначала сверь её с текущим "
    "состоянием рабочей папки, затем продолжай с первого незавершённого шага.\n\n"
    "Точка возврата:\n"
)


class CheckpointCards:
    async def _checkpoints(self, chat, command, argument):
        if command not in COMMANDS:
            return False
        number, _, mode = argument.partition(" ")
        if not number.isascii() or not number.isdecimal() or len(number) > 10:
            raise GatewayError("Откройте точку возврата кнопкой в карточке диалога.")
        record = self.core.owned_dialogue(int(number), channel=self.name, key=str(chat))
        if command == "/checkpoint":
            await self._request_checkpoint(chat, record, mode)
        elif command == "/checkpoint_show":
            await self._show_checkpoint(chat, record)
        else:
            await self._continue_checkpoint(chat, record, new=command == "/checkpoint_new")
        return True

    def _checkpoint_line(self, record):
        saved = self.core.checkpoint_of(record.id)
        if self.core.pending_checkpoint(record.id):
            return "📝 Точка возврата будет записана после текущей задачи."
        if saved:
            return f"📌 Точка возврата: {stamp(saved['created_at'])}"
        return None

    def _checkpoint_rows(self, record):
        rows = [[("📝 Завершить и сохранить", f"/checkpoint {record.id}")]]
        if self.core.checkpoint_of(record.id):
            rows.append([("📌 Точка возврата", f"/checkpoint_show {record.id}")])
        return rows

    async def _request_checkpoint(self, chat, record, mode):
        if mode not in {"", "after", "interrupt", "cancel"}:
            raise GatewayError("Откройте «Завершить и сохранить» кнопкой в карточке диалога.")
        if mode == "cancel":
            removed = self.core.cancel_pending_checkpoint(record.id)
            await self._panel(
                chat,
                "Запланированная точка возврата отменена."
                if removed
                else "Запланированной точки возврата нет.",
                [[("💬 Открыть диалог", f"/dialogue {record.id}")], *self._back_rows()],
            )
            return

        def notify(dialogue_id, error):
            self._queue_delivery(
                chat,
                f"Не удалось запросить точку возврата для диалога #{dialogue_id}: "
                f"{label(error, 200)}. Откройте диалог и попробуйте снова.",
            )

        try:
            result = await self.core.checkpoint(
                record.conversation,
                record.id,
                mode=mode or "now",
                delivery_namespace=self.namespace,
                notify=notify,
            )
        except SessionBusy:
            if mode:
                raise
            await self._panel(
                chat,
                f"Диалог #{record.id} сейчас выполняет задачу.\n\n"
                "⏳ После текущей задачи — агент закончит её, затем запишет точку возврата.\n"
                "⏹ Прервать и сохранить — текущая задача останавливается (сделанное не "
                "откатывается), затем агент записывает точку возврата.",
                [
                    [("⏳ После текущей задачи", f"/checkpoint {record.id} after")],
                    [("⏹ Прервать и сохранить", f"/checkpoint {record.id} interrupt")],
                    [("💬 Открыть диалог", f"/dialogue {record.id}")],
                    *self._back_rows(),
                ],
            )
            return
        if result["status"] == "scheduled":
            await self._panel(
                chat,
                f"Точка возврата для диалога #{record.id} будет запрошена, как только "
                "текущая задача завершится"
                + (" (остановка уже запрошена)." if mode == "interrupt" else ".")
                + "\nОтвет придёт отдельным сообщением и сохранится в карточке диалога.",
                [
                    [("Отменить", f"/checkpoint {record.id} cancel")],
                    [("💬 Открыть диалог", f"/dialogue {record.id}")],
                    *self._back_rows(),
                ],
            )
            return
        await self._show_started_job(chat, result["job"])

    async def _show_started_job(self, chat, job):
        status_text = self._work_text(job, accepted=True)
        message = await self._panel(chat, status_text, self._job_rows(job), editable=False)
        if self.settings.status_enabled:
            if len(self._statuses) >= 32:
                self._statuses.pop(next(iter(self._statuses)))
            self._statuses[job.id] = (chat, message.message_id, status_text, 0)

    async def _show_checkpoint(self, chat, record):
        saved = self.core.checkpoint_of(record.id)
        if not saved:
            raise GatewayError("У этого диалога нет сохранённой точки возврата.")
        header = (
            f"📌 Точка возврата · «{label(record.title or 'Без названия', 60)}» #{record.id}\n"
            f"Сохранена: {stamp(saved['created_at'])} · задача {saved['job_id']}\n\n"
        )
        rows = [
            [("▶ Продолжить в этом диалоге", f"/checkpoint_resume {record.id}")],
            [("➕ Продолжить в новом диалоге", f"/checkpoint_new {record.id}")],
            [("💬 Открыть диалог", f"/dialogue {record.id}")],
            *self._back_rows(),
        ]
        text = header + saved["text"]
        if len(text) > 3500:
            # The full text arrives as plain messages; the panel keeps the controls.
            await self._send(chat, text)
            text = header + "Полный текст — в сообщении выше."
        await self._panel(chat, text, rows, editable=False)

    async def _continue_checkpoint(self, chat, record, *, new):
        saved = self.core.checkpoint_of(record.id)
        if not saved:
            raise GatewayError("У этого диалога нет сохранённой точки возврата.")
        conversation = record.conversation
        if new:
            session = await self.core.new_session(conversation, cwd=record.cwd)
            self._set_route(chat, conversation.agent)
            target = session.acp_session_id
        else:
            target = record.acp_session_id
        job = await self.core.submit(
            conversation,
            RESUME_PROMPT + saved["text"],
            acp_session_id=target,
            create_session=False,
            delivery_namespace=self.namespace,
        )
        await self._show_started_job(chat, job)
