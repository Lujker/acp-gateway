# ACP Gateway — целевая архитектура

> Статус документа: действующая целевая архитектура. Обновляется, когда
> решение меняется; хронология решений и находок — в
> [`road-notes.md`](../road-notes.md), статусы и очередь работ — в
> [`road-map.md`](../road-map.md).
> Исходная версия плана (до разбора 2026-10-06) —
> [`archive/2026-10-06-initial-plan.md`](archive/2026-10-06-initial-plan.md).

## 1. Цель

**ACP Gateway** — локальная шина, которая принимает сообщения из внешних
каналов (Hermes, Telegram, позже Snikket/XMPP, Email, Web UI и другие) и
доставляет их ACP-совместимому агенту, а ответы, события и запросы
подтверждений — обратно в каналы.

Gateway не привязан к конкретному агенту: он говорит на ACP, а всё
агент-специфичное (способ подключения, аутентификация, TLS, режимы) живёт в
**профиле агента** (раздел 4.1). Первый и пока единственный целевой агент —
**Work Goose** (`goose serve` на рабочем ноутбуке); дальше в документе он
используется как основной пример.

Gateway не второй AI-агент: он не принимает решений вместо агента, не хранит
рабочих MCP-credentials и не имеет доступа к рабочей файловой системе.

## 2. Роли

- **Агент (Work Goose) — execution plane.** Единственный, кто видит рабочие
  MCP, credentials, файлы и модель и выполняет команды.
- **Gateway — transport/control plane.** Транспорт, маппинг сессий,
  авторизация каналов, политика, подтверждения, аудит.
- **Каналы — frontends.** Тонкие адаптеры поверх ядра Gateway.

Goose Desktop продолжает подключаться к Work Goose напрямую; Gateway его не
заменяет и от него не зависит.

## 3. Топологии развёртывания

Gateway поддерживает три топологии одной и той же программой; различаются
только конфигурация и профиль безопасности.

| Топология | Где Gateway | Где Work Goose | Транспорт | Статус |
|---|---|---|---|---|
| **LAN** (основная на старте) | домашний хост, WSL | рабочий ноутбук, WSL | `wss://<work-host>:<port>/acp`, TLS + secret + pinning | целевая для MVP |
| **Co-located** | та же машина, что Work Goose | WSL | `ws://127.0.0.1:3284/acp` (loopback), secret | поддерживается с MVP |
| **Anywhere** | любой хост | рабочий ноутбук вне LAN | оверлей-сеть или туннель | исследование, см. `P4.1` |

Платформы Gateway: **WSL (Linux) — основной и рекомендуемый путь**, Windows
native — поддерживаемый, macOS — в будущем. Отсюда требования к коду:
чистый Python без OS-специфичных зависимостей в ядре, пути через
`platformdirs`, сервис-обёртки (systemd / Task Scheduler / launchd) — отдельные
файлы в `deploy/`.

### 3.1. Сеть в топологии LAN

`goose serve` в WSL за NAT по умолчанию недоступен из LAN. Решается на
рабочем ноутбуке одним из способов (выбор фиксируется в `P0.1`):

1. **WSL mirrored networking** (`.wslconfig`: `networkingMode=mirrored`) +
   входящее правило Hyper-V firewall / Windows Firewall на порт — основной
   вариант;
2. `netsh interface portproxy` с Windows на IP WSL + правило firewall —
   запасной; IP WSL меняется при перезапуске, нужен скрипт при старте.

Плюс стабильный адрес рабочего ноутбука (DHCP-резервация или имя в LAN).
Возможные ограничения корпоративных политик (GPO на firewall и `.wslconfig`)
проверяются в `P0.1`.

Исходящие соединения из домашнего WSL в LAN работают без настройки.

## 4. ACP: как на самом деле устроен протокол

Подтверждено по документации goose и `agent-client-protocol` SDK
(2026-10-06); детали поведения конкретной версии goose уточняет spike `P0.2`.

Используем официальный Python SDK `agent-client-protocol` (`import acp`),
протокол руками не пишем. Его web-транспорты (Streamable HTTP — нужен HTTP/2,
и WebSocket) помечены experimental; если они не позволяют передать свой
заголовок и SSL-контекст — пишем тонкий WS-транспорт на `websockets` под тот
же интерфейс `Transport`.

