"""Private, allowlisted Telegram chats and authenticated human approvals."""

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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
)
from pydantic import SecretStr

from acp_gateway.agents import AgentError
from acp_gateway.channels.base import Channel
from acp_gateway.config import TelegramSettings
from acp_gateway.connectors.diagnostics import computer_diagnostics
from acp_gateway.core import GatewayError
from acp_gateway.core.bus import ApprovalRequested, ApprovalResolved, JobFinished
from acp_gateway.log import get_logger, redact_text, redact_value, register_secret
from acp_gateway.storage import Conversation

HELP = (
    "Select an agent and create or select a dialogue before sending messages.\n"
    "/start, /help — help\n/agent — list agents\n/agent ALIAS — select agent\n"
    "/new [ALIAS] — new session\n/sessions [PAGE] — list dialogues\n"
    "/switch ID — activate a session\n/status — connections and jobs\n"
    "/result JOB_ID — saved result\n/delivery JOB_ID — automatic delivery state\n"
    "/stop — cancel this chat's jobs\n"
    "/model, /models — observed model and catalogue\n"
    "/model state — saved evidence without reconnect\n"
    "/model reconcile ID REVISION — inspect retained outcome, no retry\n"
    "/model set PROVIDER MODEL ID REVISION [TOKEN] — select/confirm\n"
    "/model default [PROVIDER MODEL | clear] — new-dialogue default\n"
    "/approvals — pending approval buttons\n"
    "Approvals offer allow once/reject once; permanent approval is unavailable."
)


COMMANDS = (
    ("start", "Getting started", "Начало работы"),
    ("help", "Commands and usage", "Команды и подсказки"),
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
    ("approvals", "Pending confirmations", "Ожидающие подтверждения"),
)


_SESSION_PAGE_SIZE = 10


def _label(text, limit=120):
    return " ".join(str(text).split())[:limit]


def _session_state(session):
    return {
        "attached": "connected",
        "detached": "detached",
        "restore-unavailable": "cannot resume after restart",
        "unknown": "not checked",
        "busy": "running a task",
        "uncertain": "previous operation needs checking",
    }.get(session.availability, session.availability)


def _dialogue_summary(session):
    lines = [f"Agent: {session.conversation.agent}"]
    if session.title:
        lines.append(f"Title: {_label(session.title)}")
    lines.append(f"Folder: {_label(session.cwd, 500)}")
    if session.binding or session.availability == "restore-unavailable":
        lines.append(f"State: {_session_state(session)}")
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


