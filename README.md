# ACP Gateway

Локальная шина, которая принимает сообщения из внешних каналов (Hermes,
Telegram, позже Snikket/XMPP, Email, Web UI) и доставляет их ACP-агенту —
в первую очередь Work Goose (`goose serve`), — а ответы и запросы
подтверждений возвращает обратно в каналы.

- План и очередь работ — [`road-map.md`](road-map.md)
- Журнал решений — [`road-notes.md`](road-notes.md)
- Архитектура — [`docs/architecture.md`](docs/architecture.md)

Статус: каркас (`P0.3`). Подключения к агенту ещё нет.

## Разработка

Нужен [uv](https://docs.astral.sh/uv/); Python 3.12 uv поставит сам.

```bash
uv sync                                # окружение и зависимости
git config core.hooksPath .githooks    # pre-commit: проверка секретов + ruff
uv run pytest                          # тесты
uv run ruff check && uv run ruff format --check
```

## Конфигурация

```bash
cp config.example.yaml config.yaml     # структура: агенты, политика, логирование
cp .env.example .env && chmod 600 .env # только секреты
uv run acpgw config check              # проверить конфиг; секреты не печатаются
uv run acpgw paths                     # каталоги конфига и данных на этой платформе
```

Порядок поиска: `--config` / `ACPGW_CONFIG`, затем `./config.yaml`, затем
каталог конфига платформы; для секретов — `--env-file` / `ACPGW_ENV_FILE`,
`./.env`, каталог конфига. Любой параметр можно переопределить переменной
окружения `ACPGW_<РАЗДЕЛ>__<ПОЛЕ>`, например `ACPGW_LOGGING__LEVEL=DEBUG`.

Инварианты, которые проверяет загрузка конфига:

- локальный API слушает только loopback;
- нешифрованный транспорт к агенту — только на loopback (иначе нужен явный
  `allow_insecure_transport: true`);
- секреты лежат только в `.env` или окружении и маскируются в логах.