### 4.1. Профили агентов

Агент описывается профилем в `config.yaml` (`agents:` — список; в MVP один
профиль). Профиль задаёт алиас (`work`), бэкенд-транспорт, адрес, способ
аутентификации, TLS, `default_cwd` и агент-специфичные настройки.

| Бэкенд | Подключение | Когда | Статус |
|---|---|---|---|
| `remote` (WS / Streamable HTTP) | агент уже слушает сеть, как `goose serve` | Work Goose в LAN и co-located | MVP |
| `stdio` | Gateway сам запускает агента как процесс (`goose acp`, Gemini CLI, адаптеры Claude Code / Codex, Kiro…) | только co-located: агент на той же машине | `P4.7`, CONDITIONAL |

Почти все ACP-агенты, кроме goose, говорят только по stdio, поэтому удалённо
они доступны лишь через обёртку на их стороне — это вне объёма.

Специфика профиля `goose` (бэкенд `remote`):

- `goose serve` отдаёт ACP на `/acp`; по умолчанию `127.0.0.1:3284`,
  `--host/--port` меняют адрес;
- аутентификация — заголовок `X-Secret-Key` (`GOOSE_SERVER__SECRET_KEY`);
  вариант `?token=` для браузеров не используем: секрет попадает в URL и логи;
- `--tls` поднимает self-signed сертификат и печатает
  `GOOSED_CERT_FINGERPRINT=...`; Gateway пинит его по SHA-256 (как Desktop);
- режим подтверждений goose (`auto` / `approve` / `smart_approve`) — см.
  таблицу ниже.

Семантика, которую должен учитывать дизайн:

| Операция | Как в ACP | Следствие для Gateway |
|---|---|---|
| Ответ агента | `prompt()` возвращает только `stopReason`; текст и события приходят уведомлениями `session/update` (`agent_message_chunk`, `tool_call`, `tool_call_update`, `plan`, …) | streaming — базовый механизм с первого дня; «финальный ответ» = собранные чанки |
| Подтверждение | агент шлёт **запрос** `session/request_permission` с вариантами (`allow_once`, `allow_always`, `reject_once`, `reject_always`) и ждёт ответа | Approval Manager держит открытый RPC как `Future`; кнопка в канале завершает его выбранным `optionId` |
| Отмена | **уведомление** `session/cancel`; `prompt()` завершается со `stopReason=cancelled` | все висящие permission-запросы сессии закрываются исходом `cancelled` |
| Восстановление | `session/load` (если агент объявил `loadSession`) переигрывает историю через `session/update` | при восстановлении после рестарта повтор истории не отправляется в каналы |
| Новая сессия | `session/new` требует `cwd` и `mcp_servers` | `cwd` — путь на машине Work Goose (`default_cwd` в конфиге); `mcp_servers=[]` всегда |
| Режим подтверждений | если Work Goose в режиме `auto`, permission-запросов не будет | режим `approve`/`smart_approve` на рабочей стороне или `session/set_mode`; проверяется в `P0.2` |

## 5. Компоненты

```text
                 ┌──────────────────── Gateway daemon (один процесс) ───────────────────┐
 Hermes ──MCP──▶ │ channels/mcp ─┐                                                      │
 Telegram ─────▶ │ channels/tg  ─┼─▶ core: sessions · turns · jobs · approvals · policy │──ACP──▶ агент (Work Goose)
 CLI / Web ─HTTP▶│ api (HTTP+SSE)┘          │                                           │
                 │                      storage (SQLite) · audit · event bus           │
                 └──────────────────────────────────────────────────────────────────────┘
```

- **`agents/` — AgentClient.** Обёртка над `acp` SDK: бэкенды по профилю
  (`remote`, позже `stdio`), auth, TLS pinning, нормализация `session/update` в
  `AgentEvent`, нормализованные ошибки, reconnect с backoff (1, 2, 5, 10, 30 с,
  с потолком), `load_session` с подавлением повтора истории.
