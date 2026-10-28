"""Read-only dispatcher dashboards, scoped to the requesting private chat."""

# ruff: noqa: RUF001
from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.connectors.diagnostics import computer_diagnostics
from acp_gateway.connectors.protocol import ProtocolError, RuntimeSessionRef
from acp_gateway.core import GatewayError
from acp_gateway.log import redact_text


def label(value, limit=80):
    return " ".join(redact_text(str(value)).split())[:limit]


AGENT_KINDS = {"codex": "Codex", "claude_code": "Claude Code", "hermes": "Hermes", "goose": "Goose"}


def agent_kind(kind, fallback=None):
    """Product name of the agent; a generic ACP agent keeps its configured name."""
    return AGENT_KINDS.get(kind) or label(fallback or kind or "Агент", 60)


def model_line(kind, provider, model, effort=None):
    """``Codex · gpt-5 · medium``: unknown provider/effort are omitted, never guessed."""
    parts = [kind, label(provider, 50) if provider else None, label(model, 80)]
    if effort:
        parts.append(label(effort, 20))
    return " · ".join(part for part in parts if part)


def stamp(value):
    return str(value).replace("T", " ").replace("+00:00", " UTC") if value else "—"


def state(job):
    return {
        "running": "⏳ Выполняется",
        "completed": "✅ Завершена",
        "failed": "❌ Ошибка",
        "cancelled": "⏹ Отменена",
        "interrupted": "⚠ Прервана",
    }[job.status.value]


