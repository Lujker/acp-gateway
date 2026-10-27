"""One durable, opt-in context message per private chat; no agent RPC."""

# ruff: noqa: RUF001
import asyncio
import contextlib
import time

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from acp_gateway.channels.telegram_dashboard import agent_kind, label, model_line
from acp_gateway.channels.telegram_format import system_chunks
from acp_gateway.core import GatewayError


class ContextCard:
    def _navigation_selection(self, chat):
        draft = getattr(self, "_profile_drafts", {}).get(chat)
        rename = self._rename_input(chat)
        cwd = self._cwd_input(chat)
        form = (
            ("profile", draft.get("token"), draft["input"], draft["expires"])
            if draft and draft.get("input")
            else ("cwd", cwd["token"], cwd["expires"])
            if cwd
            else ("rename", rename["token"], rename["expires"])
            if rename
            else None
        )
        return self._ui_context(chat), form

    def _remember_navigation(self, chat, message_id, body, text, markup, selection):
        key = (chat, message_id)
        self._navigation_cards[key] = {
            "body": body,
            "text": text,
            "markup": markup,
            "retry_at": 0,
            "context": selection,
        }
        self._navigation_cards.move_to_end(key)
        while len(self._navigation_cards) > 32:
            self._navigation_cards.popitem(last=False)

    async def _refresh_navigation(self, chat):
        """Refresh bounded, advisory snapshots without renewing callback authority."""
        for key, record in list(self._navigation_cards.items()):
            if key[0] != chat or time.monotonic() < record["retry_at"]:
                continue
            editor = self._panels.editor(*key)
            revision = editor.revision
            async with editor.lock:
                if self._navigation_cards.get(key) is not record or editor.revision != revision:
                    continue
                body = record["body"]
                if record["context"] != self._navigation_selection(chat):
                    body += (
                        "\n\nСодержимое панели — до смены выбора или формы ввода. "
                        "Откройте /menu для обновления действий."
                    )
                text = body + "\n\n" + self._destination_text(chat)
                chunks = system_chunks(text)
                if text == record["text"]:
                    continue
                if len(chunks) != 1:
                    # Do not replace a bounded panel with truncated content or another send.
                    self._navigation_cards.pop(key, None)
                    continue
                try:
                    await self.bot.edit_message_text(
                        chat_id=chat,
                        message_id=key[1],
                        text=chunks[0],
                        parse_mode="HTML",
                        reply_markup=record["markup"],
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        request_timeout=10,
                    )
                except TelegramRetryAfter as exc:
                    record["retry_at"] = time.monotonic() + max(1, exc.retry_after)
                    continue
                except TelegramBadRequest as exc:
                    if "message is not modified" not in exc.message.lower():
                        self._navigation_cards.pop(key, None)
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # An ambiguous edit is never replayed; the footer remains a snapshot.
                    self._navigation_cards.pop(key, None)
                    continue
                record["text"] = text

    def _model_observation_lines(self, client, active):
        kind = agent_kind(client.profile.kind, client.profile.display_name)
        model = self.core.observed_model(active) if active else None
        if not model or not model.get("observed_model"):
            if active and client.profile.kind == "hermes":
                # Hermes reports its model only on request; panels never call the agent.
                return [f"{kind} · модель ещё не запрашивалась · /model"]
            if active and client.profile.kind == "claude_code":
                return [f"{kind} · модель станет известна после первого ответа"]
            return [f"{kind} · модель неизвестна"]
        lines = [
            model_line(
                kind,
                model.get("observed_provider"),
                model["observed_model"],
                model.get("observed_effort"),
            )
            + " (последнее наблюдение)"
        ]
        if model["state"] in {"intent", "pending", "unknown"}:
            lines.append("Смена модели ещё не подтверждена.")
        return lines

    def _destination_text(self, chat):
        """A local snapshot only: navigation must never initialize an agent."""
        lines = ["────────────────────", "Выбор на момент обновления панели:"]
        draft = getattr(self, "_profile_drafts", {}).get(chat)
        if draft and draft.get("input"):
            field = {"cwd": "рабочая папка", "alias": "alias", "title": "название"}[draft["input"]]
            lines.append(
                "✎ Ввод профиля истёк; следующий текст не отправится агенту."
                if time.time() > draft["expires"]
                else f"✎ Следующий текст → {field} профиля на ПК {label(draft['computer'], 60)}"
            )
            lines.append("/cancel — закрыть ввод")
            return "\n".join(lines)
        cwd = self._cwd_input(chat)
        if cwd:
            lines.append(
                "✎ Ввод папки истёк; следующий текст не отправится агенту."
                if time.time() > cwd["expires"]
                else "✎ Следующий текст → папка нового диалога"
            )
            lines.append("/cancel — закрыть ввод")
            return "\n".join(lines)
        pending = self._rename_input(chat)
        if pending:
            lines.append(
                "✎ Ввод названия истёк; следующий текст не отправится агенту."
                if time.time() > pending["expires"]
                else f"✎ Следующий текст → название диалога #{pending['id']}"
            )
            lines.append("/cancel — закрыть ввод")
            return "\n".join(lines)
        try:
            conv = self._conversation(chat)
        except GatewayError:
            lines.append("✉ Маршрут недоступен; выберите агента в /menu.")
            return "\n".join(lines)
        client = self.core.agent(conv.agent)
        active = self.core.active_session(conv)
        if active:
            lines.append(
                f"✉ Следующий текст → «{label(active.title or 'Без названия', 80)}» #{active.id}"
            )
            if active.availability in {"restore-unavailable", "interrupted", "uncertain"}:
                lines.append("Продолжение требует восстановления или проверки состояния.")
        else:
            lines.append("✉ Диалог не выбран; текст не отправится агенту.")
        lines.extend(self._model_observation_lines(client, active))
        profile = client.profile
        if profile.backend == "connector":
            computers = self.connector_dispatcher.status() if self.connector_dispatcher else []
            computer = next(
                (item for item in computers if item["computer_id"] == profile.computer_id), None
            )
            host = (
                computer.get("display_name") or profile.computer_id
                if computer
                else profile.computer_id
            )
            lines.append(f"ПК: {label(host, 60)} · агент: {label(conv.agent, 80)}")
            if not computer or not computer["connected"] or not computer["enabled"]:
                lines.append("ПК недоступен.")
            elif profile.alias not in computer.get("advertised_agents", []):
                lines.append("Агент не объявлен коннектором.")
        else:
            lines.append(f"Агент: {label(conv.agent, 80)} · прямое подключение")
        lines.append(
            f"Папка: {label(active.cwd, 250)}"
            if active
            else f"Папка новых диалогов: {label(profile.default_cwd, 250)}"
        )
        lines.append("/context — актуальный выбор")
        return "\n".join(lines)

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

    def _context_text(self, chat, *, hint=True):
        lines = ["Текущий контекст · адресат и режим ввода"]
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
        if self._navigation_selection(chat)[1] is not None:
            lines += ["", *self._destination_text(chat).splitlines()[2:]]
        else:
            lines.extend(self._model_observation_lines(client, active))
        if hint:
            lines += ["", "ℹ️ Снимок обновляется автоматически. Если устарел, откройте /context."]
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
                            text=system_chunks(text)[0],
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
                    await self._refresh_navigation(chat)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._log.warning("Telegram navigation snapshot update unavailable")
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
