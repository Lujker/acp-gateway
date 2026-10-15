"""Private, allowlisted Telegram chats and authenticated human approvals."""

# Russian UI labels and button symbols intentionally contain Cyrillic characters.
# ruff: noqa: RUF001

import asyncio
import contextlib
import hashlib
import html
import json
import time
from collections import deque

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import GetUpdates
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    LinkPreviewOptions,
    MenuButtonCommands,
    Message,
)
from pydantic import SecretStr

from acp_gateway.agents import AgentError
from acp_gateway.channels.base import Channel
from acp_gateway.channels.telegram_dashboard import Dashboard
from acp_gateway.channels.telegram_format import FORMAT_VERSION, answer_chunks, plain_html
from acp_gateway.channels.telegram_ui import PANEL_CONTEXT, Panels
from acp_gateway.channels.telegram_work import WorkCards
from acp_gateway.config import TelegramSettings
from acp_gateway.connectors.diagnostics import computer_diagnostics
from acp_gateway.core import GatewayError
from acp_gateway.core.bus import ApprovalRequested, ApprovalResolved, JobFinished
from acp_gateway.log import get_logger, redact_text, register_secret
from acp_gateway.storage import Conversation, Job

HELP = (
    "Select an agent and create or select a dialogue before sending messages.\n"
    "/computers — компьютеры и подключения\n/jobs — задачи этого чата\n/job ID — карточка задачи\n"
    "/menu — control panel\n/start, /help — help\n"
    "/agent — list agents\n/agent ALIAS — select agent\n"
    "/new [ALIAS] — new session\n/sessions [PAGE] — list dialogues\n"
    "/dialogue ID [PAGE] — preview a dialogue\n"
    "/rename ID TITLE — dispatcher display name (Hermes title unchanged)\n"
    "/switch ID — activate a session\n/status — connections and jobs\n"
    "/result JOB_ID — saved result\n/delivery JOB_ID — automatic delivery state\n"
    "/stop — cancel this chat's jobs\n"
    "/model, /models — observed model and catalogue\n"
    "/model state — saved evidence without reconnect\n"
    "/model reconcile ID REVISION — inspect retained outcome, no retry\n"
    "/model set PROVIDER MODEL ID REVISION [TOKEN] — select/confirm\n"
    "/model default [PROVIDER MODEL | clear] — new-dialogue default\n"
    "/mode — execution mode for the selected dialogue\n"
    "/approvals — pending approval buttons\n"
    "Default: upstream/human approvals. /mode can explicitly authorize this dialogue."
)


COMMANDS = (
    ("start", "Getting started", "Начало работы"),
    ("help", "Commands and usage", "Команды и подсказки"),
    ("menu", "Control panel", "Панель управления"),
    ("computers", "Computers", "Компьютеры и подключения"),
    ("jobs", "Job history", "История задач этого чата"),
    ("job", "Job details", "Подробности: /job JOB_ID"),
    ("agent", "List or select an agent", "Список и выбор агента"),
    ("new", "Create a dialogue", "Создать диалог"),
    ("sessions", "List dialogues", "Список диалогов"),
    ("switch", "Switch dialogue: /switch ID", "Выбрать диалог: /switch ID"),
    ("status", "PC connections, agents and jobs", "Подключения, агенты и задачи"),
    ("result", "Saved answer: /result JOB_ID", "Результат: /result JOB_ID"),
    ("delivery", "Answer delivery: /delivery JOB_ID", "Доставка: /delivery JOB_ID"),
    ("stop", "Stop this chat's jobs", "Остановить задачи этого чата"),
    ("model", "Current model and settings", "Текущая модель и настройки"),
    ("models", "Available models", "Доступные модели"),
    ("mode", "Dialogue execution mode", "Режим выполнения диалога"),
    ("approvals", "Pending confirmations", "Ожидающие подтверждения"),
)


_SESSION_PAGE_SIZE = 10


def _label(text, limit=120):
    return " ".join(redact_text(str(text)).split())[:limit]


def _session_state(session):
    return {
        "attached": "подключён",
        "detached": "отсоединён",
        "restore-unavailable": "нельзя восстановить после перезапуска",
        "unknown": "не проверен",
        "busy": "выполняет задачу",
        "uncertain": "предыдущая операция требует проверки",
    }.get(session.availability, session.availability)


def _dialogue_summary(session):
    lines = [f"Агент / ПК: {_label(session.conversation.agent)}"]
    if session.title:
        lines.append(f"Название: {_label(session.title)}")
    lines.append(f"Папка: {_label(session.cwd, 500)}")
    if session.binding or session.availability == "restore-unavailable":
        lines.append(f"Состояние: {_session_state(session)}")
    return "\n".join(lines)


class _Updates(BaseMiddleware):
    def __init__(self, channel):
        self.channel = channel

    async def __call__(self, handler, event, data):
        channel = self.channel
        previous = channel._cursor()
        if event.update_id <= previous:
            return None
        # Persist before invoking commands. A crash may lose a response, but
        # Telegram cannot replay an agent command after daemon restart.
        channel.core.store.set_channel_state(
            channel.namespace, "cursor", {"id": event.update_id, "time": time.time()}
        )
        return await handler(event, data)


