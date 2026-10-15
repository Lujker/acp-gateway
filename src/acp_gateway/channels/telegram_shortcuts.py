"""Chat-scoped recent dialogue previews and versioned favorite routes."""

# ruff: noqa: RUF001
from acp_gateway.channels.telegram_dashboard import label, stamp
from acp_gateway.core import GatewayError


class Shortcuts:
    def _favorites(self, chat):
        return self.core.store.channel_state(
            self.namespace, f"favorites:{chat}", {"revision": 0, "routes": []}
        )

    def _favorite_action(self, chat, alias):
        state = self._favorites(chat)
        saved = alias in state["routes"]
        return (
            "★ Убрать" if saved else "☆ Добавить",
            f"/favorite {'remove' if saved else 'add'} {alias} {state['revision']}",
        )

    def _route_availability(self, alias):
        client = self.core.agents.get(alias)
        if client is None:
            return "маршрут удалён", False
        if client.profile.backend != "connector":
            return "на связи" if client.connected else "подключение не проверено", True
        computers = self.connector_dispatcher.status() if self.connector_dispatcher else []
        pc = next((c for c in computers if c["computer_id"] == client.profile.computer_id), None)
        if pc is None:
            return "нет подключения ПК", False
        if not pc["enabled"]:
            return "доступ выключен", False
        if not pc["connected"]:
            return "ПК не подключен", False
        if client.profile.alias not in pc.get("advertised_agents", []):
            return "агент не объявлен", False
        return "готов" if client.connected else "ПК на связи; агент ещё не проверен", True

    @staticmethod
    def _recent_can_select(record):
        return record.availability != "restore-unavailable" and not (
            record.availability == "uncertain"
            and record.binding
            and record.binding.get("restore") == "disabled"
        )

    async def _shortcuts(self, chat, command, argument):
        if command == "/recent":
            if argument:
                raise GatewayError("Используйте /recent без аргументов.")
            records = self.core.recent_dialogues(self.name, str(chat))
            lines, rows = ["Недавние диалоги · все агенты этого чата"], []
            for record in records:
                status, _ = self._route_availability(record.conversation.agent)
                lines.append(
                    f"#{record.id} · {label(record.title or 'Без названия', 45)}\n"
                    f"{label(record.conversation.agent, 45)} · {status} · "
                    f"{stamp(record.last_used_at.isoformat())}"
                )
                rows.append(
                    [
                        (
                            f"#{record.id} · {label(record.conversation.agent, 20)} · "
                            f"{label(record.title or 'Без названия', 25)}",
                            f"/recent_dialogue {record.id}",
                        )
                    ]
                )
            if not records:
                lines.append("Диалогов пока нет. Выберите агента и создайте новый диалог.")
            rows += [[("↻ Обновить", "/recent"), ("Агенты", "/agent")], *self._back_rows()]
            await self._panel(chat, "\n\n".join(lines), rows)
        elif command in {"/recent_dialogue", "/select_recent"}:
            if not argument.isascii() or not argument.isdecimal() or len(argument) > 19:
                raise GatewayError("Используйте кнопки списка /recent.")
            record = self.core.owned_dialogue(int(argument), channel=self.name, key=str(chat))
            alias = record.conversation.agent
            status, available = self._route_availability(alias)
            selectable = available and self._recent_can_select(record)
            if command == "/select_recent":
                if not selectable:
                    raise GatewayError(
                        "Диалог сейчас недоступен для продолжения. Обновите /recent."
                    )
                self.core.switch_dialogue(record.conversation, record.id)
                self._set_route(chat, alias)
                await self._command(chat, f"/dialogue {record.id}")
                return True
            lines = [
                f"Недавний диалог #{record.id} · {label(record.title or 'Без названия')}",
                f"Агент / ПК: {label(alias)} · {status}",
                f"Папка: {label(record.cwd, 500)}",
                f"Последняя активность: {stamp(record.last_used_at.isoformat())}",
                "Предпросмотр не меняет получателя сообщений.",
            ]
            rows = [[("▶ Выбрать диалог", f"/select_recent {record.id}")]] if selectable else []
            if not self._recent_can_select(record):
                lines.append("Диалог нельзя восстановить. Создайте новый через список агентов.")
            rows += [[("← Недавние", "/recent"), ("История задач", "/jobs")], *self._back_rows()]
            await self._panel(chat, "\n".join(lines), rows)
        elif command == "/favorites":
            if argument and (
                not argument.isascii() or not argument.isdecimal() or len(argument) > 3
            ):
                raise GatewayError("Используйте /favorites [PAGE].")
            state = self._favorites(chat)
            pages = max(1, (len(state["routes"]) + 9) // 10)
            page = int(argument or 1)
            if not 1 <= page <= pages:
                raise GatewayError(f"Выберите страницу от 1 до {pages}.")
            lines, rows = [f"Избранные маршруты · {page}/{pages}"], []
            for alias in state["routes"][(page - 1) * 10 : page * 10]:
                status, available = self._route_availability(alias)
                lines.append(f"{label(alias, 60)} · {status}")
                row = []
                if available:
                    row.append(
                        (
                            f"Выбрать {label(alias, 35)}",
                            f"/select_favorite {alias} {state['revision']}",
                        )
                    )
                row.append(
                    (f"Убрать {label(alias, 35)}", f"/favorite remove {alias} {state['revision']}")
                )
                rows.append(row)
            if not state["routes"]:
                lines.append(
                    "Избранного пока нет. В списке агентов нажмите ☆ Добавить рядом с маршрутом."
                )
            navigation = []
            if page > 1:
                navigation.append(("← Назад", f"/favorites {page - 1}"))
            if page < pages:
                navigation.append(("Далее →", f"/favorites {page + 1}"))
            if navigation:
                rows.append(navigation)
            rows += [
                [("↻ Обновить", f"/favorites {page}"), ("Агенты", "/agent")],
                *self._back_rows(),
            ]
            await self._panel(chat, "\n\n".join(lines), rows)
        elif command in {"/favorite", "/select_favorite"}:
            parts = argument.split()
            expected = 3 if command == "/favorite" else 2
            if (
                len(parts) != expected
                or not parts[-1].isascii()
                or not parts[-1].isdecimal()
                or len(parts[-1]) > 19
            ):
                raise GatewayError("Используйте кнопки списков /agent и /favorites.")
            state = self._favorites(chat)
            if int(parts[-1]) != state["revision"]:
                raise GatewayError("Избранное изменилось. Обновите /favorites.")
            if command == "/select_favorite":
                alias = parts[0]
                if alias not in state["routes"] or not self._route_availability(alias)[1]:
                    raise GatewayError(
                        "Маршрут недоступен. Обновите /favorites или выберите другого агента."
                    )
                await self._command(chat, f"/agent {alias}")
            else:
                action, alias, _ = parts
                routes = list(state["routes"])
                if action == "add":
                    self.core.agent(alias)
                    if alias not in routes:
                        if len(routes) >= 20:
                            raise GatewayError(
                                "Можно сохранить до 20 маршрутов. Сначала уберите ненужный."
                            )
                        routes.append(alias)
                elif action == "remove":
                    if alias in routes:
                        routes.remove(alias)
                else:
                    raise GatewayError("Используйте кнопки списков /agent и /favorites.")
                if routes != state["routes"]:
                    self.core.store.set_channel_state(
                        self.namespace,
                        f"favorites:{chat}",
                        {
                            "revision": state["revision"] + 1,
                            "routes": routes,
                        },
                    )
                await self._command(chat, "/favorites")
        else:
            return False
        return True