class Dashboard:
    async def _dashboard(self, chat, command, argument):
        if command == "/diagnostics":
            await self._diagnostic_panel(chat, argument)
            return True
        if command == "/runtimes":
            await self._runtime_panel(chat, argument)
            return True
        if command == "/work":
            await self._work_panel(chat, argument)
            return True
        if command == "/jobs":
            active, before = self._history_arguments(argument)
            result = self.core.job_history("telegram", str(chat), active=active, before=before)
            lines = ["Задачи этого чата" + (" · активные" if active else " · последние")]
            rows = []
            for job in result["jobs"]:
                lines.append(
                    f"{state(job)} · {label(job.conversation.agent, 35)} · #{job.session_id}\n"
                    f"{stamp(job.created_at)} · {job.id}"
                )
                rows.append(
                    [
                        (
                            f"{state(job)} · {label(job.conversation.agent, 25)} · {job.id[:8]}",
                            f"/job {job.id} {argument}".rstrip(),
                        )
                    ]
                )
            if not rows:
                lines.append("Задач пока нет." if not active else "Активных задач нет.")
            prefix = "/jobs active" if active else "/jobs"
            if result["next_before"]:
                rows.append([("Еще →", f"{prefix} before {result['next_before']}")])
            rows += [
                [
                    ("↻ К новым", prefix),
                    ("Все задачи" if active else "Активные", "/jobs" if active else "/jobs active"),
                ],
                *self._back_rows(),
            ]
            await self._panel(chat, "\n\n".join(lines), rows)
        elif command == "/job":
            job_id, _, history = argument.partition(" ")
            await self._job_panel(chat, job_id, history=history)
        elif command == "/open_dialogue":
            job = self.core.owned_job(argument, channel="telegram", key=str(chat))
            if not self._can_continue_job(job):
                raise GatewayError(
                    "Диалог нельзя восстановить. Создайте новый диалог из результата."
                )
            self.core.switch_dialogue(job.conversation, job.session_id)
            self._set_route(chat, job.conversation.agent)
            await self._command(chat, f"/dialogue {job.session_id}")
        elif command == "/new_from_job":
            job = self.core.owned_job(argument, channel="telegram", key=str(chat))
            record = self.core.store.session(job.session_id)
            if record is None or record.conversation != job.conversation:
                raise GatewayError("Исходный диалог недоступен. Откройте историю задач.")
            context = self._ui_context(chat)
            session = await self.core.new_session(job.conversation, cwd=record.cwd)
            current = self._ui_context(chat)
            if current[:2] != context[:2] or (
                context[0] != job.conversation.agent and current[2] != context[2]
            ):
                await self._panel(
                    chat,
                    f"Диалог #{session.id} создан для {label(job.conversation.agent)}. "
                    "Ваш выбор изменился во время создания; откройте панель. "
                    "Сообщения не отправлялись.",
                    self._back_rows(),
                )
                return True
            self._set_route(chat, job.conversation.agent)
            await self._panel(
                chat,
                f"Новый диалог #{session.id}\nАгент / ПК: {label(job.conversation.agent)}\n"
                f"Папка: {label(session.cwd, 500)}\n\n"
                "Предыдущий контекст не переносится. Отправьте сообщение, чтобы начать.",
                [[("💬 Открыть диалог", f"/dialogue {session.id}")], *self._back_rows()],
            )
        elif command == "/computers":
            await self._computers_panel(chat, argument)
        elif command in {"/result", "/delivery"}:
            job_id, _, history = (
                argument.partition(" ") if command == "/delivery" else (argument, "", "")
            )
            self._history_arguments(history)
            job = self.core.owned_job(job_id, channel="telegram", key=str(chat))
            if command == "/result":
                if not self._queue_delivery(chat, job.answer or job.error or state(job), job):
                    raise GatewayError("Доставка занята. Повторите позже.")
            else:
                delivery = self.core.store.deliveries.get(self.namespace, job.id)
                lines = [f"Доставка · {job.id}", f"Задача: {state(job)}"]
                if delivery:
                    status, hint = {
                        "waiting": (
                            "Ожидает завершения задачи",
                            "Ответ будет отправлен после завершения задачи.",
                        ),
                        "pending": (
                            "Ожидает отправки",
                            "Диспетчер продолжит отправку автоматически, когда Telegram доступен.",
                        ),
                        "sending": (
                            "Отправляется",
                            "Telegram ещё не подтвердил текущий фрагмент.",
                        ),
                        "delivered": (
                            "Отправлена в Telegram",
                            "Telegram подтвердил все фрагменты. Это не отметка о прочтении.",
                        ),
                        "failed": (
                            "Отправка отклонена",
                            "Автоматическая отправка остановлена. Проверьте доступность бота.",
                        ),
                        "unknown": (
                            "Неизвестно, получен ли ответ",
                            "Часть ответа могла прийти. Автоматического повтора не будет, "
                            "чтобы не дублировать сообщения.",
                        ),
                    }[delivery["state"]]
                    lines += [
                        f"Состояние: {status}",
                        f"Подтверждено фрагментов: {delivery['next_chunk']}",
                        hint,
                    ]
                else:
                    lines.append("Записи автоматической доставки нет.")
                rows = [[("↻ Обновить", f"/delivery {job.id} {history}".rstrip())]]
                if job.status.finished:
                    lines.append(
                        "«Получить сохранённый результат» отправит его целиком ещё раз "
                        "без запуска агента. Уже полученные фрагменты могут повториться."
                    )
                    rows.append([("📄 Получить сохранённый результат", f"result:{job.id}")])
                else:
                    lines.append("Задача ещё выполняется. Обновите карточку после завершения.")
                await self._panel(
                    chat,
                    "\n".join(lines),
                    [
                        *rows,
                        [("← Задача", f"/job {job.id} {history}".rstrip())],
                        *self._back_rows(),
                    ],
                )
        else:
            return False
        return True

    @staticmethod
    def _history_arguments(argument):
        parts = argument.split()
        active = bool(parts and parts[0] == "active")
        if active:
            parts.pop(0)
        if parts and (len(parts) != 2 or parts[0] != "before"):
            raise GatewayError("Используйте /jobs [active] [before JOB_ID].")
        return active, parts[1] if parts else None

    async def _job_panel(self, chat, job_id, *, history=""):
        self._history_arguments(history)
        suffix = f" {history}" if history else ""
        job = self.core.owned_job(job_id, channel="telegram", key=str(chat))
        lines = [
            f"Задача {job.id}",
            state(job),
            f"Агент: {label(job.conversation.agent)}",
            f"Диалог: #{job.session_id}",
            f"Начало: {stamp(job.created_at)}",
        ]
        if job.finished_at:
            lines.append(f"Завершение: {stamp(job.finished_at)}")
        if job.status.value == "cancelled":
            lines.append("Уже выполненные действия не откатываются.")
        if job.error:
            lines.append(f"Ошибка: {label(job.error, 300)}")
        rows = [
            [("↻ Обновить", f"/job {job.id}{suffix}"), ("📄 Результат", f"result:{job.id}")],
            [("🧭 Ход работы", f"/work {job.id}")],
            [("Доставка", f"/delivery {job.id}{suffix}")],
        ]
        try:
            client = self.core.agent(job.conversation.agent)
        except GatewayError:
            client = None
        if client:
            rows.append(
                [
                    (
                        f"💬 Сделать активным {label(job.conversation.agent, 30)} "
                        f"· #{job.session_id}",
                        f"/open_dialogue {job.id}",
                    )
                ]
            )
        if not job.status.finished and client and getattr(client, "supports_cancel", True):
            rows.append([("⏹ Остановить задачу", f"cancel:{job.id}")])
        rows += [[("← Задачи", f"/jobs{suffix}")], *self._back_rows()]
        await self._panel(chat, "\n".join(lines), rows)

    async def _computers_panel(self, chat, argument):
        if self.connector_dispatcher is None:
            await self._panel(
                chat,
                "Подключение компьютеров не настроено.",
                [[("Агенты", "/agent")], *self._back_rows()],
            )
            return
        snapshot = computer_diagnostics(self.core, self.connector_dispatcher)
        computers = sorted(snapshot["computers"], key=lambda row: row["computer_id"])
        parts = argument.split()
        if parts and parts[0] == "id":
            if len(parts) != 2:
                raise GatewayError("Используйте /computers [PAGE | COMPUTER_ID].")
            computer = next((c for c in computers if c["computer_id"] == parts[1]), None)
            if computer is None:
                raise GatewayError("Компьютер не найден. Обновите список.")
            cid = computer["computer_id"]
            lines = [
                f"Компьютер · {label(computer['display_name'])}",
                f"ID: {label(cid)}",
                self._computer_state(computer),
                f"Подключение: {stamp(computer.get('connected_at'))}",
                f"Отключение: {stamp(computer.get('disconnected_at'))}",
            ]
            rows = []
            for route in snapshot["routes"]:
                if route["computer_id"] != cid:
                    continue
                readiness = {
                    "ready": "готов",
                    "offline": "компьютер не подключен",
                    "unadvertised": "коннектор не объявил агента",
                    "not_initialized": "на связи; готовность ещё не проверена",
                }[route["status"]]
                if route.get("last_stream_error") and not route["agent_ready"]:
                    readiness = "на связи; последняя попытка ACP завершилась ошибкой"
                lines.append(f"{label(route['address'])}: {readiness}")
                if computer["connected"] and route["advertised"]:
                    rows.append(
                        [(f"Выбрать {label(route['alias'], 35)}", f"/agent {route['address']}")]
                    )
            if not any(r["computer_id"] == cid for r in snapshot["routes"]):
                lines.append("Агенты пока не обнаружены.")
            if computer.get("agent_discovery"):
                rows.append([("Добавить агента", f"/profiles {cid} discover")])
            rows.append([("⚙ Процессы агентов", f"/runtimes {cid} 1")])
            rows.append([("🩺 Диагностика", f"/diagnostics {cid}")])
            rows += [
                [("Настроить профили", f"/profiles {cid}")],
                [("↻ Обновить", f"/computers id {cid}"), ("← Компьютеры", "/computers")],
                *self._back_rows(),
            ]
        else:
            if len(parts) > 1 or (parts and len(parts[0]) > 6):
                raise GatewayError("Используйте /computers [PAGE].")
            if parts and (not parts[0].isascii() or not parts[0].isdecimal()):
                raise GatewayError("Используйте /computers [PAGE] или /computers id COMPUTER_ID.")
            page = int(argument or "1")
            pages = max(1, (len(computers) + 9) // 10)
            if not 1 <= page <= pages:
                raise GatewayError(f"Выберите страницу от 1 до {pages}.")
            lines = [f"Компьютеры · {len(computers)} · страница {page}/{pages}"]
            rows = []
            for computer in computers[(page - 1) * 10 : page * 10]:
                cid = computer["computer_id"]
                lines.append(
                    f"{label(computer['display_name'], 45)} ({label(cid, 35)}) · "
                    f"{self._computer_state(computer)}"
                )
                rows.append([(label(computer["display_name"], 45), f"/computers id {cid}")])
            if not computers:
                lines.append("Компьютеры пока не зарегистрированы.")
            navigation = []
            if page > 1:
                navigation.append(("← Назад", f"/computers {page - 1}"))
            if page < pages:
                navigation.append(("Далее →", f"/computers {page + 1}"))
            rows += ([navigation] if navigation else []) + [
                [("↻ Обновить", f"/computers {page}")],
                *self._back_rows(),
            ]
        await self._panel(chat, "\n".join(lines), rows)

    async def _runtime_panel(self, chat, argument):
        parts = argument.split()
        if (
            len(parts) != 2
            or len(parts[1]) > 2
            or not parts[1].isascii()
            or not parts[1].isdigit()
            or not 1 <= int(parts[1]) <= 13
        ):
            raise GatewayError("Используйте /runtimes COMPUTER_ID PAGE.")
        cid, page = parts[0], int(parts[1])
        if self.connector_dispatcher is None:
            raise GatewayError("Подключение компьютеров не настроено.")
        computer = next(
            (c for c in self.connector_dispatcher.status() if c["computer_id"] == cid), None
        )
        if computer is None:
            raise GatewayError("Компьютер не найден.")
        rows = [[("← Компьютер", f"/computers id {cid}")], *self._back_rows()]
        if not computer["connected"]:
            await self._panel(chat, "Компьютер не подключён.", rows)
            return
        if not computer.get("runtime_inventory"):
            await self._panel(
                chat, "Инвентаризация не поддерживается этой версией коннектора.", rows
            )
            return
        owned = []
        for dialogue in self.core.store.recent_sessions("telegram", str(chat), 50):
            client = self.core.agents.get(dialogue.conversation.agent)
            if (
                client
                and client.profile.backend == "connector"
                and client.profile.computer_id == cid
                and client.profile.alias in computer.get("advertised_agents", [])
                and 0 < len(dialogue.acp_session_id) <= 128
            ):
                owned.append(
                    (
                        dialogue,
                        RuntimeSessionRef(
                            alias=client.profile.alias, session_id=dialogue.acp_session_id
                        ),
                    )
                )
            if len(owned) == 32:
                break
        try:
            result = await self.connector_dispatcher.runtime(cid, page, [ref for _, ref in owned])
        except (AgentUnavailable, ConnectionError, TimeoutError, ProtocolError):
            await self._panel(chat, "Сведения о процессах сейчас недоступны.", rows)
            return
        if result.status != "ok":
            await self._panel(chat, "Сведения о процессах сейчас недоступны.", rows)
            return
        lines = [f"Процессы агентов · {label(cid)} · {result.page}/{result.pages}"]
        for profile in result.profiles:
            suffix = " · список транспортов ограничен" if profile.truncated else ""
            lines.append(
                f"{label(profile.alias)} · {agent_kind(profile.kind)} · "
                f"{profile.environment}{suffix}\n"
                f"Процессы: {profile.launchers} · диалоги: {profile.dialogues} · "
                f"задачи: {profile.turns}\n"
                f"Запросы: {profile.pending} · подтверждения: {profile.approvals} · "
                f"запуск/очистка: {profile.opening}"
            )
            if profile.uncertain or profile.closing:
                lines.append("⚠ Есть неопределённость или процесс завершается.")
            if profile.idle_seconds is not None:
                lines.append(f"Последняя активность: не менее {profile.idle_seconds} с назад.")
            if profile.idle_timeout is not None:
                lines.append(
                    "Автоочистка выключена."
                    if profile.idle_timeout == 0
                    else f"Автоочистка: после {profile.idle_timeout} с простоя."
                )
            if profile.kind == "claude_code":
                lines.append("Claude: очистка всего транспорта, когда все диалоги простаивают.")
        if not result.profiles:
            lines.append("Управляемые Codex/Claude профили не настроены.")
        if result.matched:
            lines.append("Открытые диалоги этого чата (до 32 из 50 последних):")
            for index in result.matched:
                dialogue = owned[index][0]
                title = self.core.store.channel_state(
                    "dialogue_display_titles",
                    self.core._dialogue_title_key(dialogue),
                    dialogue.title,
                )
                lines.append(f"#{dialogue.id} · {label(title or 'Без названия', 45)}")
        lines.append("Только процессы коннектора; системные процессы не учитываются.")
        nav = []
        if result.page > 1:
            nav.append(("←", f"/runtimes {cid} {result.page - 1}"))
        if result.page < result.pages:
            nav.append(("→", f"/runtimes {cid} {result.page + 1}"))
        if nav:
            rows.insert(0, nav)
        rows.insert(0, [("↻ Обновить", f"/runtimes {cid} {result.page}")])
        await self._panel(chat, "\n\n".join(lines), rows)

    async def _diagnostic_panel(self, chat, argument):
        parts = argument.split()
        if len(parts) not in {1, 2, 3}:
            raise GatewayError("Используйте /diagnostics COMPUTER_ID [AGENT | page N].")
        cid = parts[0]
        alias = parts[1] if len(parts) == 2 else None
        if len(parts) == 3:
            if (
                parts[1] != "page"
                or not parts[2].isascii()
                or not parts[2].isdigit()
                or len(parts[2]) > 2
            ):
                raise GatewayError("Выберите страницу диагностики.")
            page = int(parts[2])
        else:
            page = 1
        if self.connector_dispatcher is None:
            raise GatewayError("Подключение компьютеров не настроено.")
        computer = next(
            (c for c in self.connector_dispatcher.status() if c["computer_id"] == cid), None
        )
        if computer is None:
            raise GatewayError("Компьютер не найден.")
        agents = computer.get("advertised_agents", [])
        pages = max(1, (len(agents) + 7) // 8)
        if not 1 <= page <= pages:
            raise GatewayError("Страница диагностики недоступна.")
        rows = [[("← Компьютер", f"/computers id {cid}")], *self._back_rows()]
        if not computer["connected"]:
            await self._panel(chat, "Компьютер не подключён.", rows)
            return
        if not computer.get("runtime_diagnostics"):
            await self._panel(chat, "Диагностика не поддерживается этой версией коннектора.", rows)
            return
        if alias is not None and alias not in computer.get("advertised_agents", []):
            raise GatewayError("Агент не объявлен этим компьютером.")
        if alias is not None:
            self._profile_owner(chat)
        try:
            result = await self.connector_dispatcher.runtime(
                cid, 1, action="initialize" if alias else "diagnostics", alias=alias
            )
        except (AgentUnavailable, ConnectionError, TimeoutError, ProtocolError):
            await self._panel(chat, "Диагностика временно недоступна.", rows)
            return
        if result.status != "ok" or result.diagnostics is None:
            await self._panel(chat, "Диагностика временно недоступна.", rows)
            return
        diag = result.diagnostics
        activity = {
            "idle": "свободен",
            "busy": "занят",
            "uncertain": "исход работы неизвестен",
            "reserved": "зарезервирован",
            "unsupported": "есть неподдерживаемый агент",
            "unknown": "не подтверждена",
        }
        updater = {
            "ready": "готов",
            "disabled": "отключён",
            "unsupported": "способ установки не поддерживается",
            "worker_stopped": "служба updater не запущена",
            "not_provisioned": "updater ещё не подготовлен",
            "heartbeat_stale": "нет актуального heartbeat updater",
            "scope_mismatch": "updater настроен для другой установки",
            "unknown": "неизвестно",
        }
        lines = [
            f"Диагностика ПК · {label(cid)}",
            f"Версия: {label(diag.version)}",
            f"Активность: {activity[diag.maintenance]}",
            f"Задачи: {diag.running} · запросы: {diag.requests} · "
            f"подтверждения: {diag.approvals} · запуск: {diag.opening}",
            f"Updater: {updater[diag.updater]}",
        ]
        if alias:
            probes = {
                "ok": "initialize выполнен",
                "failed": "initialize не выполнен",
                "busy": "ПК занят обслуживанием или другой проверкой",
                "unavailable": "профиль недоступен",
                "cleanup_unknown": "остановка проверочного процесса не подтверждена",
                "not_run": "не выполнялась",
            }
            lines.append(f"{label(alias)}: {probes[diag.probe]}.")
        lines.append(
            "Проверка агента только инициализирует соединение: диалог и модель не запускаются."
        )
        for name in reversed(agents[(page - 1) * 8 : page * 8]):
            rows.insert(0, [(f"Проверить {label(name, 35)}", f"/diagnostics {cid} {name}")])
        nav = []
        if page > 1:
            nav.append(("←", f"/diagnostics {cid} page {page - 1}"))
        if page < pages:
            nav.append(("→", f"/diagnostics {cid} page {page + 1}"))
        if nav:
            rows.insert(0, nav)
        rows.insert(0, [("↻ Обновить", f"/diagnostics {cid} page {page}")])
        await self._panel(chat, "\n".join(lines), rows)

    @staticmethod
    def _computer_state(computer):
        if not computer["enabled"]:
            return "⛔ Доступ выключен"
        return "🟢 На связи" if computer["connected"] else "⚪ Не подключен"
