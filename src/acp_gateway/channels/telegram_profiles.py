"""Owner-only connector profile forms; inputs are never submitted as prompts."""

# ruff: noqa: RUF001
import secrets
import time
from uuid import uuid4

from pydantic import ValidationError

from acp_gateway.channels.telegram_dashboard import label
from acp_gateway.connectors.protocol import ProfileSpec, ProfilesRequest
from acp_gateway.core import GatewayError

PROFILE_RPC_TTL = 30


class ProfileCards:
    def _profile_owner(self, chat):
        try:
            if self.pairing is None:
                raise ValueError
            self.pairing.owner(f"telegram:{chat}")
        except ValueError:
            raise GatewayError("Управление профилями доступно владельцу подключения.") from None
        if self.connector_dispatcher is None:
            raise GatewayError("Диспетчер подключений недоступен.")

    async def _profiles_rpc(self, chat, state, action, **kwargs):
        self._profile_owner(chat)
        request = ProfilesRequest(
            epoch=uuid4(),
            request=uuid4(),
            operation=state["operation"],
            actor=f"telegram:{chat}",
            # Leave headroom inside the connector's 120-second bound: its clock
            # can lag the dispatcher. The owner's form expiry remains an upper bound.
            expires=min(state["expires"], time.time() + PROFILE_RPC_TTL),
            action=action,
            revision=state.get("revision"),
            **kwargs,
        )
        token = self.core.prepare_maintenance() if action not in {"list", "discover"} else None
        try:
            result = await self.connector_dispatcher.profiles(state["computer"], request)
        finally:
            if token is not None:
                self.core.resume_maintenance(token)
        messages = {
            "disabled": "На ПК выполните acpgw agents allow-remote-profiles "
            "для разрешения управления.",
            "busy": "Коннектор занят или зарезервирован. Завершите работу и повторите позже.",
            "stale": "Настройки изменились или форма устарела. Откройте список заново.",
            "invalid": "Настройки отклонены. Проверьте имя, существующую папку и тип профиля.",
            "unknown": "Завершение агента не подтверждено. Нужна локальная проверка коннектора.",
        }
        if result.status != "ok":
            raise GatewayError(messages[result.status])
        return result

    def _profile_forms(self):
        if not hasattr(self, "_profile_drafts"):
            self._profile_drafts = {}
        return self._profile_drafts

    def _known_telegram_command(self, command):
        # Import after TelegramChannel is initialized to avoid the mixin import cycle.
        from acp_gateway.channels.telegram import COMMANDS

        return command in {"/" + name for name, *_ in COMMANDS} | {
            "/shell",
            "/shells",
            "/shell_cancel",
            "/shell_select",
            "/shell_send",
            "/cancel",
            "/rename",
            "/rename_cancel",
            "/profile_action",
            "/update_prepare",
            "/update_confirm",
            "/update_cancel",
            "/update_status",
            "/update_history",
            "/updates_check",
            "/approval",
            "/pair_approve",
            "/pair_deny",
            "/rename_hint",
            "/history",
            "/dialogue_status",
            "/folder",
            "/cwd_hint",
            "/cwd_cancel",
            "/new_from_job",
            "/open_dialogue",
            "/favorite",
            "/recent_dialogue",
            "/select_favorite",
            "/select_recent",
            "/resume_dialogue",
            "/release_dialogue",
            "/send_held",
            "/checkpoint",
            "/checkpoint_show",
            "/checkpoint_resume",
            "/checkpoint_new",
        }

    async def _profiles(self, chat, text):
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0]
        known_command = self._known_telegram_command(command)
        forms = self._profile_forms()
        current = forms.get(chat)
        if current and current["expires"] < time.time():
            forms.pop(chat)
            if not known_command and (not command.startswith("/") or current.get("input") == "cwd"):
                await self._panel(
                    chat,
                    "Ввод профиля устарел; это сообщение не отправлено агенту. "
                    "Откройте /profiles КОМПЬЮТЕР, чтобы продолжить настройку.",
                    [*self._hold_text(chat, text), *self._back_rows()],
                )
                return True
            current = None
        if current and current.get("input"):
            if command in {"/cancel", "/menu", "/profiles"}:
                forms.pop(chat)
                if command == "/cancel":
                    await self._panel(chat, "Изменение профиля отменено.", self._back_rows())
                    return True
            elif not command.startswith("/") or (current["input"] == "cwd" and not known_command):
                self._profile_owner(chat)
                value = text.strip()
                field = current["input"]
                candidate = {**current["draft"], field: value}
                if field == "alias":
                    candidate["title"] = value
                try:
                    ProfileSpec.model_validate(candidate)
                except ValidationError:
                    raise GatewayError(
                        "Некорректное значение. Повторите ввод или /cancel."
                    ) from None
                current["draft"] = candidate
                current.pop("input")
                await self._profile_form(chat, current)
                return True
            else:
                forms.pop(chat)
                current = None
        if command == "/profiles":
            self._profile_owner(chat)
            computer, _, option = argument.strip().partition(" ")
            if not computer or option not in {"", "discover"}:
                raise GatewayError("Используйте /profiles КОМПЬЮТЕР.")
            state = {
                "computer": computer,
                "operation": uuid4(),
                "expires": time.time() + 120,
            }
            result = await self._profiles_rpc(chat, state, "discover" if option else "list")
            state.update(
                token=secrets.token_hex(4),
                revision=result.revision,
                profiles={p.alias: p.model_dump() for p in result.profiles},
                candidates={p.id: p.model_dump() for p in result.candidates or []},
            )
            forms[chat] = state
            if option:
                await self._candidate_list(chat, state, result.hints or [])
                return True
            rows = []
            for p in result.profiles:
                rows.append(
                    [
                        (
                            f"{label(p.title, 30)} · {p.mode}",
                            f"/profile_action {state['token']} edit {p.alias}",
                        )
                    ]
                )
            rows.append([("Добавить профиль", f"/profile_action {state['token']} templates")])
            registration = getattr(self.connector_dispatcher, "active", {}).get(computer)
            if registration and registration.relay.agent_discovery:
                rows.append([("Добавить агента", f"/profiles {computer} discover")])
            rows += self._back_rows()
            await self._panel(
                chat,
                f"Профили · {label(state['computer'])}\n"
                "Выберите профиль или добавьте новый на основе установленного агента.",
                rows,
            )
            return True
        if command != "/profile_action":
            return False
        self._profile_owner(chat)
        parts = argument.split()
        if not current or len(parts) not in {2, 3} or parts[0] != current["token"]:
            raise GatewayError("Форма устарела. Откройте /profiles КОМПЬЮТЕР.")
        action = parts[1]
        if action == "candidate":
            if len(parts) != 3 or parts[2] not in current.get("candidates", {}):
                raise GatewayError("Кандидат недоступен. Повторите поиск агентов.")
            candidate = current["candidates"][parts[2]]
            base = candidate["kind"]
            alias = base
            index = 2
            while alias in current["profiles"]:
                alias = f"{base}_{index}"
                index += 1
            current.update(
                draft={
                    "alias": alias,
                    "candidate": candidate["id"],
                    "title": candidate["title"],
                    "cwd": candidate["default_cwd"],
                    "mode": "safe",
                },
                action="add",
                operation=uuid4(),
                input="cwd",
            )
            current.pop("confirmed", None)
            await self._panel(
                chat,
                "Введите существующую абсолютную папку проекта в ОС агента "
                "(для WSL — Linux-путь).\n"
                f"Домашняя папка агента: {label(candidate['default_cwd'], 1000)}",
                self._back_rows(),
            )
            return True
        if action == "templates":
            rows = [
                [(label(p["title"], 40), f"/profile_action {current['token']} add {alias}")]
                for alias, p in current["profiles"].items()
            ]
            await self._panel(
                chat,
                "Выберите установленный агент и среду как основу нового профиля.",
                rows + self._back_rows(),
            )
            return True
        if action in {"edit", "add"}:
            if len(parts) != 3 or parts[2] not in current["profiles"]:
                raise GatewayError("Профиль недоступен. Обновите список.")
            current["draft"] = dict(current["profiles"][parts[2]])
            current["action"] = "update" if action == "edit" else "add"
            current["operation"] = uuid4()
            current.pop("confirmed", None)
            if action == "add":
                current["input"] = "alias"
                await self._panel(
                    chat,
                    "Введите уникальный alias нового профиля: латиница, цифры, "
                    "подчёркивание; начало — буква.",
                    self._back_rows(),
                )
                return True
        elif "draft" not in current:
            raise GatewayError("Сначала выберите профиль.")
        elif action in {"cwd", "title"}:
            current["input"] = action
            await self._panel(
                chat,
                "Введите существующую абсолютную папку в ОС агента (для WSL — Linux-путь)."
                if action == "cwd"
                else "Введите название профиля.",
                self._back_rows(),
            )
            return True
        elif action in {"safe", "work"}:
            current["draft"]["mode"] = action
        elif action in {"save", "delete"}:
            if action == "delete" and current["action"] != "update":
                raise GatewayError("Новый профиль ещё не сохранён.")
            current["confirmed"] = "remove" if action == "delete" else current["action"]
            p = current["draft"]
            await self._panel(
                chat,
                f"Подтвердить: {current['confirmed']} · {label(p['alias'])}\n"
                f"Папка: {label(p['cwd'], 1000)}\nРежим: {p['mode']}\n\n"
                "История и файлы не удаляются. Изменение папки/режима может сделать "
                "старые диалоги недоступными для восстановления. "
                "Work использует настройки и hooks агента.",
                [
                    [("Подтвердить", f"/profile_action {current['token']} apply")],
                    *self._back_rows(),
                ],
            )
            return True
        elif action == "apply":
            operation = current.pop("confirmed", None)
            if operation is None:
                raise GatewayError("Сначала откройте подтверждение изменения.")
            p = ProfileSpec.model_validate(current["draft"])
            kwargs = {"alias": p.alias} if operation == "remove" else {"profile": p}
            try:
                result = await self._profiles_rpc(chat, current, operation, **kwargs)
            except (ConnectionError, TimeoutError):
                raise GatewayError(
                    "Ответ не получен; исход неизвестен. Откройте список профилей для проверки."
                ) from None
            forms.pop(chat, None)
            check = ""
            if result.initialization == "ok":
                check = "\nInitialize пройден; диалоги и модельные запросы не создавались."
            elif result.initialization == "failed":
                check = "\nПрофиль сохранён, но initialize не прошёл. Проверьте CLI локально на ПК."
            await self._panel(
                chat,
                "Профили применены; каталог диспетчера обновлён." + check,
                [[("Обновить список", f"/profiles {current['computer']}")], *self._back_rows()],
            )
            return True
        else:
            raise GatewayError("Неизвестное действие профиля.")
        await self._profile_form(chat, current)
        return True

    async def _candidate_list(self, chat, state, hints):
        rows = [
            [(label(p["title"], 60), f"/profile_action {state['token']} candidate {identity}")]
            for identity, p in state["candidates"].items()
        ]
        # Callback bodies are stored behind short tokens by the Telegram panel renderer.
        text = "Выберите найденного агента." if rows else "Агенты не найдены."
        if hints:
            text += "\n\n" + "\n".join(label(h, 256) for h in hints[:8])
        rows.append([("Повторить поиск", f"/profiles {state['computer']} discover")])
        await self._panel(chat, text, rows + self._back_rows())

    async def _profile_form(self, chat, state):
        state.pop("confirmed", None)
        p, token = state["draft"], state["token"]
        rows = [
            [
                ("Папка", f"/profile_action {token} cwd"),
                ("Название", f"/profile_action {token} title"),
            ],
            [("Safe", f"/profile_action {token} safe"), ("Work", f"/profile_action {token} work")],
            [("Сохранить…", f"/profile_action {token} save")],
        ]
        if state["action"] == "update":
            rows.append([("Удалить…", f"/profile_action {token} delete")])
        await self._panel(
            chat,
            f"Профиль: {label(p['alias'])}\nНазвание: {label(p['title'])}\n"
            f"Папка: {label(p['cwd'], 1000)}\nРежим: {p['mode']}",
            rows + self._back_rows(),
        )
