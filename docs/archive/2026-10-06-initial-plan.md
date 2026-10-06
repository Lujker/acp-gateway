# Work Goose Bridge — план архитектуры и разработки

## 1. Цель проекта

Создать локальный **Work Goose Bridge**, который будет запущен на домашнем Windows-хосте и обеспечит безопасный унифицированный доступ к удалённому **Work Goose (`goose serve`)**, работающему в WSL на рабочем ноутбуке.

Bridge должен стать единым промежуточным слоем между Work Goose и несколькими клиентами/каналами:

- Telegram bot;
- Hermes на домашнем ПК;
- Snikket/XMPP в будущем;
- Web UI в будущем;
- Email gateway в будущем;
- потенциально другие клиенты.

При этом:

- рабочий ноутбук должен запускать только Work Goose;
- рабочие MCP, credentials, файлы и модель остаются на рабочем ноутбуке;
- домашний Hermes остаётся отдельным home-only агентом;
- Goose Desktop на домашнем Windows продолжает подключаться напрямую к Work Goose по ACP;
- Bridge не должен дублировать бизнес-логику Work Goose и не должен хранить рабочие MCP-токены.

---

# 2. Целевая архитектура

```text
                              WORK LAPTOP
                    ┌──────────────────────────┐
                    │ Windows                  │
                    │                          │
                    │ WSL                     │
                    │ ┌──────────────────────┐ │
                    │ │ goose serve          │ │
                    │ │                      │ │
                    │ │ Work Goose           │ │
                    │ │ ├─ corporate MCP     │ │
                    │ │ ├─ work filesystem   │ │
                    │ │ ├─ work credentials  │ │
                    │ │ └─ work model        │ │
                    │ └──────────▲───────────┘ │
                    └────────────┼─────────────┘
                                 │
                           ACP / HTTPS / WSS
                                 │
                                 │
                    HOME WINDOWS HOST
      ┌──────────────────────────┴──────────────────────────┐
      │                                                     │
      │  Goose Desktop ───────── direct ACP ───────────────┤
      │                                                     │
      │  Work Goose Bridge                                 │
      │  ┌───────────────────────────────────────────────┐  │
      │  │ ACP client                                    │  │
      │  │ session manager                               │  │
      │  │ approval manager                              │  │
      │  │ policy layer                                  │  │
      │  │ adapters                                      │  │
      │  └───────────────▲────────▲────────▲─────────────┘  │
      │                  │        │        │                │
      │               Telegram  Hermes   future            │
      │                                Snikket/Web/Email    │
      │                                                     │
      │  WSL                                                │
      │  └─ Hermes (home-only)                              │
      └─────────────────────────────────────────────────────┘
```

---

# 3. Основные архитектурные принципы

## 3.1. Разделение HOME и WORK

Work Goose является единственной системой, которая:

- имеет доступ к рабочим MCP;
- знает корпоративные credentials;
- видит рабочие файлы;
- выполняет команды в рабочем окружении.

Домашний Hermes не должен напрямую получать:

- токены корпоративных MCP;
- filesystem credentials;
- SSH-доступ к рабочему ноутбуку;
- прямой shell на рабочей машине.

Bridge общается только с API/ACP Work Goose.

---

## 3.2. Bridge — transport + policy layer

Bridge не является вторым AI-агентом.

Он отвечает только за:

- transport;
- session mapping;
- авторизацию;
- маршрутизацию;
- tool approval;
- rate limiting;
- logging;
- channel adapters.

Он не должен принимать самостоятельные решения вместо Goose.

---

## 3.3. ACP — единственный backend protocol

Внутренние клиенты не должны напрямую зависеть от реализации Goose.

Все обращения к рабочему агенту идут через один клиент:

```text
WorkGooseClient
```

Методы уровня приложения:

```text
connect()
healthcheck()
new_session()
load_session()
prompt()
cancel()
approve()
deny()
close_session()
```

---

# 4. Компоненты проекта

## 4.1. `WorkGooseClient`

Основная библиотека взаимодействия с Work Goose.

Ответственность:

- подключение к `goose serve`;
- ACP handshake;
- authentication;
- создание сессий;
- восстановление сессий;
- отправка prompt;
- streaming events;
- cancel;
- approval requests;
- reconnect;
- error normalization.

Пример интерфейса:

