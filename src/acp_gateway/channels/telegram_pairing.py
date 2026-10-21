"""Durable owner enrollment cards, independent of connector registration I/O."""

import asyncio
import html
import re

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class PairingCards:
    @staticmethod
    def _pairing_text(row):
        return (
            "Compare this exact request and target with the applicant PC. Never "
            "approve an unsolicited request.\n\n"
            f"Request: {row['request_id']}\n"
            f"PC: {row['name']}\n"
            f"Target: {row['target'] or 'NEW COMPUTER'}\n"
            f"Compare: {row['verification']}\n"
            f"Expires: {row['expires']}\n"
            f"/pair_approve {row['request_id']} {row['verification']}\n"
            f"/pair_deny {row['request_id']} {row['verification']}"
        )

    async def _send_pairing_card(self, chat, request_id, *, explicit=False):
        actor = f"telegram:{chat}"
        row = self.pairing.claim_notification(actor, self.namespace, request_id, explicit=explicit)
        if row is None:
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Подтвердить", callback_data=f"p:a:{row['token']}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"p:d:{row['token']}"),
                ]
            ]
        )
        try:
            message = await self._send(chat, self._pairing_text(row), markup=markup)
            self.pairing.settle_notification(
                actor, self.namespace, row["token"], "delivered", message_id=message.message_id
            )
        except TelegramRetryAfter as exc:
            self.pairing.settle_notification(
                actor, self.namespace, row["token"], "pending", retry_after=exc.retry_after
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            self.pairing.settle_notification(actor, self.namespace, row["token"], "failed")
        except asyncio.CancelledError:
            raise  # The durable pre-send claim already says unknown; never auto-resend.
        except Exception:
            self._log.warning("Pairing notification outcome unknown; use /pairing")

    async def _notify_pairing_once(self):
        try:
            owner = self.pairing.owner("local")
        except ValueError:
            return  # Local bootstrap is explicit; never infer ownership from the allowlist.
        for chat in sorted(set(owner["telegram_ids"]) & set(self.settings.allowed_user_ids)):
            for row in self.pairing.pending(f"telegram:{chat}"):
                try:
                    await self._send_pairing_card(chat, row["request_id"])
                except ValueError:
                    continue  # Cancelled/expired while a previous card was being sent.

    async def _pairing_callback(self, query):
        try:
            parts = (query.data or "").split(":")
            if (
                self.pairing is None
                or len(parts) != 3
                or parts[0] != "p"
                or parts[1] not in {"a", "d"}
                or not re.fullmatch(r"[0-9a-f]{32}", parts[2])
            ):
                raise ValueError("Invalid pairing button; open /pairing.")
            result = self.pairing.decide_notification(
                f"telegram:{query.from_user.id}",
                self.namespace,
                parts[2],
                query.message.message_id,
                approve=parts[1] == "a",
            )
        except ValueError as exc:
            await self.bot.answer_callback_query(query.id, text=str(exc)[:180], show_alert=True)
            return
        await self.bot.answer_callback_query(query.id, text="Решение записано.")
        try:
            await self.bot.edit_message_text(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=html.escape(self._pairing_text(result) + "\nPairing: " + result["state"]),
                parse_mode="HTML",
                reply_markup=None,
            )
        except TelegramAPIError:
            self._log.warning("Pairing decision saved; card edit unavailable")

    async def _pairing_notifications(self):
        while True:
            try:
                await self._notify_pairing_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.warning("Pairing notification scan unavailable; use /pairing")
            await asyncio.sleep(1)
