"""Real aiogram methods/types without contacting the Telegram Bot API."""

import asyncio
from datetime import UTC, datetime

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, GetUpdates, SendMessage
from aiogram.types import Chat, Message, User


def answer_body(text):
    """Assert answer content separately from the independently tested source footer."""
    return text.rsplit("\n\n────────────────────\n", 1)[0]


class FakeTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.sent = []
        self.failures = []
        self.edit_failures = []
        self.closed = False

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.01)
            return []
        self.calls.append(method)
        if isinstance(method, SendMessage):
            if self.failures:
                raise self.failures.pop(0)(method)
            msg = Message(
                message_id=len(self.sent) + 1,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
                text=method.text,
                reply_markup=method.reply_markup,
            )
            self.sent.append(msg)
            return msg
        if isinstance(method, EditMessageText):
            if self.edit_failures:
                raise self.edit_failures.pop(0)(method)
            for index, old in enumerate(self.sent):
                if old.message_id == method.message_id and old.chat.id == method.chat_id:
                    message = old.model_copy(
                        update={"text": method.text, "reply_markup": method.reply_markup}
                    )
                    self.sent[index] = message
                    return message
            raise TelegramBadRequest(method, "message to edit not found")
        return True

    async def stream_content(self, url, **kwargs):
        yield b""