```python
class WorkGooseClient:
    async def connect(self) -> None: ...
    async def healthcheck(self) -> bool: ...
    async def create_session(self, cwd: str | None = None) -> str: ...
    async def prompt(self, session_id: str, text: str) -> AsyncIterator[AgentEvent]: ...
    async def cancel(self, session_id: str) -> None: ...
    async def approve(self, request_id: str) -> None: ...
    async def deny(self, request_id: str) -> None: ...
```

---

## 4.2. Session Manager

Bridge должен хранить соответствие:

```text
channel + user/chat/context → Goose session_id
```

Пример:

```text
telegram:123456789        → goose-session-abc
hermes:conversation-777   → goose-session-def
snikket:vlad@example.org  → goose-session-ghi
```

### Минимальное хранилище

MVP:

- SQLite.

Таблица:

```sql
sessions (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    goose_session_id TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    metadata_json TEXT
)
```

Unique key:

```text
(channel, external_id)
```

---

## 4.3. Approval Manager

ACP может запрашивать подтверждение действий.

Bridge должен уметь:

```text
Work Goose
    ↓
approval_request
    ↓
Bridge
    ↓
Telegram / Hermes / Web
    ↓
Approve / Deny
    ↓
Bridge
    ↓
Work Goose
```

Структура:

```python
ApprovalRequest:
    request_id
    session_id
    tool_name
    description
    arguments
    created_at
    timeout
```

Важно:

- approval нельзя автоматически подтверждать по умолчанию;
- expired request должен становиться denied/cancelled;
- каждая платформа должна поддерживать собственный UX подтверждения.

---

# 5. Telegram adapter

## 5.1. UX

Команды:

```text
/start
/new
/status
/stop
/help
/session
```

### Поведение

Обычное сообщение:

```text
Telegram message
    ↓
chat_id lookup
    ↓
session exists?
    ├─ no → create_session()
    └─ yes
    ↓
prompt()
    ↓
stream response
    ↓
Telegram
```

---

## 5.2. Streaming

MVP:

- не отправлять каждый токен;
- накапливать текст;
- обновлять одно Telegram-сообщение раз в 1–2 секунды.

Production:

- throttling;
- retry;
- message chunking;
- Markdown escaping;
- Telegram size limits.

---

## 5.3. Approvals

Пример:

```text
Work Goose запрашивает разрешение:

Tool: shell
Command:
git push origin main

[Approve] [Deny]
```

Inline buttons:

```text
approve:<request_id>
deny:<request_id>
```

Проверять:

- chat_id;
- Telegram user_id;
- request_id;
- timeout.

---

## 5.4. Access control

Environment:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=123456789
```

Никакого `allow all`.

---

# 6. Hermes integration

Есть два варианта.

## Вариант A — Hermes plugin

Hermes получает tools:

```text
work_goose_new_session
work_goose_ask
work_goose_continue
work_goose_cancel
work_goose_status
```

Плагин обращается к локальному Bridge HTTP API.

Пример:

```text
Hermes
  ↓
tool: work_goose_ask
  ↓
http://127.0.0.1:8765
  ↓
Bridge
  ↓
ACP
  ↓
Work Goose
```

Плюсы:

- минимальная связность;
- Bridge можно обновлять отдельно;
- Hermes не знает деталей ACP;
- проще контролировать разрешённые операции.

---

## Вариант B — Bridge как MCP server

Bridge экспортирует MCP:

```text
ask_work_goose
continue_work_goose
new_work_session
cancel_work_session
```

Hermes подключается к:

```text
http://127.0.0.1:8766/mcp
```

Плюсы:

- переиспользуется другими агентами;
- стандартный интерфейс;
- меньше Hermes-specific кода.

Минусы:

- чуть больше инфраструктуры;
- нужно отдельно продумать mapping Hermes conversation → Goose session.

### Рекомендация

Начать с HTTP plugin integration.

После стабилизации добавить MCP facade.

---

# 7. Bridge API

MVP REST API:

```text
GET  /health
POST /sessions
GET  /sessions/{id}
POST /sessions/{id}/messages
POST /sessions/{id}/cancel
POST /approvals/{id}/approve
POST /approvals/{id}/deny
```

Пример:

```http
POST /sessions/{id}/messages
Content-Type: application/json

