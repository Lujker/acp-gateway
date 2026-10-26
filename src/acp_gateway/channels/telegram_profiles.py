"""Owner-only connector profile forms; inputs are never submitted as prompts."""

# ruff: noqa: RUF001
import secrets
import time
from uuid import uuid4

from pydantic import ValidationError

from acp_gateway.channels.telegram_dashboard import label
from acp_gateway.connectors.protocol import ProfileSpec, ProfilesRequest
from acp_gateway.core import GatewayError


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
            expires=state["expires"],
            action=action,
            revision=state.get("revision"),
            **kwargs,
        )
        token = self.core.prepare_maintenance() if action != "list" else None
        try:
            result = await self.connector_dispatcher.profiles(state["computer"], request)
        finally:
            if token is not None:
                self.core.resume_maintenance(token)
        messages = {
            "disabled": "Разрешите connector.remote_profiles локально и перезапустите коннектор.",
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

    async def _profiles(self, chat, text):
        # Import after TelegramChannel is initialized to avoid the mixin import cycle.
        from acp_gateway.channels.telegram import COMMANDS

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0]
        known_command = command in {"/" + name for name, *_ in COMMANDS} | {
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
            "/new_from_job",
            "/open_dialogue",
            "/favorite",
            "/recent_dialogue",
            "/select_favorite",
            "/select_recent",
        }
        forms = self._profile_forms()
        current = forms.get(chat)
        if current and current["expires"] < time.time():
            forms.pop(chat)
            if not known_command and (not command.startswith("/") or current.get("input") == "cwd"):
                raise GatewayError("Ввод профиля устарел. Откройте /profiles КОМПЬЮТЕР.")
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
            state = {
                "computer": argument.strip(),
                "operation": uuid4(),
                "expires": time.time() + 120,
            }
            result = await self._profiles_rpc(chat, state, "list")
            state.update(
                token=secrets.token_hex(4),
                revision=result.revision,
                profiles={p.alias: p.model_dump() for p in result.profiles},
            )
            forms[chat] = state
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
                await self._profiles_rpc(chat, current, operation, **kwargs)
            except (ConnectionError, TimeoutError):
                raise GatewayError(
                    "Ответ не получен; исход неизвестен. Откройте список профилей для проверки."
                ) from None
            forms.pop(chat, None)
            await self._panel(
                chat,
                "Профили применены; каталог диспетчера обновлён.",
                [[("Обновить список", f"/profiles {current['computer']}")], *self._back_rows()],
            )
            return True
        else:
            raise GatewayError("Неизвестное действие профиля.")
        await self._profile_form(chat, current)
        return True

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
