"""Read-only dialogue history and status; neither action changes the text route."""

# ruff: noqa: RUF001
import secrets
import time

from acp_gateway.agents import AgentError
from acp_gateway.channels.telegram_dashboard import agent_kind, label, model_line, state
from acp_gateway.connectors.policy import LocalPolicyError, _cwd
from acp_gateway.core import GatewayError
from acp_gateway.log import redact_text

HISTORY_FRAGMENT = 900


class DialogueCards:
    def _result_footer(self, chat, job):
        source = self.core.store.job_context(job.id)
        record = self.core.store.session(job.session_id)
        title = source.get("title") if source else record.title if record else None
        lines = [f"Ответ из «{label(title or 'Без названия', 60)}» #{job.session_id}"]
        if source:
            try:
                fallback = self.core.agent(job.conversation.agent).profile.display_name
            except GatewayError:
                fallback = job.conversation.agent
            kind = agent_kind(source["kind"], fallback)
            if source.get("model"):
                phase = (
                    "наблюдение native init"
                    if source.get("model_observation_source") == "claude.system.init"
                    else "наблюдение перед запросом"
                )
                lines.append(
                    model_line(kind, source.get("provider"), source["model"], source.get("effort"))
                    + f" ({phase})"
                )
            else:
                lines.append(f"{kind} · модель неизвестна")
            host = label(source.get("computer_id") or "прямое подключение", 60)
            cwd = source["cwd"]
        else:
            host = "исторические параметры ПК/модели не сохранены"
            cwd = record.cwd if record else "неизвестна"
        lines += [f"{label(job.conversation.agent, 80)} · {host}", f"Папка: {label(cwd, 200)}"]
        try:
            conv = self._conversation(chat)
            active = self.core.active_session(conv)
        except GatewayError:
            active = None
        if active and active.id == job.session_id and active.conversation == job.conversation:
            lines.append("✉ На момент отправки выбран этот диалог.")
        elif active:
            lines.append(
                f"✉ На момент отправки текст → «{label(active.title or 'Без названия', 60)}»"
                f" #{active.id} · {label(active.conversation.agent, 80)}"
            )
        else:
            lines.append("✉ На момент отправки доступный диалог не выбран.")
        return "\n".join(lines)

    def _cwd_input(self, chat):
        return self.core.store.channel_state(self.namespace, f"cwd_input:{chat}")

    def _clear_cwd_input(self, chat):
        self.core.store.set_channel_state(self.namespace, f"cwd_input:{chat}", None)

    async def _cwd_message(self, chat, text):
        pending = self._cwd_input(chat)
        if not pending:
            return False
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0]
        if command == "/cwd_cancel" and argument != pending["token"]:
            raise GatewayError("Этот ввод папки уже закрыт. Откройте карточку диалога.")
        if self._known_telegram_command(command):
            self._clear_cwd_input(chat)
            if command in {"/cancel", "/cwd_cancel"}:
                await self._panel(chat, "Ввод папки отменён.", self._back_rows())
                return True
            return False
        if time.time() > pending["expires"] or list(self._ui_context(chat)) != pending["context"]:
            self._clear_cwd_input(chat)
            await self._panel(
                chat,
                "Ввод папки истёк или выбранный диалог изменился. Текст не отправлен агенту.",
                [*self._hold_text(chat, text), *self._back_rows()],
            )
            return True
        record = self.core.owned_dialogue(pending["id"], channel=self.name, key=str(chat))
        if record.created_at.isoformat() != pending["created_at"]:
            self._clear_cwd_input(chat)
            raise GatewayError("Исходный диалог изменился. Откройте его карточку заново.")
        try:
            cwd = _cwd(text.strip())
        except LocalPolicyError:
            raise GatewayError(
                "Введите абсолютный путь без '..' в ОС агента или /cancel."
            ) from None
        context = self._ui_context(chat)
        # Consume before host RPC: even an ambiguous outcome must never retry creation.
        self._clear_cwd_input(chat)
        try:
            created = await self.core.new_session(record.conversation, cwd=cwd)
        except (AgentError, GatewayError):
            await self._panel(
                chat,
                "Создание диалога не подтверждено. Проверьте список диалогов и доступность папки. "
                "Автоматического повтора нет; исходный диалог не изменялся.",
                [[("Диалоги", "/sessions")], *self._back_rows()],
            )
            return True
        current = self._ui_context(chat)
        if current[:2] != context[:2] or (
            context[0] != record.conversation.agent and current[2] != context[2]
        ):
            await self._panel(
                chat,
                f"Диалог #{created.id} создан, но ваш выбор изменился во время создания. "
                "Откройте список; сообщения не отправлялись.",
                self._back_rows(),
            )
            return True
        self._set_route(chat, record.conversation.agent)
        await self._panel(
            chat,
            f"Новый диалог #{created.id}\nПапка: {label(created.cwd, 250)}\n\n"
            "Контекст прежнего диалога не переносился. Отправьте сообщение, чтобы начать.",
            [[("Открыть диалог", f"/dialogue {created.id}")], *self._back_rows()],
        )
        return True

    def _dialogue_return(self, chat, record, page):
        try:
            if self._conversation(chat) == record.conversation:
                return f"/dialogue {record.id} {page}"
        except GatewayError:
            pass
        return f"/recent_dialogue {record.id}"

    async def _dialogues(self, chat, command, argument):
        if command not in {"/history", "/dialogue_status", "/folder", "/cwd_hint"}:
            return False
        parts = argument.split()
        if not parts or not parts[0].isascii() or not parts[0].isdecimal() or len(parts[0]) > 10:
            raise GatewayError("Откройте историю или статус кнопкой в карточке диалога.")
        record = self.core.owned_dialogue(int(parts[0]), channel=self.name, key=str(chat))
        if command in {"/folder", "/cwd_hint"}:
            if len(parts) > 2:
                raise GatewayError("Откройте папку кнопкой в карточке диалога.")
            page = self._dialogue_page(parts[1] if len(parts) == 2 else "1")
            client = self.core.agent(record.conversation.agent)
            supported = client.profile.backend == "remote"
            rows = []
            if command == "/cwd_hint":
                if not supported:
                    raise GatewayError(
                        "Папка закреплена локальным профилем. Настройте другой профиль."
                    )
                self._clear_rename_input(chat)
                pending = {
                    "id": record.id,
                    "created_at": record.created_at.isoformat(),
                    "context": list(self._ui_context(chat)),
                    "expires": time.time() + 300,
                    "token": secrets.token_hex(12),
                }
                self.core.store.set_channel_state(self.namespace, f"cwd_input:{chat}", pending)
                await self._panel(
                    chat,
                    f"Новая папка для нового диалога · {label(record.conversation.agent)}\n\n"
                    "Введите абсолютный путь в ОС агента. Будет создан и выбран новый диалог; "
                    "история и папка старого сохранятся. Агент может отказать в доступе к папке. "
                    "Ввод действует 5 минут; /cancel — отменить.",
                    [[("Отмена", f"/cwd_cancel {pending['token']}")]],
                )
                return True
            text = f"Рабочая папка · диалог #{record.id}\nПапка: {label(record.cwd, 250)}\n\n"
            text += "Папка существующего диалога не меняется. "
            if supported:
                text += "Можно запросить новый диалог в другой папке."
                rows.append([("Новый диалог в другой папке", f"/cwd_hint {record.id} {page}")])
            else:
                text += (
                    "Папка закреплена локальной политикой профиля. "
                    "Для другой папки настройте другой профиль агента."
                )
                if client.profile.backend == "connector":
                    rows.append([("Профили ПК", f"/profiles {client.profile.computer_id}")])
            rows += [[("← Диалог", self._dialogue_return(chat, record, page))], *self._back_rows()]
            await self._panel(chat, text, rows)
            return True
        if command == "/dialogue_status":
            if len(parts) > 2:
                raise GatewayError("Откройте статус кнопкой в карточке диалога.")
            page = self._dialogue_page(parts[1] if len(parts) == 2 else "1")
            jobs = [
                j for j in self.core.running_jobs(record.conversation) if j.session_id == record.id
            ]
            lines = [
                f"Статус диалога #{record.id} · {label(record.title or 'Без названия')}",
                f"Агент / ПК: {label(record.conversation.agent)}",
                f"Папка: {label(record.cwd, 250)}",
                f"Состояние подключения: {label(record.availability)}",
                f"Активных задач шлюза: {len(jobs)}",
                "Это статус задач, а не счётчик живых процессов агента.",
            ]
            rows = [[(f"{state(j)} · {j.id}", f"/job {j.id}")] for j in jobs]
            rows += [
                [("↻ Обновить", f"/dialogue_status {record.id} {page}")],
                [("История", f"/history {record.id} - 1 {page}")],
                [("← Диалог", self._dialogue_return(chat, record, page))],
                *self._back_rows(),
            ]
            await self._panel(chat, "\n".join(lines), rows)
            return True
        if len(parts) > 4:
            raise GatewayError("Откройте историю кнопкой в карточке диалога.")
        job_id = parts[1] if len(parts) > 1 and parts[1] != "-" else None
        part = self._dialogue_page(parts[2] if len(parts) > 2 else "1")
        page = self._dialogue_page(parts[3] if len(parts) > 3 else "1")
        result = self.core.dialogue_history(
            record.id, channel=self.name, key=str(chat), job_id=job_id
        )
        title = f"История шлюза · «{label(record.title or 'Без названия', 60)}» #{record.id}"
        rows = []
        turn = result["turn"]
        if turn is None:
            text = (
                title
                + "\n\nСохранённых сообщений нет. Старые записи могли истечь по сроку хранения."
            )
        else:
            prompt = (
                turn["prompt"] if turn["prompt"] is not None else "[Исходный запрос не сохранён]"
            )
            answer = turn["answer"] or turn["error"] or "[Ответа пока нет]"
            # Redact before slicing: secrets must not leak across fragment boundaries.
            body = redact_text(f"Вы:\n{prompt}\n\nАгент:\n{answer}")
            fragments = max(1, (len(body) + HISTORY_FRAGMENT - 1) // HISTORY_FRAGMENT)
            if part > fragments:
                raise GatewayError("Фрагмент истории недоступен. Откройте запись заново.")
            cursor = f"/history {record.id} {turn['id']}"
            text = (
                f"{title}\nЗадача: {turn['id']} · {label(turn['status'])}\n"
                f"Фрагмент {part}/{fragments}\n\n"
                + body[(part - 1) * HISTORY_FRAGMENT : part * HISTORY_FRAGMENT]
            )
            navigation = []
            if part > 1:
                navigation.append(("← Текст", f"{cursor} {part - 1} {page}"))
            if part < fragments:
                navigation.append(("Текст →", f"{cursor} {part + 1} {page}"))
            if navigation:
                rows.append(navigation)
            navigation = []
            if result["older"]:
                navigation.append(("← Раньше", f"/history {record.id} {result['older']} 1 {page}"))
            if result["newer"]:
                navigation.append(("Позже →", f"/history {record.id} {result['newer']} 1 {page}"))
            if navigation:
                rows.append(navigation)
            rows.append([("Задача и результат", f"/job {turn['id']}")])
        text += (
            "\n\nИстория шлюза может быть неполной: старые запросы и удалённые записи недоступны."
        )
        rows += [
            [("К последней записи", f"/history {record.id} - 1 {page}")],
            [("← Диалог", self._dialogue_return(chat, record, page))],
            *self._back_rows(),
        ]
        await self._panel(chat, text, rows)
        return True

    @staticmethod
    def _dialogue_page(value):
        if not value.isascii() or not value.isdecimal() or len(value) > 6 or int(value) < 1:
            raise GatewayError("Некорректная страница. Откройте карточку диалога заново.")
        return int(value)