{
  "text": "Проверь рабочий проект X"
}
```

Response:

```json
{
  "message_id": "msg-123",
  "status": "running"
}
```

Streaming:

- SSE для простоты;
- WebSocket позже.

---

# 8. Рекомендуемый стек

## Основной вариант

Python 3.12+ / 3.13+:

- FastAPI;
- uvicorn;
- httpx;
- websockets;
- pydantic;
- SQLAlchemy или sqlite3;
- python-telegram-bot или aiogram;
- structlog/loguru;
- pytest;
- pytest-asyncio.

Почему Python:

- быстрое прототипирование;
- Hermes тоже Python;
- хорошая async-экосистема;
- легко писать Telegram/XMPP adapters.

---

# 9. Структура репозитория

```text
work-goose-bridge/
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── config/
│   └── config.example.yaml
│
├── src/
│   └── work_goose_bridge/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       │
│       ├── acp/
│       │   ├── client.py
│       │   ├── protocol.py
│       │   ├── events.py
│       │   └── errors.py
│       │
│       ├── sessions/
│       │   ├── manager.py
│       │   ├── models.py
│       │   └── repository.py
│       │
│       ├── approvals/
│       │   ├── manager.py
│       │   └── models.py
│       │
│       ├── api/
│       │   ├── app.py
│       │   ├── routes_sessions.py
│       │   ├── routes_approvals.py
│       │   └── schemas.py
│       │
│       ├── adapters/
│       │   ├── telegram/
│       │   │   ├── bot.py
│       │   │   ├── handlers.py
│       │   │   └── formatter.py
│       │   │
│       │   ├── hermes/
│       │   │   └── client.py
│       │   │
│       │   ├── xmpp/
│       │   │   └── adapter.py
│       │   │
│       │   └── email/
│       │       └── adapter.py
│       │
│       └── security/
│           ├── auth.py
│           ├── policy.py
│           └── secrets.py
│
├── hermes-plugin/
│   ├── plugin.yaml
│   └── work_goose_plugin/
│       ├── __init__.py
│       └── tools.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
└── scripts/
    ├── run-dev.ps1
    ├── install-task.ps1
    └── smoke-test.py
```

---

# 10. Конфигурация

`.env.example`:

```env
# Work Goose
WORK_GOOSE_URL=https://work-host:3000
WORK_GOOSE_SECRET=
WORK_GOOSE_TLS_FINGERPRINT=

# Bridge
BRIDGE_HOST=127.0.0.1
BRIDGE_PORT=8765
BRIDGE_API_TOKEN=

# Telegram
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=

# Database
DATABASE_URL=sqlite:///./data/bridge.db

# Logging
LOG_LEVEL=INFO
```

Никогда не коммитить `.env`.

---

# 11. Security model

## 11.1. Что Bridge хранит

Разрешено хранить:

- Work Goose URL;
- Work Goose ACP secret;
- Telegram token;
- session IDs;
- audit metadata.

Не хранить:

- корпоративные MCP credentials;
- корпоративные API keys;
- рабочие browser cookies;
- SSH keys рабочего ноутбука.

---

## 11.2. Network boundary

Рекомендуется:

```text
Work Goose:
TLS + secret
```

Bridge:

```text
127.0.0.1 only
```

Telegram adapter работает внутри того же процесса/хоста.

Если API понадобится другим устройствам:

- TLS;
- API token;
- firewall;
- private network.

---

## 11.3. Policy layer

Добавить:

```yaml
policy:
  allow_new_sessions: true
  allow_cancel: true
  allow_approvals: true
  allow_file_upload: false
  allow_file_download: false
  max_prompt_length: 50000
  max_response_length: 100000
