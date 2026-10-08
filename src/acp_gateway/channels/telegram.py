"""Private, allowlisted Telegram chats and authenticated human approvals."""

import asyncio
import contextlib
import html
import json
import time
from collections import deque

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.methods import GetUpdates
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pydantic import SecretStr

from acp_gateway.agents import AgentError
from acp_gateway.channels.base import Channel
from acp_gateway.config import TelegramSettings
from acp_gateway.core import GatewayError
from acp_gateway.core.bus import ApprovalRequested, ApprovalResolved, JobFinished
from acp_gateway.log import get_logger, redact_text, redact_value, register_secret
from acp_gateway.storage import Conversation

HELP = (
    "Send a message to your agent.\n"
    "/start, /help — help\n/agent ALIAS — select agent\n"
    "/new [ALIAS] — new session\n/sessions — list sessions\n"
    "/switch ID — activate a session\n/status — connections and jobs\n"
    "/result JOB_ID — saved result\n/stop — cancel this chat's jobs\n"
    "/approvals — pending approval buttons\n"
    "Approvals offer allow once/reject once; permanent approval is unavailable."
)


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

    def __init__(self, settings: TelegramSettings, token: SecretStr, *, bot=None):
        register_secret(token.get_secret_value())
        self.settings = settings.model_copy(deep=True)
        self.bot = bot or Bot(
            token.get_secret_value(), default=DefaultBotProperties(parse_mode="HTML")
        )
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
        self._tasks = [asyncio.create_task(self._poll()), asyncio.create_task(self._events())]

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
        await self.bot.session.close()

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
        text = redact_text(text)
        # Telegram rejects whitespace-only messages, which would abort the rest.
        chunks = [
            chunk for i in range(0, len(text), 2000) if (chunk := text[i : i + 2000]).strip()
        ] or ["(empty response)"]
        for chunk in chunks:
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
        if command in {"/agent", "/new"} and argument:
            self.core.agent(argument)
            self.core.store.set_channel_state(self.namespace, f"agent:{chat}", argument)
        if command == "/agent":
            await self._send(chat, f"Agent: {self._conversation(chat).agent}")
            return
        if command == "/status":
            lines = [
                f"{a}: {'connected' if c.connected else 'disconnected'}"
                for a, c in self.core.agents.items()
            ]
            lines += [
                f"{c.name}: {'connected' if c.connected else 'unavailable'}"
                for c in self.core.channels.values()
            ]
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
        conv = self._conversation(chat)
        if command == "/new":
            session = await self.core.new_session(conv)
            await self._send(chat, f"New session: {session.id}")
        elif command == "/sessions":
            active = self.core.active_session(conv)
            lines = [
                f"{s.id}{' *' if active and s.id == active.id else ''}: {s.title or s.cwd}"
                for s in self.core.sessions(conv)
            ]
            await self._send(chat, "\n".join(lines) or "No sessions; use /new.")
        elif command == "/switch":
            if not argument.isdigit():
                raise GatewayError("Use /switch SESSION_ID.")
            session = self.core.store.session(int(argument))
            if session is None or session.conversation != conv:
                raise GatewayError("This chat does not own that session.")
            self.core.switch_session(conv, session.acp_session_id)
            await self._send(chat, f"Active session: {session.id}")
        elif command == "/result":
            job = self.core.job(argument)
            if job.conversation != conv:
                raise GatewayError("This chat does not own that job.")
            await self._send(chat, job.answer or job.error or f"Job {job.id}: {job.status.value}")
        elif command == "/stop":
            jobs = await self.core.cancel(conv)
            await self._send(chat, f"Stopped jobs: {len(jobs)}")
        elif command.startswith("/"):
            await self._send(chat, "Unknown command. Use /help.")
        else:
            job = await self.core.submit(conv, text)
            await self._send(chat, f"Working: {job.id}\nUse /stop to cancel.")

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
                    if chat in self._chats:
                        await self._send(
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
