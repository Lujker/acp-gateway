"""Approval details and advisory work status, independent of result delivery."""

# ruff: noqa: RUF001
import contextlib
import html
import json

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from acp_gateway.channels.telegram_dashboard import label, state
from acp_gateway.core import GatewayError
from acp_gateway.log import redact_text, redact_value
from acp_gateway.storage.records import utcnow


class WorkCards:
    def _work_text(self, job, *, accepted=False):
        elapsed = max(0, int(((job.finished_at or utcnow()) - job.created_at).total_seconds()))
        try:
            pending = any(a.job_id == job.id for a in self.core.pending_approvals(self.name))
        except GatewayError:
            pending = False
        status = "⏸ Ожидает подтверждения" if pending and not job.status.finished else state(job)
        if accepted and not job.status.finished and not pending:
            status = "📥 Принято диспетчером"
        lines = [
            f"Задача: {job.id}",
            status,
            "",
            f"Агент / ПК: {label(job.conversation.agent)}",
            f"Диалог: #{job.session_id}",
            f"Длительность: {elapsed} с",
        ]
        if job.status.finished:
            lines.append(f"Результат: /result {job.id}")
            if job.error:
                lines.append(f"Ошибка: {label(job.error, 300)}")
        else:
            lines.append("Управление — кнопками этой задачи.")
            work = self._work_snapshot(job)
            if work["text"]:
                lines += ["", "🧭 Ход работы", redact_text(work["text"])[-500:]]
            preview = redact_text(job.answer)[:1200]
            if preview:
                lines += ["", "📄 Ответ", preview[:700]]
        return "\n".join(lines)

    @staticmethod
    def _model_evidence(result):
        evidence = result.get("model_state") or {}
        status = evidence.get("state")
        lines = []
        hints = {
            "intent": "Смена модели начата; исход ещё не подтверждён.",
            "pending": "Смена модели принята, но применение ещё не подтверждено наблюдением.",
            "unknown": "Исход смены модели неизвестен. Не повторяйте смену автоматически.",
            "refused": "Смена модели отклонена; это не подтверждение применения.",
            "confirmation_required": "Смена модели требует вашего явного подтверждения.",
            "observed": "Модель подтверждена наблюдением агента.",
        }
        if status in hints:
            lines.append(hints[status])
        if evidence.get("deferred") and status == "pending":
            lines.append("Изменение отложено до завершения текущей работы.")
        if evidence.get("observed_model"):
            lines.append(
                f"Последнее наблюдение: {label(evidence.get('observed_provider'))}/"
                f"{label(evidence['observed_model'])} — сохранённые сведения."
            )
        native = result.get("native_observation") or {}
        if native.get("observed_model"):
            effort = f" {label(native['observed_effort'])}" if native.get("observed_effort") else ""
            provider = native.get("observed_provider")
            lines.append(
                "Последнее native-наблюдение: "
                + (f"{label(provider)}/" if provider else "")
                + f"{label(native['observed_model'])}{effort}"
                + ("" if provider else " (провайдер не сообщён)")
                + " — сохранённые сведения."
            )
        if status in {"intent", "unknown"}:
            lines.append("/model state показывает сохранённый исход без подключения к агенту.")
        if result.get("reconciliation") in {"unknown", "pending", "unavailable"}:
            lines.append("Проверка не подтвердила итог. Повторная смена модели не выполнялась.")
        if result.get("default_required"):
            lines.append(
                "Для этого диалога требуется выбрать или подтвердить модель перед отправкой."
            )
        return lines

    async def _approval_details(self, chat, argument):
        parts = argument.split()
        if not 1 <= len(parts) <= 2:
            raise GatewayError("Используйте /approval REQUEST_ID [PAGE].")
        page = parts[1] if len(parts) == 2 else "1"
        if not page.isascii() or not page.isdecimal() or len(page) > 6:
            raise GatewayError("Укажите номер страницы от 1.")
        approval = next(
            (
                a
                for a in self.core.pending_approvals(self.name)
                if a.id == parts[0] and chat in self._approval_chats(a)
            ),
            None,
        )
        if approval is None:
            raise GatewayError("Подтверждение завершено, просрочено или недоступно этому чату.")
        await self._approval(chat, approval, page=int(page))

    async def _approval(self, chat, approval, *, page=None):
        options = tuple(
            o for o in approval.request.options if o.kind in {"allow_once", "reject_once"}
        )
        if not options:
            return
        raw = redact_text(
            json.dumps(redact_value(approval.request.raw_input), ensure_ascii=False, indent=2)
        )
        # Bound each message, but preserve the complete input across detail pages.
        chunks = [raw[i : i + 1000] for i in range(0, len(raw), 1000)] or ["null"]
        if page is not None and not 1 <= page <= len(chunks):
            raise GatewayError(f"Укажите страницу от 1 до {len(chunks)}.")
        conversation = approval.conversation
        origin = (
            "этот чат"
            if conversation.channel == self.name
            else (f"{conversation.channel}:{conversation.key}")
        )
        job = self.core.job(approval.job_id)
        body = (
            f"Подтверждение · {label(conversation.agent)}\n"
            f"Источник: {label(origin)}\nЗадача: {approval.job_id} · Диалог: #{job.session_id}\n"
            f"Действие: {label(approval.request.title, 300)}\n"
            f"Тип: {label(approval.request.kind)}\n"
            f"Срок: {approval.expires_at:%Y-%m-%d %H:%M:%S} UTC\n\n"
        )
        if page is None:
            body += raw[:700]
            if len(raw) > 700:
                body += "\n… Ввод сокращён. Откройте полный ввод перед решением."
        else:
            body += f"Полный ввод · страница {page}/{len(chunks)}\n{chunks[page - 1]}"
        rows = []
        if page is None:
            rows.append([("📄 Полный ввод", f"/approval {approval.id} 1")])
        else:
            navigation = []
            if page > 1:
                navigation.append(("← Ввод", f"/approval {approval.id} {page - 1}"))
            if page < len(chunks):
                navigation.append(("Ввод →", f"/approval {approval.id} {page + 1}"))
            if navigation:
                rows.append(navigation)
        if conversation.channel == self.name:
            rows.append([("Задача", f"/job {job.id}")])
        markup, tokens = self._panels.create(chat, rows, self._ui_context(chat))
        decisions = [
            InlineKeyboardButton(
                text="✅ Разрешить один раз" if o.kind == "allow_once" else "❌ Отклонить",
                callback_data=f"a:{approval.id}:{i}",
            )
            for i, o in enumerate(options)
        ]
        markup = InlineKeyboardMarkup(inline_keyboard=[decisions, *markup.inline_keyboard])
        message = await self._send(chat, body, markup=markup)
        key = (chat, message.message_id)
        self._panels.bind(tokens, message.message_id)
        self._buttons[key] = (approval.id, tuple(o.option_id for o in options))
        self._approval_texts[key] = body
        while len(self._buttons) > 256:
            oldest = next(iter(self._buttons))
            self._buttons.pop(oldest)
            self._approval_texts.pop(oldest, None)
            self._panels.retire_message(*oldest)
        # A human can settle another copy while this Bot API send is in flight.
        try:
            pending = any(a.id == approval.id for a in self.core.pending_approvals(self.name))
        except GatewayError:
            pending = False
        if not pending:
            audits = self.core.store.approval_audit(approval.job_id)
            outcome = next((a.outcome for a in audits if a.id == approval.id), "unavailable")
            await self._settle_approval_card(key, outcome)

    async def _settle_approval_card(self, key, outcome):
        self._buttons.pop(key, None)
        self._panels.retire_message(*key)
        body = self._approval_texts.pop(key, "")
        outcome_text = {
            "approved": "✅ Разрешено один раз",
            "rejected": "❌ Отклонено",
            "timed_out": "⌛ Срок подтверждения истёк",
            "cancelled": "⏹ Подтверждение отменено",
        }.get(outcome, "Подтверждение уже завершено или недоступно.")
        try:
            await self.bot.edit_message_text(
                chat_id=key[0],
                message_id=key[1],
                text=html.escape(f"{body}\n\nИтог: {outcome_text}", quote=False),
                parse_mode="HTML",
                reply_markup=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError:
            # No replay of ambiguous edits; the decision is already durable.
            with contextlib.suppress(TelegramAPIError):
                await self.bot.edit_message_reply_markup(
                    chat_id=key[0], message_id=key[1], reply_markup=None
                )