- **`core/` — GatewayCore.** In-process сервис, через который работают все
  каналы:
  - *sessions* — `(channel, conversation_key) → (agent, acp_session_id)`; у одного
    ключа может быть несколько сессий и указатель на активную (`/new`,
    `/sessions`);
  - *turns* — одна активная задача на сессию; новое сообщение в занятую
    сессию — отказ «занят, /stop» (очередь — позже, если понадобится);
  - *jobs* — асинхронная модель для долгих задач: `ask → job_id`, затем
    `result(job_id, wait)`; нужна для MCP-канала с его таймаутами;
  - *approvals* — см. раздел 6;
  - *policy* — раздел 7;
  - *event bus* — подписка каналов на события сессии и на approvals.
- **`channels/` — контракт канала** (`base.py`) и адаптеры. Новый канал
  реализует контракт и не трогает ядро.
  **Что видит канал** (решение владельца 2026-10-06): по умолчанию — только
  запрос и итоговый ответ агента, короткий статус «в работе» и, если канал
  подтверждающий, запросы подтверждения. Промежуточные `tool_call`, `plan` и
  рассуждения агента в Telegram и Hermes не пересылаются; детальный поток
  событий доступен через SSE локального API (CLI, Web UI).
- **`api/` — локальный HTTP API + SSE.** Нужен CLI, Web UI и сторонним
  клиентам. Telegram и MCP-канал работают с ядром напрямую, не через HTTP.
- **`cli/` — `acpgw`.** Клиент HTTP API демона (`status`, `sessions`, `ask`,
  `approvals`) плюс прямой режим для отладки без демона.
- **`storage/`** — SQLite (`sqlite3`/`aiosqlite`, без ORM): `sessions`,
  `jobs`, `approvals_audit`, миграции.

## 6. Подтверждения (approvals)

- По умолчанию ничего не подтверждается автоматически; истёкший запрос →
  `reject`/`cancelled`.
- Запрос подтверждения уходит в **канал-подтверждатель** — канал с живым
  человеком, — а не обязательно в канал, откуда пришёл prompt. Задача из
  Hermes подтверждается человеком в CLI/Telegram, а не моделью Hermes.
- MCP-канал **никогда** не экспортирует инструменты approve/deny: иначе
  LLM-агент может подтвердить действие сам себе.
- `allow_always` из удалённых каналов запрещён политикой; доступны
  `allow_once` / `reject_once`.
- Если подтверждающий канал не подключён — немедленный `reject` с понятным
  сообщением в исходный канал.
- Каждое решение пишется в `approvals_audit`: кто, когда, через какой канал,
  инструмент, аргументы (с маскированием секретов), исход.
- Ожидающие approvals при рестарте Gateway теряются — это осознанное
  поведение: со стороны агента они будут отклонены вместе с соединением.

## 7. Безопасность

Инварианты (проверяются тестами):

1. В `initialize` Gateway объявляет **все client capabilities выключенными**
   (`fs.readTextFile`, `fs.writeTextFile`, `terminal`) — агент не может
   попросить хост Gateway читать/писать файлы или выполнять команды.
2. `session/new` всегда с `mcp_servers=[]`.
3. Секреты (секрет агента, токены ботов и API) не попадают в логи —
   фильтр-редактор в логгере + тест.
4. API и MCP-endpoint слушают только `127.0.0.1` и требуют bearer-токен.
5. Каналы работают по allowlist идентичностей (Telegram user_id, JID,
   email); `allow all` не существует.

Что Gateway хранит: адреса и секреты агентов, fingerprint, токены каналов
(в `.env` с правами `600`; OS keyring — позже), session id, audit-метаданные.
Не хранит: корпоративные MCP-credentials, API-ключи, cookies, SSH-ключи.

Политика (`config.yaml`):

```yaml
policy:
  allow_new_sessions: true
  allow_cancel: true
  allow_approvals: true
  allow_always_approval: false
  allow_file_upload: false
  allow_file_download: false
  max_prompt_length: 50000
  max_response_length: 100000
  approval_timeout_seconds: 300
  approver_channels: [cli, telegram]
```

Позже: allowlist инструментов, denylist shell-шаблонов, права по каналам.

Принятые риски и границы данных:

- **Telegram** не даёт end-to-end шифрования для ботов: всё, что агент
  отвечает в Telegram, проходит через серверы Telegram. Владелец принял риск
  без ограничений (2026-10-06).
