"""Bound, short-lived inline controls; callback data never contains a target."""

import asyncio
import secrets
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from acp_gateway.core import GatewayError

PANEL_CONTEXT = ContextVar("telegram_navigation_panel", default=None)


@dataclass
class _Editor:
    revision: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class _Action:
    chat: int
    command: str
    context: tuple | None
    selection: int | None
    once: bool
    expires: float
    message_id: int | None = None
    panel: bool = False


class Panels:
    def __init__(self):
        self.actions = OrderedDict()
        self.editors = OrderedDict()

    def create(self, chat, rows, context, *, panel=False):
        tokens, keyboard = [], []
        for row in rows:
            buttons = []
            for label, command in row:
                verb = command.split(" ", 1)[0]
                bound = verb in {
                    "/new",
                    "/sessions",
                    "/dialogue",
                    "/rename_hint",
                    "/switch",
                    "/open_dialogue",
                    "/model",
                    "/models",
                    "/mode",
                }
                selected = verb in {
                    "/new",
                    "/switch",
                    "/open_dialogue",
                    "/model",
                    "/models",
                    "/mode",
                }
                once = verb in {"/new", "/switch", "/open_dialogue"} or command.startswith(
                    ("/agent ", "cancel:", "/model set ", "/model default ", "/mode set ")
                )
                token = secrets.token_hex(12)
                self.actions[token] = _Action(
                    chat,
                    command,
                    context[:2] if bound else None,
                    context[2] if selected else None,
                    once,
                    time.monotonic() + 3600,
                    panel=panel,
                )
                tokens.append(token)
                buttons.append(InlineKeyboardButton(text=label[:60], callback_data=f"u:{token}"))
            keyboard.append(buttons)
        while len(self.actions) > 1024:
            self.actions.popitem(last=False)
        return InlineKeyboardMarkup(inline_keyboard=keyboard), tokens

    def bind(self, tokens, message_id):
        for token in tokens:
            if token in self.actions:
                self.actions[token] = replace(self.actions[token], message_id=message_id)

    def take(self, data, chat, message_id, context):
        return self.consume(data, chat, message_id, context).command

    def consume(self, data, chat, message_id, context):
        token = data.removeprefix("u:")
        action = self.actions.get(token)
        if (
            action is None
            or action.chat != chat
            or action.message_id != message_id
            or action.expires < time.monotonic()
        ):
            raise GatewayError("Кнопка устарела. Откройте /menu.")
        if action.context is not None and action.context != context[:2]:
            raise GatewayError("Агент изменился. Откройте /menu заново.")
        if action.selection is not None and action.selection != context[2]:
            raise GatewayError("Диалог изменился. Откройте /menu заново.")
        if action.once:
            del self.actions[token]
        return action

    def begin_edit(self, message):
        key = (message.chat.id, message.message_id)
        editor = self.editors.setdefault(key, _Editor())
        self.editors.move_to_end(key)
        editor.revision += 1
        while len(self.editors) > 256:
            self.editors.popitem(last=False)
        return message, editor, editor.revision

    def current_edit(self, context):
        message, editor, revision = context
        return (
            self.editors.get((message.chat.id, message.message_id)) is editor
            and editor.revision == revision
        )

    def retire_message(self, chat, message_id):
        for token, action in list(self.actions.items()):
            if action.chat == chat and action.message_id == message_id:
                del self.actions[token]

    def discard(self, tokens):
        for token in tokens:
            self.actions.pop(token, None)

    def clear(self):
        self.actions.clear()
        self.editors.clear()