class TelegramChannel(Channel):
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
        self._buttons: dict[tuple[int, int], tuple[str, tuple[str, ...]]] = {}
        self._recent: dict[int, deque[float]] = {}
        self._throttled: dict[int, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._subscription = None
        self._deliveries: asyncio.Queue[tuple[int, str]] = asyncio.Queue(maxsize=32)
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
        self._statuses.clear()
        while not self._deliveries.empty():
            self._deliveries.get_nowait()
        await self.bot.session.close()

    def _queue_delivery(self, chat: int, text: str):
        # Saved answers remain retrievable with /result. A bounded queue keeps
        # slow delivery from consuming memory or blocking approval events/clicks.
        try:
            self._deliveries.put_nowait((chat, text))
        except asyncio.QueueFull:
            self._log.warning("Telegram delivery queue full; use /result")
            return False
        return True

    async def _deliver(self):
        while True:
            chat, text = await self._deliveries.get()
            try:
                await self._send(chat, text)
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
            chunks = self._chunks(job.answer or job.error or f"Job: {job.status.value}")
            digest = hashlib.sha256(
                json.dumps(chunks, ensure_ascii=True).encode("utf-8")
            ).hexdigest()
            if not deliveries.bind_content(self.namespace, job_id, digest):
                return
            index = row["next_chunk"]
            if index >= len(chunks):
                # A changed formatter must not silently restart old content.
                deliveries.settle(self.namespace, job_id, "unknown")
                return
            await self._send(chat, chunks[index])
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
                    if job.status.finished:
                        text = f"Job {job.id}: {job.status.value}. Use /result {job.id}."
                    else:
                        preview = redact_text(job.answer)[:1500]
                        text = f"Working: {job.id}\nUse /stop to cancel."
                        if preview:
                            text += "\n" + preview
                    if text != previous:
                        await self.bot.edit_message_text(
                            chat_id=chat,
                            message_id=message_id,
                            text=html.escape(text, quote=False),
                            parse_mode="HTML",
                        )
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

    async def _send(self, chat: int, text: str, *, markup=None):
        first = None
        # Telegram's limit is 4096 after entity parsing. 2000 codepoints also
        # fit when every character occupies two UTF-16 code units.
        for chunk in self._chunks(text):
            for attempt in range(3):
                try:
                    message = await self.bot.send_message(
                        chat,
                        html.escape(chunk, quote=False),
                        parse_mode="HTML",
                        reply_markup=markup if first is None else None,
                    )
                    first = first or message
                    break
                except TelegramRetryAfter as exc:
                    # Only explicit rejection is safe to retry. A network
                    # timeout may already have delivered the message.
                    if attempt == 2 or exc.retry_after > 30:
                        raise
                    await asyncio.sleep(exc.retry_after)
        return first

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

    async def _command(self, chat: int, text: str):
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0]
        argument = argument.strip()
        if command in {"/start", "/help"}:
            await self._send(chat, HELP)
            return
        if command == "/agent" and argument:
            self.core.agent(argument)
            self.core.store.set_channel_state(self.namespace, f"agent:{chat}", argument)
            route_revision = self.core.store.channel_state(
                self.namespace, f"route_revision:{chat}", 0
            )
            self.core.store.set_channel_state(
                self.namespace, f"route_revision:{chat}", route_revision + 1
            )
        if command == "/agent":
            if argument:
                await self._send(chat, f"Agent: {self._conversation(chat).agent}")
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
                await self._send(chat, "\n".join(lines))
            return
        if command == "/status":
            lines = self._status_lines()
            lines += [
                f"Running: {j.id} ({j.conversation.agent})"
                for j in self.core.running_jobs()
                if j.conversation.channel == self.name and j.conversation.key == str(chat)
            ]
            await self._send(chat, "\n".join(lines) or "No agents configured.")
            return
        if command == "/approvals":
            pending = [
                a for a in self.core.pending_approvals(self.name) if chat in self._approval_chats(a)
            ]
            for approval in pending:
                await self._approval(chat, approval)
            if not pending:
                await self._send(chat, "No pending approvals.")
            return
        conv = (
            Conversation(self.name, str(chat), argument)
            if command == "/new" and argument
            else self._conversation(chat)
        )
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
            if command == "/model":
                result = {k: v for k, v in result.items() if k != "providers"}
            encoded = json.dumps({"agent": conv.agent, **result}, ensure_ascii=False, indent=2)
            if len(encoded) > 10000:
                encoded = (
                    encoded[:10000] + "\nCatalogue truncated; use CLI model list for the full list."
                )
            await self._send(chat, encoded)
        elif command == "/new":
            session = await self.core.new_session(conv)
            # A failed creation must not silently change the selected route.
            self.core.store.set_channel_state(self.namespace, f"agent:{chat}", conv.agent)
            await self._send(
                chat,
                f"New dialogue #{session.id}\n{_dialogue_summary(session)}\n\n"
                "Send a message to begin.",
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
            lines = [f"Dialogues · {conv.agent}"]
            if not sessions:
                lines += ["", "No dialogues yet. Create one with /new."]
            else:
                start = (page - 1) * _SESSION_PAGE_SIZE
                for session in sessions[start : start + _SESSION_PAGE_SIZE]:
                    selected = bool(active and session.id == active.id)
                    marker = "▶" if selected else "•"
                    title = _label(session.title) if session.title else "Untitled dialogue"
                    lines += ["", f"{marker} #{session.id} — {title}"]
                    if session.binding or session.availability == "restore-unavailable":
                        lines.append(f"State: {_session_state(session)}")
                    lines.append(f"Last used: {session.last_used_at:%Y-%m-%d %H:%M} UTC")
                    lines.append(f"Folder: {_label(session.cwd, 500)}")
                lines += ["", "▶ Selected dialogue", "Switch: /switch ID · New: /new"]
                if pages > 1:
                    lines.append(f"Page {page}/{pages} · Open page: /sessions PAGE")
            await self._send(chat, "\n".join(lines))
        elif command == "/switch":
            if not argument.isascii() or not argument.isdecimal():
                raise GatewayError("Use /switch SESSION_ID.")
            session = self.core.switch_dialogue(conv, int(argument))
            await self._send(chat, f"Selected dialogue #{session.id}\n{_dialogue_summary(session)}")
        elif command == "/delivery":
            job = self.core.job(argument)
            if job.conversation != conv:
                raise GatewayError("This chat does not own that job.")
            delivery = self.core.store.deliveries.get(self.namespace, job.id)
            await self._send(
                chat,
                f"Delivery · {job.id}\nState: {delivery['state']}\n"
                f"Acknowledged fragments: {delivery['next_chunk']}\n\n"
                f"Saved answer: /result {job.id}"
                if delivery
                else "No automatic delivery record for this job.",
            )
        elif command == "/result":
            job = self.core.job(argument)
            if job.conversation != conv:
                raise GatewayError("This chat does not own that job.")
            if not self._queue_delivery(
                chat, job.answer or job.error or f"Job {job.id}: {job.status.value}"
            ):
                await self._send(chat, "Delivery is busy; try /result again shortly.")
        elif command == "/stop":
            jobs = await self.core.cancel(conv)
            await self._send(chat, f"Stopped jobs: {len(jobs)}")
        elif command.startswith("/"):
            await self._send(chat, "Unknown command. Use /help.")
        else:
            job = await self.core.submit_selected(conv, text, delivery_namespace=self.namespace)
            status_text = f"Working: {job.id}\nUse /stop to cancel."
            message = await self._send(chat, status_text)
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

    async def _approval(self, chat, approval):
        options = tuple(
            o for o in approval.request.options if o.kind in {"allow_once", "reject_once"}
        )
        if not options:
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=redact_text(o.name)[:50], callback_data=f"a:{approval.id}:{i}"
                    )
                    for i, o in enumerate(options)
                ]
            ]
        )
        conversation = approval.conversation
        origin = (
            "this chat"
            if conversation.channel == self.name
            else f"{conversation.channel}:{conversation.key}"
        )
        # Redact before truncating: a cut-off secret no longer matches the registry.
        body = redact_text(
            f"Approval: {conversation.agent}\nFrom: {origin}\nJob: {approval.job_id}\n"
            f"{approval.request.title}\n"
            f"{json.dumps(redact_value(approval.request.raw_input), ensure_ascii=False)}\n"
            f"Expires: {approval.expires_at.isoformat()}"
        )[:1800]
        message = await self._send(chat, body, markup=markup)
        self._buttons[(chat, message.message_id)] = (
            approval.id,
            tuple(o.option_id for o in options),
        )

    async def _callback(self, query: CallbackQuery):
        message = query.message
        if not isinstance(message, Message) or not self._authorized(query.from_user, message.chat):
            return
        try:
            binding = self._buttons.get((message.chat.id, message.message_id))
            parts = (query.data or "").split(":")
            if (
                not binding
                or len(parts) != 3
                or parts[0] != "a"
                or parts[1] != binding[0]
                or not parts[2].isdigit()
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
            await self.bot.answer_callback_query(query.id, text="Decision recorded.")
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
                            with contextlib.suppress(TelegramAPIError):
                                await self.bot.edit_message_reply_markup(
                                    chat_id=key[0], message_id=key[1], reply_markup=None
                                )
            except Exception:
                self._log.exception(
                    "Telegram event could not be delivered; use /result or /approvals"
                )