```

В будущем:

- allowlist tool names;
- denylist shell patterns;
- per-channel permissions.

---

# 12. Логирование

Логировать:

- connection state;
- session created;
- prompt accepted;
- response completed;
- approval requested;
- approve/deny;
- cancel;
- reconnect;
- errors.

Не логировать:

- полный secret;
- Telegram token;
- bearer tokens;
- MCP credentials.

Опционально не логировать полный prompt/response.

---

# 13. Обработка ошибок

Нормализованные ошибки:

```text
WorkGooseUnavailable
AuthenticationFailed
SessionNotFound
PromptFailed
ApprovalTimeout
TransportDisconnected
TLSFingerprintMismatch
RateLimited
```

Telegram UX:

```text
Рабочий Goose сейчас недоступен.
Повторить: /retry
```

---

# 14. Reconnect logic

При потере соединения:

```text
1s
2s
5s
10s
30s
```

с upper bound.

После reconnect:

- healthcheck;
- existing sessions не удалять автоматически;
- попытаться load/resume;
- если session invalid — предложить `/new`.

---

# 15. Этапы разработки

## Phase 0 — проверить Work Goose

Готово, когда:

- Goose Desktop подключается к Work Goose;
- Work Goose видит все MCP;
- Work Goose видит рабочие файлы;
- ACP доступен стабильно.

---

## Phase 1 — ACP client MVP

Реализовать:

- connect;
- auth;
- new session;
- prompt;
- collect final response;
- errors.

CLI smoke test:

```bash
python scripts/smoke-test.py
```

Пример:

```text
> create session
session=abc

> prompt "say ping"
pong
```

Definition of Done:

- 20 последовательных prompts;
- reconnect работает;
- errors читаемые.

---

## Phase 2 — Telegram MVP

Реализовать:

- bot startup;
- allowed users;
- `/new`;
- обычные prompts;
- mapping chat → session;
- final response;
- `/status`.

DoD:

- можно вести многоходовый диалог;
- после перезапуска Bridge session mapping сохраняется.

---

## Phase 3 — Streaming

Добавить:

- partial text events;
- update Telegram message;
- throttle;
- chunking.

---

## Phase 4 — Cancel

Telegram:

```text
/stop
```

↓

```text
ACP cancel
```

---

## Phase 5 — Approvals

Добавить:

- approval event;
- Telegram inline buttons;
- approve;
- deny;
- timeout;
- audit.

Это критическая точка перед полноценной удалённой работой.

---

## Phase 6 — Bridge HTTP API

Вынести transport logic из Telegram adapter.

Telegram становится клиентом внутреннего API.

Добавить:

```text
/health
/sessions
/messages
/approvals
```

---

## Phase 7 — Hermes plugin

Hermes tools:

```text
work_goose_new_session
work_goose_ask
work_goose_cancel
work_goose_status
```

Правило:

Hermes никогда не получает Work Goose secret.

Он видит только:

```text
http://127.0.0.1:8765
```

---

## Phase 8 — MCP facade

Опционально.

Bridge предоставляет MCP server:

```text
ask_work_goose
new_work_session
cancel_work_goose
```

---

## Phase 9 — Snikket/XMPP

Добавить adapter:

```text
XMPP message
    ↓
Bridge session manager
    ↓
Work Goose
```

Mapping:

```text
xmpp:JID → goose_session_id
```

---

## Phase 10 — Web UI

Минимальный UI:

- new session;
- chat;
- status;
- approvals;
- sessions list.

---

# 16. Автозапуск на Windows

Bridge лучше запускать на домашнем Windows.

Варианты:

1. Task Scheduler;
2. Windows Service через NSSM;
3. native service wrapper.

Для MVP — Task Scheduler.

Пример:

```text
Trigger:
At log on

Program:
C:\Users\<user>\work-goose-bridge\.venv\Scripts\python.exe

Arguments:
-m work_goose_bridge.main
```

Working directory:

```text
C:\Users\<user>\work-goose-bridge
```

---

# 17. Health checks

`GET /health`

Response:

```json
{
  "bridge": "ok",
  "work_goose": "connected",
  "telegram": "connected",
  "database": "ok"
}
```

Дополнительно:

```text
GET /health/work-goose
GET /health/telegram
```

---

# 18. Tests

## Unit

- config;
- session mapping;
- approval state machine;
- policy;
- formatter.

## Integration

Mock ACP server:

- session/new;
- prompt;
- stream;
- approval;
- cancel.

## E2E

Real Work Goose:

```text
Telegram
  ↓
Bridge
  ↓
Work Goose
  ↓
response
```

---

# 19. Минимальный MVP scope

Не включать в первую версию:

- files;
- voice;
- images;
- multiple Telegram users;
- Snikket;
- Email;
- Web UI;
- MCP facade;
- complex dashboards.

MVP:

```text
Telegram
  ↓
Bridge
  ↓
Work Goose

