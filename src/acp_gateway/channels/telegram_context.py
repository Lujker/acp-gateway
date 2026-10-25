"""One durable, opt-in context message per private chat; no agent RPC."""

# ruff: noqa: RUF001
import asyncio
import contextlib
import html
import time

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from acp_gateway.channels.telegram_dashboard import label
from acp_gateway.core import GatewayError


class ContextCard:
    def _agent_profile_text(self, client):
        profile = client.profile
        settings = getattr(profile, profile.kind, None)
        if profile.kind not in {"codex", "claude_code"}:
            return ""
        if settings is not None:
            mode = settings.mode
            environment = "wsl" if settings.wsl else "native"
        elif client.connected:
            caps = getattr(client, "agent_capabilities", None)
            metadata = getattr(caps, "field_meta", None) or {}
            if not isinstance(metadata, dict):
                return ""
            flags = metadata.get("acp-gateway", {})
            if not isinstance(flags, dict):
                return ""
            mode, environment = flags.get("profileMode"), flags.get("executionEnvironment")
        else:
            return ""
        if mode not in {"safe", "work"}:
            return ""
        name = "Рабочий" if mode == "work" else "Безопасный"
        return name + (" · WSL" if environment == "wsl" else "")

    def _context_text(self, chat):
        lines = ["Текущий контекст · сюда пойдут обычные сообщения"]
        alias = self.core.store.channel_state(self.namespace, f"agent:{chat}")
        try:
            conv = self._conversation(chat)
        except GatewayError:
            lines += [f"Агент: {label(alias) if alias else 'не выбран'}", "Маршрут недоступен."]
            return "\n".join([*lines, "Выберите агента в панели /menu."])
        client = self.core.agent(conv.agent)
        profile = client.profile
        lines.append(f"Агент: {label(conv.agent)}")
        mode = self._agent_profile_text(client)
        if mode:
            lines.append("Профиль агента: " + mode)
        if profile.backend == "connector":
            cid = profile.computer_id
            computers = self.connector_dispatcher.status() if self.connector_dispatcher else []
            computer = next((c for c in computers if c["computer_id"] == cid), None)
            name = label(computer.get("display_name") or cid) if computer else label(cid)
            lines.append(f"Компьютер: {name} · {label(cid)}")
            state = (
                "сведения недоступны"
                if self.connector_dispatcher is None
                else "не зарегистрирован"
                if computer is None
                else "доступ выключен"
                if not computer["enabled"]
                else "на связи"
                if computer["connected"]
                else "не подключен"
            )
            lines.append(f"Подключение ПК: {state}")
            advertised = bool(
                computer
                and computer["enabled"]
                and computer["connected"]
                and profile.alias in computer.get("advertised_agents", [])
            )
            readiness = "проверена" if advertised and client.connected else "не проверена"
            if computer and computer["enabled"] and computer["connected"] and not advertised:
                readiness = "не объявлен коннектором"
            lines.append("Готовность агента: " + readiness)
        else:
            lines.append("Компьютер: прямое подключение агента (имя ПК не объявлено)")
            lines.append(
                "Подключение агента: " + ("на связи" if client.connected else "не проверено")
            )
        active = self.core.active_session(conv)
        if active:
            lines += [
                f"Папка: {label(active.cwd, 500)}",
                f"Диалог: #{active.id} · {label(active.title or 'Без названия')}",
                "Состояние: "
                + {
                    "attached": "подключён",
                    "detached": "отсоединён",
                    "unknown": "не проверен",
                    "restore-required": "потребуется подключение к диалогу",
                    "busy": "выполняет задачу",
                    "restore-unavailable": "нельзя восстановить; создайте новый диалог",
                    "uncertain": "предыдущая операция требует проверки",
                }.get(active.availability, active.availability),
            ]
        else:
            lines += [
                f"Папка новых диалогов: {label(profile.default_cwd, 500)}",
                "Диалог: не выбран — создайте или выберите в /menu.",
            ]
        pending = self._rename_input(chat)
        if pending:
            lines.append(
                "Время ввода названия истекло; /cancel — закрыть ввод."
                if time.time() > pending["expires"]
                else "Ввод названия: следующий текст переименует диалог; /cancel — отменить."
            )
        lines.append("Снимок обновляется автоматически. Если устарел, откройте /context.")
        return "\n".join(lines)

    async def _show_context(self, chat, *, explicit=False):
        async with self._context_lock:
            store = self.core.store
            key = f"context_card:{chat}"
            record = store.channel_state(self.namespace, key)
            if not explicit and (
                not record or record["state"] != "ready" or time.time() < record.get("retry_at", 0)
            ):
                return
            text = self._context_text(chat)
            if (
                record
                and record["state"] == "ready"
                and text == record["text"]
                and chat in self._context_bound
                and not explicit
            ):
                return
            # Old buttons remain harmless on ambiguity; all are navigation-only.
            rows = [[("Обновить контекст", "/context"), ("Панель", "/menu")]]
            markup, tokens = self._panels.create(chat, rows, self._ui_context(chat))
            previous = record.get("message_id") if record else None
            new_message = not record or record["state"] != "ready"
            store.set_channel_state(
                self.namespace,
                key,
                {
                    "state": "unknown",
                    "message_id": previous,
                },
            )
            try:
                if not new_message:
                    try:
                        await self.bot.edit_message_text(
                            chat_id=chat,
                            message_id=previous,
                            text=html.escape(text, quote=False),
                            parse_mode="HTML",
                            reply_markup=markup,
                            request_timeout=10,
                        )
                    except TelegramBadRequest as exc:
                        message = exc.message.lower()
                        if "message is not modified" in message:
                            pass
                        elif "message to edit not found" in message:
                            new_message = True
                        else:
                            raise
                if new_message:
                    # A crash or timeout after send must not produce an automatic duplicate.
                    store.set_channel_state(self.namespace, key, {"state": "unknown"})
                    sent = await self._send(chat, text, markup=markup)
                    previous = sent.message_id
                if record and record.get("message_id"):
                    self._panels.retire_message(chat, record["message_id"])
                self._panels.bind(tokens, previous)
                self._context_bound.add(chat)
                store.set_channel_state(
                    self.namespace,
                    key,
                    {
                        "state": "ready",
                        "message_id": previous,
                        "text": text,
                    },
                )
            except TelegramRetryAfter as exc:
                self._panels.discard(tokens)
                if record and not new_message:
                    store.set_channel_state(
                        self.namespace,
                        key,
                        {
                            **record,
                            "retry_at": time.time() + exc.retry_after,
                        },
                    )
                else:
                    store.set_channel_state(self.namespace, key, {"state": "unknown"})
                if explicit:
                    raise
            except BaseException:
                self._panels.discard(tokens)
                if record and record.get("message_id"):
                    self._panels.retire_message(chat, record["message_id"])
                store.set_channel_state(
                    self.namespace,
                    key,
                    {
                        "state": "unknown",
                        "message_id": previous,
                    },
                )
                raise

    async def _context_updates(self):
        while True:
            self._context_wake.clear()
            for chat in list(self._chats):
                if chat not in self.settings.allowed_user_ids:
                    continue
                try:
                    await self._show_context(chat)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._log.warning("Telegram context update failed; use /context")
                    # Unknown sends/edits are fenced; a separate notice cannot block approvals.
                    with contextlib.suppress(Exception):
                        record = self.core.store.channel_state(
                            self.namespace, f"context_card:{chat}"
                        )
                        if record and record["state"] == "unknown":
                            self._queue_delivery(
                                chat,
                                "Не удалось обновить карточку контекста. "
                                "Она может быть устаревшей. "
                                "Откройте /context, чтобы явно обновить её.",
                            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._context_wake.wait(), 5)
