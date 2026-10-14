"""Bound, short-lived inline controls; callback data never contains a target."""

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, replace

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from acp_gateway.core import GatewayError


@dataclass(frozen=True)
class _Action:
    chat: int
    command: str
    context: tuple | None
    selection: int | None
    once: bool
    expires: float
    message_id: int | None = None


class Panels:
    def __init__(self):
        self.actions = OrderedDict()

    def create(self, chat, rows, context):
        tokens, keyboard = [], []
        for row in rows:
            buttons = []
            for label, command in row:
                verb = command.split(" ", 1)[0]
                bound = verb in {"/new", "/sessions", "/switch", "/model", "/models", "/result"}
                selected = verb in {"/new", "/switch", "/model", "/models"}
                once = verb in {"/new", "/switch"} or command.startswith(
                    ("/agent ", "cancel:", "/model set ", "/model default ")
                )
                token = secrets.token_hex(12)
                self.actions[token] = _Action(
                    chat,
                    command,
                    context[:2] if bound else None,
                    context[2] if selected else None,
                    once,
                    time.monotonic() + 3600,
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
        return action.command

    def clear(self):
        self.actions.clear()