/new
/status
/stop
text prompts
persistent sessions
```

---

# 20. MVP Definition of Done

Проект считается пригодным к ежедневному использованию, когда:

1. Bridge стартует автоматически.
2. Telegram bot отвечает только разрешённому user ID.
3. `/new` создаёт Work Goose session.
4. Последующие сообщения используют ту же session.
5. Ответы приходят обратно в Telegram.
6. `/stop` отменяет текущую задачу.
7. После рестарта mapping восстанавливается.
8. Work Goose secret не логируется.
9. При недоступном ноутбуке бот возвращает понятную ошибку.
10. Goose Desktop продолжает работать независимо.

---

# 21. Production Definition of Done

Дополнительно:

1. streaming;
2. approvals;
3. TLS fingerprint validation;
4. reconnect;
5. audit log;
6. rate limiting;
7. retry;
8. health endpoint;
9. Hermes integration;
10. Snikket adapter.

---

# 22. Hermes UX

Примеры:

```text
"Спроси рабочего Goose, доступны ли MCP."
```

Hermes:

```text
tool: work_goose_status
```

---

```text
"Попроси рабочего агента проверить проект X."
```

Hermes:

```text
tool: work_goose_ask
```

---

```text
"Продолжи предыдущую рабочую задачу и уточни второй пункт."
```

Hermes plugin использует сохранённый Work Goose session.

---

# 23. Telegram UX

```text
/new
```

```text
Создана новая Work Goose session: abc123
```

---

```text
Проверь доступные MCP.
```

```text
Work Goose:
Доступны:
- ...
```

---

```text
/stop
```

```text
Текущая задача остановлена.
```

---

# 24. Будущий Snikket UX

```text
Vladislav@xmpp
    ↓
Hermes/Work Goose contact
```

Рекомендуется отдельный XMPP account:

```text
work-goose@chat.example.com
```

Adapter должен использовать тот же Bridge API.

---

# 25. Что не делать

Не рекомендуется:

- подключать Hermes напрямую к рабочему shell;
- копировать Work MCP credentials домой;
- открывать `goose serve` напрямую в интернет;
- хранить secrets в git;
- автоматически approve tool calls;
- смешивать Home Hermes sessions и Work Goose sessions;
- делать Telegram gateway непосредственно на рабочем ноутбуке, если Telegram там нежелателен.

---

# 26. Рекомендуемый порядок разработки

```text
[1] ACP client
 ↓
[2] CLI smoke test
 ↓
[3] SQLite session manager
 ↓
[4] Telegram bot
 ↓
[5] /new + /status
 ↓
[6] /stop
 ↓
[7] persistence
 ↓
[8] streaming
 ↓
[9] approvals
 ↓
[10] Bridge API
 ↓
[11] Hermes plugin
 ↓
[12] Snikket
 ↓
[13] Web UI
```

---

# 27. Первый coding milestone

Создать:

```text
src/work_goose_bridge/acp/client.py
```

и CLI:

```text
python -m work_goose_bridge.cli
```

Команды:

```text
connect
new
ask <text>
status
quit
```

Не начинать с Telegram.

Сначала доказать:

```text
Home Python client
    ↓
ACP
    ↓
Work Goose
```

После этого Telegram — только transport adapter.

---

# 28. Второй coding milestone

Telegram MVP:

```text
Telegram
    ↓
bot.py
    ↓
SessionManager
    ↓
WorkGooseClient
```

Без streaming и approvals.

---

# 29. Третий coding milestone

Production agent control:

```text
streaming
cancel
approvals
reconnect
```

После этого можно считать Bridge полноценным remote frontend для Work Goose.

---

# 30. Итоговая целевая модель

```text
                         WORK
                          │
                    Work Goose
                          │
                         ACP
                          │
                         HOME
                          │
                  Work Goose Bridge
                /         |         \
               /          |          \
          Telegram      Hermes      Snikket
                                       \
                                        Web
```

Goose Desktop при этом продолжает работать отдельно:

```text
Goose Desktop ───── direct ACP ───── Work Goose
```

Bridge не заменяет Desktop, а добавляет дополнительные способы удалённого взаимодействия.

---

# 31. Ключевое архитектурное решение

**Work Goose — execution plane.**

**Bridge — transport/control plane.**

**Telegram/Hermes/Snikket/Web — frontends.**

Не смешивать эти роли.

Это позволит добавлять новые каналы без изменений рабочего агента и без переноса корпоративных секретов на домашний ПК.