class TelegramChannel(WorkCards, Dashboard, Channel):
    name = "telegram"
    can_approve = True

    def __init__(self, settings: TelegramSettings, token: SecretStr, *, bot=None, dispatcher=None):
        register_secret(token.get_secret_value())
        self.settings = settings.model_copy(deep=True)
        self.bot = bot or Bot(
            token.get_secret_value(), default=DefaultBotProperties(parse_mode="HTML")
        )
        self.connector_dispatcher = dispatcher
        self.namespace = f"telegram:{self.bot.id}"
        self.dispatcher = Dispatcher()
        self.dispatcher.update.outer_middleware(_Updates(self))
        self.dispatcher.message.register(self._message)
        self.dispatcher.callback_query.register(self._callback)
        self.bot.session.middleware(self._request)
        self._ready = False
        self._chats: set[int] = set()
        self._panels = Panels()
        self._buttons: dict[tuple[int, int], tuple[str, tuple[str, ...]]] = {}
        self._approval_texts: dict[tuple[int, int], str] = {}
        self._recent: dict[int, deque[float]] = {}
        self._throttled: dict[int, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._subscription = None
        self._deliveries: asyncio.Queue[tuple[int, str, Job | None]] = asyncio.Queue(maxsize=32)
        self._outbound_ready = asyncio.Event()
        self._statuses: dict[str, tuple[int, int, str, float]] = {}
        self._log = get_logger(__name__)

    @property
    def connected(self):
        return self._ready and bool(self._chats) and all(not t.done() for t in self._tasks)

    def _cursor(self):
        # Telegram chooses a new random update_id after a week of inactivity.
        cursor = self.core.store.channel_state(self.namespace, "cursor", {})
        if time.time() - cursor.get("time", 0) > 6 * 86400:
            return -1
        return cursor.get("id", -1)

    async def _request(self, make_request, bot, method):
        polling = isinstance(method, GetUpdates)
        if polling:
            offset = self._cursor() + 1
            method.offset = max(method.offset or 0, offset)
        try:
            result = await make_request(bot, method)
        except Exception:
            if polling:
                self._ready = False
            raise
        if polling:
            self._ready = True
        return result

    async def start(self, core):
        self.core = core
        allowed = set(self.settings.allowed_user_ids)
        self._chats = allowed.intersection(core.store.channel_state(self.namespace, "chats", []))
        self._subscription = core.bus.subscribe(
            lambda e: (
                (isinstance(e, JobFinished) and e.conversation.channel == self.name)
                or (isinstance(e, ApprovalRequested) and e.approver_channel == self.name)
                or (isinstance(e, ApprovalResolved) and self.name in e.approver_channels)
            )
        )
        core.store.deliveries.recover(self.namespace)
        self._tasks = [
            asyncio.create_task(self._deliver_results(), name="telegram-outbound"),
            asyncio.create_task(self._poll()),
            asyncio.create_task(self._menus(), name="telegram-menu"),
            asyncio.create_task(self._events()),
            asyncio.create_task(self._deliver()),
        ]
        if self.settings.status_enabled:
            self._tasks.append(asyncio.create_task(self._update_statuses()))

    async def _register_commands(self):
        for language, column in (("", 1), ("ru", 2)):
            await self.bot.set_my_commands(
                [BotCommand(command=row[0], description=row[column]) for row in COMMANDS],
                scope=BotCommandScopeAllPrivateChats(),
                language_code=language,
                request_timeout=10,
            )
        await self.bot.set_chat_menu_button(menu_button=MenuButtonCommands(), request_timeout=10)

    async def _menus(self):
        # Menu failures must never prevent polling or result delivery.
        while True:
            try:
                await self._register_commands()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.warning("Telegram command menu registration failed; retrying")
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(3600)

    def _status_lines(self):
        lines = ["Dispatcher: running"]
        routes = {}
        if self.connector_dispatcher is not None:
            snapshot = computer_diagnostics(self.core, self.connector_dispatcher)
            routes = {row["address"]: row for row in snapshot["routes"]}
            for pc in snapshot["computers"]:
                state = (
                    "access disabled"
                    if not pc["enabled"]
                    else "online"
                    if pc["connected"]
                    else "offline"
                )
                lines.append(f"PC {pc['computer_id']}: {state}")
        for alias, client in self.core.agents.items():
            route = routes.get(alias)
            if route is not None:
                problem = route["problem"]
                state = {
                    "not_enrolled": "PC not registered",
                    "access_disabled": "PC access disabled",
                    "computer_offline": "PC offline",
                    "agent_unadvertised": "agent not announced by PC",
                    "agent_not_initialized": "not checked yet; PC online",
                }.get(problem)
                if route["agent_ready"]:
                    state = "ready"
                elif state is None:
                    state = f"connection needs checking ({problem})"
            else:
                state = "ready" if client.connected else "not initialized"
                if client.profile.backend == "connector":
                    state = "ready" if client.connected else "PC connection not checked"
            lines.append(f"{alias}: {state}")
        if not self.core.agents:
            lines.append("No agents configured.")
        # CLI approvals are optional; stateless MCP readiness is not a client connection.
        mcp = self.core.channels.get("mcp")
        if mcp is not None:
            lines.append(f"MCP: {'serving' if mcp.connected else 'not started'}")
        return lines

    async def _poll(self):
        delay = 1
        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=self._cursor() + 1,
                    timeout=10,
                    request_timeout=20,
                    allowed_updates=["message", "callback_query"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._ready = False
                self._log.exception("Telegram polling failed; reconnecting", retry_in=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            delay = 1
            for update in updates:
                try:
                    # Sequential updates plus the durable cursor prevent
                    # replay after restart and bound concurrent handlers.
                    await self.dispatcher.feed_update(self.bot, update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._log.exception("Telegram update failed")

    async def stop(self):
        self._ready = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._subscription is not None:
            self._subscription.close()
        self._buttons.clear()
        self._approval_texts.clear()
        self._panels.clear()
        self._statuses.clear()
        while not self._deliveries.empty():
            self._deliveries.get_nowait()
        await self.bot.session.close()

    def _queue_delivery(self, chat: int, text: str, job=None):
        # Saved answers remain retrievable with /result. A bounded queue keeps
        # slow delivery from consuming memory or blocking approval events/clicks.
        try:
            self._deliveries.put_nowait((chat, text, job))
        except asyncio.QueueFull:
            self._log.warning("Telegram delivery queue full; use /result")
            return False
        return True

    async def _deliver(self):
        while True:
            chat, text, job = await self._deliveries.get()
            try:
                await self._send_answer(chat, text, job)
            except Exception:
                self._log.exception("Telegram result could not be delivered; use /result")

    async def _deliver_results(self):
        while True:
            try:
                self._outbound_ready.clear()
                row = self.core.store.deliveries.due(self.namespace)
                if row is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._outbound_ready.wait(), 1)
                    continue
                await self._deliver_result(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.warning("Telegram outbound storage unavailable; retrying")
                await asyncio.sleep(1)

    async def _deliver_result(self, row):
        deliveries = self.core.store.deliveries
        job_id = row["job_id"]
        if not deliveries.claim(self.namespace, job_id):
            return
        try:
            chat = int(row["conversation_key"])
            if chat not in self._chats or chat not in self.settings.allowed_user_ids:
                deliveries.settle(self.namespace, job_id, "failed")
                return
            job = self.core.job(job_id)
            text = job.answer or job.error or f"Job: {job.status.value}"
            chunks = answer_chunks(text)
            digest = hashlib.sha256(
                json.dumps([FORMAT_VERSION, chunks], ensure_ascii=True).encode("utf-8")
            ).hexdigest()
            # Finish an already-bound rc5 delivery in its original plain format.
            # Changing chunk boundaries after an ACK could repeat or omit content.
            legacy = self._chunks(text)
            legacy_digest = hashlib.sha256(
                json.dumps(legacy, ensure_ascii=True).encode("utf-8")
            ).hexdigest()
            if row["content_hash"] == legacy_digest:
                chunks = [html.escape(chunk, quote=False) for chunk in legacy]
                digest = legacy_digest
            if not deliveries.bind_content(self.namespace, job_id, digest):
                return
            index = row["next_chunk"]
            if index >= len(chunks):
                # A changed formatter must not silently restart old content.
                deliveries.settle(self.namespace, job_id, "unknown")
                return
            markup, tokens = self._panels.create(chat, self._job_rows(job), self._ui_context(chat))
            message = await self._send_payload(chat, chunks[index], markup=markup)
            self._panels.bind(tokens, message.message_id)
            deliveries.acknowledge(self.namespace, job_id, last=index == len(chunks) - 1)
        except TelegramRetryAfter as exc:
            # Every send attempt for this chunk was explicitly rejected.
            deliveries.settle(self.namespace, job_id, "pending", retry_after=exc.retry_after)
        except (TelegramBadRequest, TelegramForbiddenError):
            deliveries.settle(self.namespace, job_id, "failed")
        except asyncio.CancelledError:
            deliveries.settle(self.namespace, job_id, "unknown")
            raise
        except Exception:
            # Includes loss of a local ACK commit after a successful remote send.
            deliveries.settle(self.namespace, job_id, "unknown")
            self._log.warning("Telegram delivery outcome unknown; use /result")

    async def _update_statuses(self):
        while True:
            await asyncio.sleep(self.settings.status_interval_seconds)
            for job_id, (chat, message_id, previous, retry_at) in list(self._statuses.items()):
                if time.monotonic() < retry_at:
                    continue
                try:
                    job = self.core.job(job_id)
                    text = self._work_text(job)
                    rendered = html.escape(text, quote=False)
                    if text != previous:
                        markup, tokens = self._panels.create(
                            chat, self._job_rows(job), self._ui_context(chat)
                        )
                        await self.bot.edit_message_text(
                            chat_id=chat,
                            message_id=message_id,
                            text=rendered,
                            parse_mode="HTML",
                            reply_markup=markup,
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                        self._panels.retire_message(chat, message_id)
                        self._panels.bind(tokens, message_id)
                    if job.status.finished:
                        self._statuses.pop(job_id, None)
                    else:
                        self._statuses[job_id] = (chat, message_id, text, 0)
                except TelegramRetryAfter as exc:
                    self._statuses[job_id] = (
                        chat,
                        message_id,
                        previous,
                        time.monotonic() + max(1, exc.retry_after),
                    )
                except Exception:
                    # Status edits are advisory; final answers use the durable queue.
                    self._statuses.pop(job_id, None)
                    self._log.warning("Telegram status edit unavailable; use /status or /result")

    @staticmethod
    def _chunks(text):
        text = redact_text(text)
        return [
            chunk for i in range(0, len(text), 2000) if (chunk := text[i : i + 2000]).strip()
        ] or ["(empty response)"]

    def _authorized(self, user, chat):
        return (
            user is not None
            and not user.is_bot
            and user.id in self.settings.allowed_user_ids
            and chat.type == "private"
            and chat.id == user.id
        )

    async def _send_payload(self, chat, text, *, markup=None, plain=False):
        for attempt in range(3):
            try:
                return await self.bot.send_message(
                    chat,
                    text,
                    parse_mode=None if plain else "HTML",
                    reply_markup=markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramBadRequest as exc:
                if not plain and any(
                    reason in exc.message.lower()
                    for reason in (
                        "can't parse entities",
                        "unsupported start tag",
                        "can't find end tag",
                    )
                ):
                    return await self._send_payload(
                        chat, plain_html(text), markup=markup, plain=True
                    )
                raise
            except TelegramRetryAfter as exc:
                # Only an explicit rejection is safe to retry.
                if attempt == 2 or exc.retry_after > 30:
                    raise
                await asyncio.sleep(exc.retry_after)

    async def _send(self, chat: int, text: str, *, markup=None):
        first = None
        for chunk in self._chunks(text):
            message = await self._send_payload(
                chat, html.escape(chunk, quote=False), markup=markup if first is None else None
            )
            first = first or message
        return first

    async def _send_answer(self, chat, text, job=None):
        chunks = answer_chunks(text)
        for index, chunk in enumerate(chunks):
            rows = self._job_rows(job) if job and index == len(chunks) - 1 else []
            markup, tokens = (
                self._panels.create(chat, rows, self._ui_context(chat)) if rows else (None, [])
            )
            message = await self._send_payload(chat, chunk, markup=markup)
            self._panels.bind(tokens, message.message_id)

    def _ui_context(self, chat):
        try:
            conv = self._conversation(chat)
        except GatewayError:
            conv = None
        return (
            conv.agent if conv else None,
            self.core.store.channel_state(self.namespace, f"route_revision:{chat}", 0),
            self.core.store.active_revision(conv) if conv else None,
        )

    @staticmethod
    def _back_rows():
        return [[("← Меню", "/menu")]]

    def _job_rows(self, job):
        rows = [
            [(f"🖥 {job.conversation.agent} · #{job.session_id}", f"details:{job.id}")],
            [("📄 Результат", f"result:{job.id}")],
        ]
        try:
            client = self.core.agent(job.conversation.agent)
        except GatewayError:
            client = None
        if not job.status.finished and client and getattr(client, "supports_cancel", True):
            rows.insert(0, [("⏹ Остановить задачу", f"cancel:{job.id}")])
        return rows + TelegramChannel._back_rows()

    async def _panel(self, chat, text, rows, *, editable=True):
        context = PANEL_CONTEXT.get() if editable else None
        chunks = self._chunks(text)
        if context is not None and context[0].chat.id == chat and len(chunks) == 1:
            message, editor, _ = context
            async with editor.lock:
                if not self._panels.current_edit(context):
                    return message
                markup, tokens = self._panels.create(chat, rows, self._ui_context(chat), panel=True)
                try:
                    await self.bot.edit_message_text(
                        chat_id=chat,
                        message_id=message.message_id,
                        text=html.escape(chunks[0], quote=False),
                        parse_mode="HTML",
                        reply_markup=markup,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                except TelegramBadRequest as exc:
                    self._panels.discard(tokens)
                    if "message is not modified" in exc.message.lower():
                        return message
                    if not any(
                        reason in exc.message.lower()
                        for reason in (
                            "message to edit not found",
                            "message can't be edited",
                            "message cannot be edited",
                        )
                    ):
                        raise
                    # An explicit rejection permits a fresh navigation message.
                    self._panels.retire_message(chat, message.message_id)
                except BaseException:
                    self._panels.discard(tokens)
                    raise  # An ambiguous edit is never resent automatically.
                else:
                    self._panels.retire_message(chat, message.message_id)
                    self._panels.bind(tokens, message.message_id)
                    return message
        if context is not None and not self._panels.current_edit(context):
            return context[0]
        markup, tokens = self._panels.create(chat, rows, self._ui_context(chat), panel=editable)
        try:
            message = await self._send(chat, text, markup=markup)
        except BaseException:
            self._panels.discard(tokens)
            raise
        if context is not None:
            self._panels.retire_message(chat, context[0].message_id)
        self._panels.bind(tokens, message.message_id)
        return message

    async def _home(self, chat):
        context = self._ui_context(chat)
        text = "Управление ACP Gateway"
        rows = [
            [("💻 Компьютеры", "/computers"), ("📋 Задачи", "/jobs")],
            [("🖥 Агенты", "/agent"), ("📊 Статус", "/status")],
        ]
        if context[0]:
            conv = self._conversation(chat)
            active = self.core.active_session(conv)
            text += f"\nАгент: {conv.agent}"
            text += f"\nДиалог: #{active.id}" if active else "\nДиалог: не выбран"
            if active and active.title:
                text += f" — {_label(active.title)}"
            rows += [[("💬 Диалоги", "/sessions"), ("➕ Новый диалог", "/new")]]
            if active:
                mode = self.core.execution_mode(conv)["mode"]
                text += "\nВыполнение: " + ("без подтверждений" if mode == "auto" else "как Hermes")
                rows += [[("🧠 Модель", "/model"), ("⚙ Выполнение", "/mode")]]
        else:
            text += "\nВыберите агента, затем создайте или выберите диалог."
        rows += [[("✅ Подтверждения", "/approvals"), ("❓ Помощь", "/help")]]
        await self._panel(chat, text, rows)

    async def _model_panel(self, chat, conv, result, *, catalogue=False, page=1, selection=None):
        dialogue = result.get("dialogue_id")
        revision = result.get("selection_revision")
        status = result.get("status")
        lines = [f"Модель · {conv.agent}"]
        if dialogue is not None:
            lines.append(f"Диалог: #{dialogue}")
        rows = []
        if selection:
            lines.append(f"Выбор: {selection[0]} / {selection[1]}")
        if status == "confirmation_required" and selection:
            lines += ["Требуется подтверждение смены модели.", result.get("message") or ""]
            provider, model = selection
            token = result["confirmation_token"]
            rows.append(
                [
                    (
                        "Подтвердить смену модели",
                        f"/model set {provider} {model} {dialogue} {revision} {token}",
                    )
                ]
            )
        elif status in {"accepted", "deferred"}:
            lines.append(
                "Запрос принят; проверьте настройки модели."
                if status == "accepted"
                else "Смена модели отложена до завершения текущей работы."
            )
        else:
            lines += [
                f"Провайдер: {result.get('provider') or 'не определён'}",
                f"Модель: {result.get('model') or 'не определена'}",
            ]
            pending = result.get("pending")
            if pending:
                lines.append(f"Ожидается: {pending.get('provider')}/{pending.get('model')}")
        if catalogue:
            choices = [
                (provider["provider"], model)
                for provider in result.get("providers", [])
                for model in provider["models"]
            ]
            pages = max(1, (len(choices) + 9) // 10)
            if not 1 <= page <= pages:
                raise GatewayError(f"Choose a page from 1 to {pages}: /models PAGE.")
            if not choices:
                lines.append("Список моделей недоступен.")
            for provider, model in choices[(page - 1) * 10 : page * 10]:
                marker = (
                    "✓ "
                    if (provider, model) == (result.get("provider"), result.get("model"))
                    else ""
                )
                rows.append(
                    [
                        (
                            f"{marker}{_label(provider, 20)} / {_label(model, 35)}",
                            f"/model set {provider} {model} {dialogue} {revision}",
                        )
                    ]
                )
            navigation = []
            if page > 1:
                navigation.append(("← Назад", f"/models {page - 1}"))
            if page < pages:
                navigation.append(("Далее →", f"/models {page + 1}"))
            if navigation:
                rows.append(navigation)
            if pages > 1:
                lines.append(f"Страница {page}/{pages}")
        else:
            rows.append([("Выбрать модель", "/models")])
        rows += [[("↻ Проверить", "/model")], *self._back_rows()]
        await self._panel(chat, "\n".join(lines), rows)

    async def _admit_ui(self, chat):
        recent = self._recent.setdefault(chat, deque())
        now = time.monotonic()
        while recent and recent[0] <= now - 10:
            recent.popleft()
        if len(recent) >= 5:
            raise GatewayError("Слишком много действий. Подождите несколько секунд.")
        recent.append(now)

    async def _ui_callback(self, query):
        chat = query.message.chat.id
        try:
            action = self._panels.consume(
                query.data, chat, query.message.message_id, self._ui_context(chat)
            )
            await self._admit_ui(chat)
        except GatewayError as exc:
            await self.bot.answer_callback_query(query.id, text=str(exc)[:180], show_alert=True)
            return
        command = action.command
        context = self._panels.begin_edit(query.message) if action.panel else None
        token = PANEL_CONTEXT.set(context)
        try:
            await self._execute_ui_callback(query, command)
        finally:
            PANEL_CONTEXT.reset(token)

    async def _execute_ui_callback(self, query, command):
        chat = query.message.chat.id
        # Consume one-shot controls and dismiss the spinner before any host RPC.
        await self.bot.answer_callback_query(query.id)
        try:
            if command.startswith(("cancel:", "result:", "details:")):
                action, job_id = command.split(":", 1)
                job = self.core.job(job_id)
                if job.conversation.channel != self.name or job.conversation.key != str(chat):
                    raise GatewayError("This chat does not own that job.")
                if action == "cancel":
                    if job.status.finished:
                        raise GatewayError("Задача уже завершена.")
                    await self.core.cancel_job(job.id)
                    await self._job_panel(chat, job.id)
                elif action == "result":
                    if not self._queue_delivery(
                        chat, job.answer or job.error or job.status.value, job
                    ):
                        raise GatewayError("Доставка занята. Повторите позже.")
                else:
                    await self._job_panel(chat, job.id)
            else:
                await self._command(chat, command)
        except (AgentError, GatewayError) as exc:
            await self._panel(chat, str(exc), self._back_rows())
        except TelegramAPIError:
            self._log.warning("Telegram control response could not be delivered")
        except Exception:
            self._log.exception("Telegram control failed")
            await self._panel(chat, "Не удалось выполнить действие.", self._back_rows())

    def _conversation(self, user_id: int):
        alias = self.core.store.channel_state(self.namespace, f"agent:{user_id}")
        if alias is None and len(self.core.agents) == 1:
            alias = next(iter(self.core.agents))
        if alias is None:
            raise GatewayError("Select an agent with /agent ALIAS.")
        self.core.agent(alias)
        return Conversation(self.name, str(user_id), alias)

    async def _message(self, message: Message):
        if not self._authorized(message.from_user, message.chat) or not message.text:
            return
        user_id = message.from_user.id
        self._chats.add(user_id)
        self.core.store.set_channel_state(self.namespace, "chats", sorted(self._chats))
        recent = self._recent.setdefault(user_id, deque())
        now = time.monotonic()
        while recent and recent[0] <= now - 10:
            recent.popleft()
        if len(recent) >= 5:
            if now - self._throttled.get(user_id, 0) >= 10:
                self._throttled[user_id] = now
                await self._send(user_id, "Too many messages; wait a few seconds.")
            return
        recent.append(now)
        try:
            await self._command(user_id, message.text)
        except (AgentError, GatewayError) as exc:
            await self._send(user_id, str(exc))
        except TelegramAPIError:
            self._log.exception("Telegram response could not be delivered")
        except Exception:
            self._log.exception("Telegram command failed")
            await self._send(user_id, "Internal gateway error.")

    def _set_route(self, chat, alias):
        self.core.agent(alias)
        self.core.store.set_channel_state(self.namespace, f"agent:{chat}", alias)
        revision = self.core.store.channel_state(self.namespace, f"route_revision:{chat}", 0)
        self.core.store.set_channel_state(self.namespace, f"route_revision:{chat}", revision + 1)

    async def _command(self, chat: int, text: str):
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0]
        argument = argument.strip()
        if command == "/approval":
            await self._approval_details(chat, argument)
            return
        if await self._dashboard(chat, command, argument):
            return
        if command in {"/start", "/menu"}:
            await self._home(chat)
            return
        if command == "/help":
            await self._panel(chat, HELP, self._back_rows())
            return
        if command == "/agent" and argument:
            self._set_route(chat, argument)
        if command == "/agent":
            if argument:
                await self._panel(
                    chat,
                    f"Agent: {self._conversation(chat).agent}",
                    [
                        [("➕ Новый диалог", "/new"), ("💬 Диалоги", "/sessions")],
                        *self._back_rows(),
                    ],
                )
            else:
                selected = self.core.store.channel_state(self.namespace, f"agent:{chat}")
                if selected is None and len(self.core.agents) == 1:
                    selected = next(iter(self.core.agents))
                lines = ["Agents"]
                for alias, client in self.core.agents.items():
                    marker = "▶" if alias == selected else "•"
                    lines.append(f"{marker} {alias} — {_label(client.profile.display_name)}")
                if not self.core.agents:
                    lines.append("No agents available. Connect a configured PC first.")
                lines += ["", "▶ Selected agent", "Select: /agent ALIAS", "New dialogue: /new"]
                rows = [
                    [(_label(alias, 50), f"/agent {alias}")]
                    for alias in list(self.core.agents)[:20]
                ]
                await self._panel(chat, "\n".join(lines), rows + self._back_rows())
            return
        if command == "/status":
            lines = self._status_lines()
            lines += [
                f"Running: {j.id} ({j.conversation.agent})"
                for j in self.core.running_jobs()
                if j.conversation.channel == self.name and j.conversation.key == str(chat)
            ]
            await self._panel(
                chat, "\n".join(lines), [[("↻ Обновить", "/status")], *self._back_rows()]
            )
            return
        if command == "/approvals":
            pending = [
                a for a in self.core.pending_approvals(self.name) if chat in self._approval_chats(a)
            ]
            for approval in pending:
                await self._approval(chat, approval)
            if not pending:
                await self._panel(chat, "Ожидающих подтверждений нет.", self._back_rows())
            return
        conv = (
            Conversation(self.name, str(chat), argument)
            if command == "/new" and argument
            else self._conversation(chat)
        )
        if command == "/mode":
            parts = argument.split()
            if parts:
                if (
                    len(parts) != 4
                    or parts[0] != "set"
                    or not all(p.isdecimal() for p in parts[2:])
                ):
                    raise GatewayError("Use /mode and its buttons to choose a mode.")
                self.core.set_execution_mode(
                    conv,
                    parts[1],
                    dialogue_id=int(parts[2]),
                    expected_revision=int(parts[3]),
                    actor=f"telegram:{chat}",
                )
            state = self.core.execution_mode(conv)
            dialogue, revision = state["dialogue_id"], state["selection_revision"]
            automatic = state["mode"] == "auto"
            label = "Без подтверждений" if automatic else "Как Hermes"
            await self._panel(
                chat,
                f"Выполнение · {conv.agent}\nДиалог: #{dialogue}\nРежим: {label}\n\n"
                "Как Hermes: запросы разрешений подтверждаете вы.\n"
                "Без подтверждений: диспетчер разрешает все запросы разрешений этого диалога, "
                "включая запись и выполнение команд. Решения сохраняются в аудите.\n"
                "Настройки остальных чатов Hermes не меняются. Новый диалог — Как Hermes.",
                [
                    [
                        (
                            ("✓ " if not automatic else "") + "Как Hermes",
                            f"/mode set ask {dialogue} {revision}",
                        )
                    ],
                    [
                        (
                            ("✓ " if automatic else "") + "Без подтверждений",
                            f"/mode set auto {dialogue} {revision}",
                        )
                    ],
                    *self._back_rows(),
                ],
            )
            return
        if command in {"/model", "/models"}:
            route_revision = self.core.store.channel_state(
                self.namespace, f"route_revision:{chat}", 0
            )

            def route_guard():
                if (
                    self._conversation(chat) != conv
                    or self.core.store.channel_state(self.namespace, f"route_revision:{chat}", 0)
                    != route_revision
                ):
                    raise GatewayError(
                        "Agent route changed; refresh model settings before retrying."
                    )

            parts = argument.split()
            catalogue_page = 1
            if (
                command == "/models"
                and len(parts) == 1
                and parts[0].isascii()
                and parts[0].isdecimal()
            ):
                if len(parts[0]) > 6:
                    raise GatewayError("Use /models [PAGE].")
                catalogue_page = int(parts[0])
                parts = []
            if parts and parts[0] == "default":
                if len(parts) == 1:
                    result = self.core.model_default(conv)
                elif parts[1:] == ["clear"]:
                    result = self.core.clear_model_default(conv)
                elif len(parts) == 3:
                    result = self.core.set_model_default(conv, provider=parts[1], model=parts[2])
                else:
                    raise GatewayError("Use /model default [PROVIDER MODEL | clear].")
            else:
                active = self.core.active_session(conv)
                if active is None:
                    raise GatewayError("No active dialogue; use /new or /switch ID.")
                revision = self.core.store.active_revision(conv)
                if not parts and command in {"/model", "/models"}:
                    result = await self.core.model_options(
                        conv,
                        dialogue_id=active.id,
                        expected_revision=revision,
                        admission=route_guard,
                    )
                elif (
                    len(parts) == 3
                    and parts[0] == "reconcile"
                    and all(p.isascii() and p.isdecimal() for p in parts[1:])
                ):
                    result = await self.core.reconcile_model(
                        conv,
                        dialogue_id=int(parts[1]),
                        expected_revision=int(parts[2]),
                        admission=route_guard,
                    )
                elif parts == ["state"]:
                    result = self.core.model_state(conv, active.id)
                elif (
                    len(parts) in {5, 6}
                    and parts[0] == "set"
                    and all(p.isascii() and p.isdecimal() for p in parts[3:5])
                ):
                    result = await self.core.set_model(
                        conv,
                        provider=parts[1],
                        model=parts[2],
                        dialogue_id=int(parts[3]),
                        expected_revision=int(parts[4]),
                        confirmation_token=parts[5] if len(parts) == 6 else None,
                        admission=route_guard,
                    )
                else:
                    raise GatewayError("Use /model set PROVIDER MODEL ID REVISION [TOKEN].")
            if not parts or parts[0] != "default":
                route_guard()
                if result.get("selection_changed"):
                    raise GatewayError("Dialogue changed; refresh model settings before retrying.")
            if not parts or (
                parts[0] == "set"
                and result.get("status") in {"confirmation_required", "accepted", "deferred"}
            ):
                await self._model_panel(
                    chat,
                    conv,
                    result,
                    catalogue=command == "/models",
                    page=catalogue_page,
                    selection=(parts[1], parts[2]) if parts and parts[0] == "set" else None,
                )
                return
            if command == "/model":
                result = {k: v for k, v in result.items() if k != "providers"}
            encoded = json.dumps({"agent": conv.agent, **result}, ensure_ascii=False, indent=2)
            if len(encoded) > 10000:
                encoded = (
                    encoded[:10000] + "\nCatalogue truncated; use CLI model list for the full list."
                )
            await self._panel(chat, encoded, self._back_rows())
        elif command == "/new":
            revision = self.core.store.channel_state(self.namespace, f"route_revision:{chat}", 0)
            session = await self.core.new_session(conv)
            # A route change during the host RPC must not be silently overwritten.
            if revision != self.core.store.channel_state(
                self.namespace, f"route_revision:{chat}", 0
            ):
                await self._panel(
                    chat,
                    f"Dialogue #{session.id} created for {conv.agent}. "
                    "Selected agent changed; open /menu.",
                    self._back_rows(),
                )
                return
            if self.core.store.channel_state(self.namespace, f"agent:{chat}") != conv.agent:
                self._set_route(chat, conv.agent)
            await self._panel(
                chat,
                f"Новый диалог #{session.id}\n{_dialogue_summary(session)}\n\n"
                "Отправьте сообщение, чтобы начать.",
                [[("💬 Диалоги", "/sessions")], *self._back_rows()],
            )
        elif command == "/sessions":
            if argument and (
                not argument.isascii() or not argument.isdecimal() or len(argument) > 6
            ):
                raise GatewayError("Use /sessions [PAGE], starting with page 1.")
            page = int(argument or "1")
            sessions = self.core.sessions(conv)
            pages = max(1, (len(sessions) + _SESSION_PAGE_SIZE - 1) // _SESSION_PAGE_SIZE)
            if not 1 <= page <= pages:
                raise GatewayError(f"Choose a page from 1 to {pages}: /sessions PAGE.")
            active = self.core.active_session(conv)
            lines = [f"Диалоги · {_label(conv.agent)}"]
            if not sessions:
                lines += ["", "Диалогов пока нет. Создайте: /new."]
            else:
                start = (page - 1) * _SESSION_PAGE_SIZE
                for session in sessions[start : start + _SESSION_PAGE_SIZE]:
                    selected = bool(active and session.id == active.id)
                    marker = "▶" if selected else "•"
                    title = _label(session.title) if session.title else "Без названия"
                    lines += ["", f"{marker} #{session.id} — {title}"]
                    if session.binding or session.availability == "restore-unavailable":
                        lines.append(f"Состояние: {_session_state(session)}")
                    lines.append(f"Последняя активность: {session.last_used_at:%Y-%m-%d %H:%M} UTC")
                    lines.append(f"Папка: {_label(session.cwd, 500)}")
                lines += ["", "▶ Выбранный диалог", "Выбрать: /switch ID · Создать: /new"]
                if pages > 1:
                    lines.append(f"Страница {page}/{pages} · Открыть: /sessions PAGE")
            rows = [
                [
                    (
                        f"{'▶ ' if active and item.id == active.id else ''}#{item.id} · "
                        f"{_label(item.title or 'Без названия', 35)}",
                        f"/dialogue {item.id} {page}",
                    )
                ]
                for item in sessions[(page - 1) * _SESSION_PAGE_SIZE : page * _SESSION_PAGE_SIZE]
            ]
            navigation = []
            if page > 1:
                navigation.append(("← Назад", f"/sessions {page - 1}"))
            if page < pages:
                navigation.append(("Далее →", f"/sessions {page + 1}"))
            if navigation:
                rows.append(navigation)
            rows += [[("➕ Новый диалог", "/new")], *self._back_rows()]
            await self._panel(chat, "\n".join(lines), rows)
        elif command in {"/dialogue", "/switch", "/rename_hint"}:
            parts = argument.split()
            if not 1 <= len(parts) <= 2 or any(
                not part.isascii() or not part.isdecimal() or len(part) > 10 for part in parts
            ):
                raise GatewayError(f"Use {command} SESSION_ID [PAGE].")
            page = int(parts[1]) if len(parts) == 2 else 1
            sessions = self.core.sessions(conv)
            pages = max(1, (len(sessions) + _SESSION_PAGE_SIZE - 1) // _SESSION_PAGE_SIZE)
            if not 1 <= page <= pages:
                raise GatewayError(f"Choose a page from 1 to {pages}: /sessions PAGE.")
            session = next((item for item in sessions if item.id == int(parts[0])), None)
            if session is None:
                raise GatewayError("This conversation does not own that dialogue.")
            if command == "/rename_hint":
                await self._panel(
                    chat,
                    f"Название диспетчера · #{session.id}\n"
                    f"Сейчас: {_label(session.title or 'Без названия')}\n\n"
                    "Чтобы переименовать, отправьте команду:\n"
                    f"/rename {session.id} НОВОЕ НАЗВАНИЕ\n"
                    "Название Hermes не меняется. Обычный текст отправляется агенту.",
                    [[("← Диалог", f"/dialogue {session.id} {page}")], *self._back_rows()],
                )
                return
            if command == "/switch":
                session = self.core.switch_dialogue(conv, session.id)
            active = self.core.active_session(conv)
            selected = bool(active and active.id == session.id)
            lines = [
                f"Диалог #{session.id} · {_label(session.title or 'Без названия')}",
                "▶ Выбран для сообщений" if selected else "Не выбран для сообщений",
                _dialogue_summary(session),
                f"Последняя активность: {session.last_used_at:%Y-%m-%d %H:%M} UTC",
            ]
            rows = []
            if not selected:
                rows.append([("▶ Выбрать диалог", f"/switch {session.id} {page}")])
            else:
                lines += ["", "Отправьте сообщение, чтобы продолжить этот диалог."]
            lines += [
                "",
                f"Название диспетчера: /rename {session.id} НОВОЕ НАЗВАНИЕ",
                "Название Hermes не меняется.",
            ]
            rows += [
                [("✏ Переименовать", f"/rename_hint {session.id} {page}")],
                [("← Диалоги", f"/sessions {page}")],
                *self._back_rows(),
            ]
            await self._panel(chat, "\n".join(lines), rows)
        elif command == "/rename":
            identifier, _, title = argument.partition(" ")
            if not identifier.isascii() or not identifier.isdecimal() or len(identifier) > 19:
                raise GatewayError("Use /rename ID TITLE.")
            session = self.core.rename_dialogue(conv, int(identifier), title)
            await self._panel(
                chat,
                f"Название диспетчера сохранено: #{session.id} · {_label(session.title)}\n"
                "Название Hermes не менялось.",
                [[("💬 Открыть диалог", f"/dialogue {session.id}")], *self._back_rows()],
            )
        elif command == "/stop":
            jobs = await self.core.cancel(conv)
            await self._send(chat, f"Stopped jobs: {len(jobs)}")
        elif command.startswith("/"):
            await self._send(chat, "Unknown command. Use /help.")
        else:
            job = await self.core.submit_selected(conv, text, delivery_namespace=self.namespace)
            status_text = self._work_text(job)
            message = await self._panel(chat, status_text, self._job_rows(job), editable=False)
            if self.settings.status_enabled:
                if len(self._statuses) >= 32:
                    self._statuses.pop(next(iter(self._statuses)))
                self._statuses[job.id] = (chat, message.message_id, status_text, 0)

    def _approval_chats(self, approval):
        conversation = approval.conversation
        if conversation.channel == self.name:
            # A chat's own tool requests stay private to it, like its answers.
            chat = int(conversation.key)
            return (chat,) if chat in self._chats else ()
        return tuple(self._chats)

    async def _callback(self, query: CallbackQuery):
        message = query.message
        if not isinstance(message, Message) or not self._authorized(query.from_user, message.chat):
            return
        if (query.data or "").startswith("u:"):
            await self._ui_callback(query)
            return
        try:
            binding = self._buttons.get((message.chat.id, message.message_id))
            parts = (query.data or "").split(":")
            if (
                not binding
                or len(parts) != 3
                or parts[0] != "a"
                or parts[1] != binding[0]
                or not parts[2].isascii()
                or not parts[2].isdecimal()
                or len(parts[2]) > 3
            ):
                raise GatewayError("Approval is stale or does not belong to this message.")
            index = int(parts[2])
            if index >= len(binding[1]):
                raise GatewayError("Invalid approval option.")
            self.core.resolve_approval(
                binding[0],
                binding[1][index],
                channel=self.name,
                actor=f"telegram:{query.from_user.id}",
            )
            await self.bot.answer_callback_query(query.id, text="Решение записано.")
        except GatewayError as exc:
            await self.bot.answer_callback_query(
                query.id, text=redact_text(str(exc))[:180], show_alert=True
            )

    async def _events(self):
        while True:
            event = await self._subscription.get()
            if event is None:
                return
            try:
                if isinstance(event, JobFinished):
                    chat = int(event.conversation.key)
                    if self.core.store.deliveries.get(self.namespace, event.job.id):
                        self._outbound_ready.set()
                    elif chat in self._chats:
                        self._queue_delivery(
                            chat,
                            event.job.answer or event.job.error or f"Job: {event.job.status.value}",
                            event.job,
                        )
                elif isinstance(event, ApprovalRequested):
                    for chat in self._approval_chats(event.approval):
                        # One blocked or failing chat must not starve the others.
                        try:
                            await self._approval(chat, event.approval)
                        except Exception:
                            self._log.exception("Telegram approval could not be delivered")
                elif isinstance(event, ApprovalResolved):
                    for key, binding in list(self._buttons.items()):
                        if binding[0] == event.approval_id:
                            del self._buttons[key]
                            await self._settle_approval_card(key, event.outcome)
            except Exception:
                self._log.exception(
                    "Telegram event could not be delivered; use /result or /approvals"
                )