- **Hermes** обладает постоянной памятью: ответы агента, прошедшие через
  Hermes, могут осесть в его памяти. Граница «home/work» соблюдается на уровне
  сессий, на уровне памяти Hermes — нет; владелец принял риск (2026-10-06).

## 8. Конфигурация

Реализовано в `P0.3` (`src/acp_gateway/config.py`); полный пример —
[`config.example.yaml`](../config.example.yaml) и
[`.env.example`](../.env.example).

- **`config.yaml`** — вся структура: `gateway`, `agents`, `policy`, `logging`.
  Поиск: `--config` / `ACPGW_CONFIG` → `./config.yaml` → каталог конфига
  платформы.
- **Переменные окружения** `ACPGW_<РАЗДЕЛ>__<ПОЛЕ>` переопределяют
  `config.yaml` (`ACPGW_LOGGING__LEVEL=DEBUG`).
- **`.env` — только секреты.** Профили и раздел `gateway` ссылаются на имя
  переменной (`secret_env`, `api_token_env`, `mcp_token_env`); значение берётся
  из окружения процесса, затем из `.env`. Поиск: `--env-file` /
  `ACPGW_ENV_FILE` → `./.env` → каталог конфига. Настройки через `.env` не
  переопределяются — это сделано намеренно, чтобы в `.env` не смешивались
  секреты и параметры.
- При загрузке все упомянутые секреты регистрируются в маскировщике логов.

Проверки при загрузке (инварианты): `gateway.host` — только loopback;
`ws://`/`http://` к агенту — только на loopback, иначе нужен явный
`allow_insecure_transport: true`; `tls_fingerprint` — SHA-256 и только с
`wss://`/`https://`; уникальные алиасы агентов; неизвестные ключи — ошибка.

Фрагмент профиля:

```yaml
agents:
  - alias: work                  # префикс MCP-инструментов и имя в каналах
    title: Work Goose
    kind: goose                  # агент-специфичная логика профиля
    backend: remote
    url: wss://work-laptop.lan:3000/acp   # co-located: ws://127.0.0.1:3284/acp
    secret_env: AGENT_WORK_SECRET
    tls_fingerprint: ""          # пусто → trust-on-first-use
    default_cwd: /home/<user>/work
```

Данные (SQLite, логи) — в каталогах платформы (`platformdirs`, `acpgw paths`).

## 9. Ошибки

Нормализованные: `AgentUnavailable`, `AuthenticationFailed`,
`TLSFingerprintMismatch`, `TransportDisconnected`, `SessionNotFound`,
`SessionBusy`, `PromptFailed`, `ApprovalTimeout`, `PolicyDenied`,
`RateLimited`. Каждый канал переводит их в свой понятный текст
с названием агента из профиля («Work Goose сейчас недоступен»).

## 10. Стек

Python 3.12+, `uv`, `agent-client-protocol` (`acp`), FastAPI + uvicorn,
MCP Python SDK (FastMCP, Streamable HTTP) для MCP-канала, aiogram 3 для
Telegram (HTML parse mode), `sqlite3`/`aiosqlite`, `pydantic`/
`pydantic-settings`, `structlog`, `platformdirs`, `pytest` +
`pytest-asyncio`, `ruff`.

## 11. Структура репозитория

```text
acp-gateway/
├── road-map.md · road-notes.md · README.md
├── pyproject.toml · .env.example · config.example.yaml
├── src/acp_gateway/
│   ├── config.py · log.py · paths.py · daemon.py
│   ├── agents/     client.py · profiles.py · transport.py · tls.py · events.py · errors.py
│   ├── core/       sessions.py · turns.py · jobs.py · approvals.py · policy.py · bus.py
│   ├── storage/    db.py · migrations/
│   ├── channels/   base.py · mcp/ · telegram/ · (xmpp/ · email/ позже)
│   ├── api/        app.py · routes_*.py · schemas.py
│   └── cli/        main.py
├── tests/          unit/ · integration/ · e2e/ · fixtures/acp/
├── scripts/        check_secrets.py · spike_acp.py
├── .githooks/      pre-commit
├── deploy/         systemd/ · windows/ · macos/
└── docs/           architecture.md · setup/ · archive/
```

Тестовый стенд: mock ACP-агент на том же SDK (агентская сторона), который
воспроизводит записанный в `P0.2` реальный трафик Work Goose.
