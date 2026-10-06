# Дорожная карта ACP Gateway

> Статус: единый действующий план ACP Gateway.
> Последняя сверка: 2026-10-06 — план собран из исходного документа
> ([`docs/archive/2026-10-06-initial-plan.md`](docs/archive/2026-10-06-initial-plan.md))
> с поправками по фактической семантике ACP и решениями владельца; проект
> переименован из «Work Goose Bridge» в **ACP Gateway** и обобщён до любого
> ACP-агента (первый агент — Work Goose); фундамент `P0` закрыт: каркас,
> сеть и протокол проверены против реального Work Goose.
> Целевая архитектура — [`docs/architecture.md`](docs/architecture.md).
> Актуальная очередь дальнейшей разработки — последний раздел.
> Журнал решений и находок — [`road-notes.md`](road-notes.md); пункты с большой
> историей получат собственные файлы в `docs/items/`.
> Область: один репозиторий `acp-gateway`.

## 1. Правила ведения

Статусы:

- **READY** — выполнено и подтверждено кодом, тестами или другим проверяемым
  evidence;
- **OPEN** — план готов, работа не начата;
- **IN PROGRESS** — реализация начата;
- **DECISION** — требуется решение владельца;
- **CONDITIONAL** — выполнять только при наступлении триггера;
- **PARTIAL** — часть результата готова (всегда со списком остатка).

Приоритеты:

- **P0** — фундамент: среда, протокол, каркас и инварианты безопасности; без
  них всё остальное строится на догадках;
- **P1** — ядро Gateway: клиент ACP-агента, сессии, задачи, подтверждения,
  политика, локальный API;
- **P2** — каналы (порядок по решению владельца: Hermes → Telegram → прочие);
- **P3** — эксплуатация: автозапуск на платформах, надёжность, наблюдаемость;
- **P4** — расширения: новые каналы, связность вне LAN, файлы, macOS.

Где что живёт: приоритет, статус и краткий план — здесь; проектные решения и
хронология — в `road-notes.md` или `docs/items/`; целевая архитектура — в
`docs/architecture.md`; превзойдённое — в `docs/archive/`.

## 2. Подтверждённая база

- Исходный план и цели — `docs/archive/2026-10-06-initial-plan.md`.
- **READY `P0.3`:** каркас репозитория, конфиг с инвариантами безопасности,
  маскирование секретов в логах, CLI `acpgw`, pre-commit-хук.
- **READY `P0.1`, `P0.2`:** Work Goose (goose 1.53.0) доступен по LAN с
  домашнего ПК; ACP по WebSocket с TLS-пиннингом и `X-Secret-Key`, сессии,
  подтверждения, отмена и восстановление сессий проверены на реальном агенте
  (`scripts/spike_acp.py`, `tests/fixtures/acp/goose-1.53.0/`).
- Hermes подключает MCP-серверы по URL с заголовками — по документации, на
  практике не проверялось.

## 3. P0 — Фундамент

### P0.1. Среда Work Goose и сеть в LAN

**Статус:** READY — 2026-10-06: `goose serve` 1.53.0 работает сервисом в WSL
рабочего ноутбука, порт 3284 открыт правилом Hyper-V firewall; с домашнего ПК
(WSL) по LAN проходят TLS с пиннингом и все сценарии spike; Goose Desktop
подключён к тому же серверу. Runbook — [`docs/setup/work-goose.md`](docs/setup/work-goose.md)
(детали сервиса goose и режим сети WSL — дописать со слов владельца).
**Репозиторий:** — (настройка рабочего ноутбука) + `docs/setup/work-goose.md`

- `goose serve --tls` с `GOOSE_SERVER__SECRET_KEY` в WSL рабочего ноутбука,
  режим подтверждений `approve`/`smart_approve`; fingerprint записан.
- Доступ из LAN: WSL mirrored networking + правило firewall (основной путь)
  или portproxy (запасной); стабильный адрес ноутбука; проверка корпоративных
  ограничений (GPO).
- Критерий: с домашнего хоста (из WSL) проходит TLS-соединение и `initialize`;
  Goose Desktop подключается; Work Goose видит MCP и рабочие файлы; runbook
  записан в `docs/setup/work-goose.md`.

### P0.2. ACP spike против реального Work Goose

**Статус:** READY — 2026-10-06 spike прогнан против реального goose 1.53.0:
WS + TLS-пиннинг + `X-Secret-Key`, `initialize`, режимы, `ping`, permission
(reject), cancel на трёх стадиях, `load` с повтором истории — работают;
Streamable HTTP ломает `load`; сессии Gateway видны в Goose Desktop
(подтвердил владелец). Трафик — `tests/fixtures/acp/goose-1.53.0/`, выводы —
`road-notes.md` и `docs/architecture.md` §4.
**Репозиторий:** `acp-gateway` (`scripts/spike_acp.py`)

Короткий скрипт на `acp` SDK, весь трафик пишется в JSONL. Ответить на
вопросы:

- какой транспорт работает (WS / Streamable HTTP), передаётся ли
  `X-Secret-Key`, можно ли подставить свой SSL-контекст для pinning;
- `initialize` с выключенными client capabilities; `new_session(cwd)`;
- prompt «say ping» и состав `session/update`;
- инструмент, требующий подтверждения: приходит ли `request_permission`, какие
  `options`; что происходит при `auto`;
- `session/cancel` при висящем permission-запросе;
- `load_session` после разрыва: есть ли replay; видны ли сессии Gateway в
  Desktop; работают ли Desktop и Gateway одновременно.

Критерий: ответы и дампы трафика зафиксированы (дампы → `tests/fixtures/acp/`),
выводы — в `road-notes.md`, `docs/architecture.md` поправлен, если нужно.

### P0.3. Каркас репозитория и инварианты безопасности

**Статус:** READY — каркас создан 2026-10-06: `uv`-проект (Python 3.12),
конфиг (`config.yaml` + переменные окружения, секреты только в `.env`/окружении),
логирование с маскированием секретов, CLI `acpgw` (`config check`, `paths`),
pre-commit-хук (проверка секретов + ruff). `uv run pytest` — 80 passed,
`ruff check` и `ruff format --check` — чисто. Первый коммит ещё не сделан.
**Репозиторий:** `acp-gateway`
Решения и находки — `road-notes.md`, запись 2026-10-06 «P0.3».

## 4. P1 — Ядро Gateway

### P1.1. AgentClient и профиль агента `goose`

**Статус:** OPEN — после `P0.2`.
**Репозиторий:** `acp-gateway` (`src/acp_gateway/agents/`)

Обёртка над `acp` SDK со своим WS-транспортом (решено в `P0.2`; основа —
`PinnedWebSocketTransport` и `pin_certificate` из `scripts/spike_acp.py`):
профили агентов в конфиге (`agents:`), бэкенд `remote` (loopback/LAN),
профиль `goose` (`X-Secret-Key`, TLS-пиннинг/TOFU, дописывание `/acp`,
`session_mode` — по умолчанию `smart_approve` (решение владельца) — через
`session/set_mode` после `new`/`load`), выключенные client capabilities,
`mcp_servers=[]`, нормализация `session/update` → `AgentEvent`,
нормализованные ошибки, reconnect с backoff, `load_session` с подавлением
повтора истории. Критерий: 20 последовательных prompts подряд, reconnect после
обрыва, читаемые ошибки; тесты инвариантов безопасности.

### P1.2. Mock ACP-агент и тестовый стенд

**Статус:** OPEN — параллельно с `P1.1`.
**Репозиторий:** `acp-gateway` (`tests/`)

Fake-агент на агентской стороне `acp` SDK, воспроизводящий дампы из `P0.2`:
сессии, чанки, permission-запросы, cancel, load. Критерий: интеграционные
тесты `P1.1` проходят без реального Work Goose.

### P1.3. GatewayCore: сессии, задачи, шина событий, контракт канала

**Статус:** OPEN — после `P1.1`.
**Репозиторий:** `acp-gateway` (`core/`, `storage/`, `channels/base.py`)

SQLite-хранилище с миграциями; маппинг `(channel, conversation_key) →
goose_session_id` с несколькими сессиями на ключ и активной сессией; одна
активная задача на сессию (`SessionBusy`); модель jobs (`ask → job_id`,
`result(wait)`); шина событий; контракт канала. Критерий: маппинг переживает
рестарт; unit-тесты на сессии, jobs и busy.

### P1.4. Approval Manager и политика

**Статус:** OPEN — после `P1.3`; какой канал подтверждает на этапе Hermes —
решается в `P2.2`.
**Репозиторий:** `acp-gateway` (`core/approvals.py`, `core/policy.py`)

Висящие `request_permission` как `Future`; маршрутизация в канал-подтверждатель
(не в исходный канал); таймаут → reject; запрет `allow_always`; reject, если
подтверждающий канал не подключён; закрытие при cancel; `approvals_audit`;
`policy` из конфига. Критерий: конечный автомат approval покрыт тестами, в том
числе таймаут, cancel и отсутствие канала.

### P1.5. Демон, локальный HTTP API и CLI `acpgw`

**Статус:** OPEN — после `P1.3`; нужен до `P2.1` как первый человеческий канал
(в том числе для подтверждений).
**Репозиторий:** `acp-gateway` (`daemon.py`, `api/`, `cli/`)

Один процесс-демон: ядро + API на `127.0.0.1` с bearer-токеном. API: `/health`
(gateway, агенты, каналы, БД), `/sessions`, `/sessions/{id}/messages`,
SSE событий, `/approvals`. CLI: `status`, `sessions`, `new`, `ask` (со
стримингом), `stop`, `approvals` (просмотр, approve/reject). Критерий:
многоходовый диалог и подтверждение действия из CLI против реального Work
Goose.

## 5. P2 — Каналы

### P2.1. Hermes через MCP-фасад (первый канал)

**Статус:** OPEN — после `P1.5`; маппинг разговоров Hermes уточняется в
начале работы.
**Репозиторий:** `acp-gateway` (`channels/mcp/`) + сниппет конфига Hermes

MCP-endpoint (Streamable HTTP) внутри демона, bearer-токен, подключение через
`mcp_servers.<name>.url/headers` в `~/.hermes/config.yaml`. Инструменты с
префиксом алиаса агента, для `work`: `work_status`, `work_sessions`,
`work_new_session`, `work_ask` (возвращает ответ или `job_id`, если не уложился
в `wait`), `work_result`, `work_cancel`. Разговор ключуется явным аргументом
`thread` (по умолчанию `default`). Инструментов approve/deny нет.
В Hermes уходят только запрос и итоговый ответ (без промежуточных событий);
оседание ответов в памяти Hermes владелец принял.
Критерий: многоходовая работа из Hermes в нескольких `thread`, долгая задача
через `job_id`, Hermes не знает секрета агента.

### P2.2. Подтверждения на этапе «только Hermes»

**Статус:** OPEN — решение владельца 2026-10-06: подтверждает человек в CLI
`acpgw approvals`; если подтверждающий канал не подключён — reject.
Реализуется в составе `P1.4` и `P1.5`.
**Репозиторий:** `acp-gateway`

Критерий: действие, запрошенное агентом в задаче из Hermes, подтверждается
или отклоняется в CLI; без подключённого CLI — отклоняется с понятным
сообщением в Hermes.

### P2.3. Telegram

**Статус:** OPEN — после `P2.1`; ограничений на данные через Telegram нет
(решение владельца 2026-10-06), в канал идут только запросы, ответы и
подтверждения.
**Репозиторий:** `acp-gateway` (`channels/telegram/`)

aiogram 3, отдельный бот (не бот Hermes), allowlist user_id; команды `/new`,
`/sessions`, `/status`, `/stop`, `/help`; стриминг редактированием одного
сообщения раз в 1–2 с, разбиение по лимиту 4096, HTML parse mode; inline-кнопки
`allow_once`/`reject_once` с проверкой chat_id, user_id, request_id и
таймаута — Telegram становится основным каналом подтверждений; понятные
ошибки («Work Goose сейчас недоступен»). Критерий: пункты MVP Definition of
Done из исходного плана (раздел 20), выполненные через Telegram.

## 6. P3 — Эксплуатация

### P3.1. Автозапуск в WSL (основной путь)

**Статус:** OPEN — после `P2.1`, чтобы Hermes мог полагаться на Gateway.
**Репозиторий:** `acp-gateway` (`deploy/systemd/`, `docs/setup/`)

systemd user unit для демона; запуск WSL при входе в Windows (Task Scheduler
→ `wsl -d <distro>`), чтобы ВМ WSL поднималась без открытого терминала.
Критерий: после перезагрузки домашнего ПК Gateway отвечает на `/health` без
ручных действий.

### P3.2. Автозапуск в Windows native

**Статус:** OPEN — после `P3.1`.
**Репозиторий:** `acp-gateway` (`deploy/windows/`)

Task Scheduler («при входе», `python -m acp_gateway`), позже — служба
через NSSM. Критерий: то же, что в `P3.1`, для Windows-хоста.

### P3.3. Надёжность и наблюдаемость

**Статус:** OPEN — после `P2.3`.
**Репозиторий:** `acp-gateway`

Rate limiting по каналам, retry отправки в каналы, ротация логов и audit,
`/health/work-goose` и `/health/<channel>`, e2e-проверка (`scripts/smoke`)
против реального Work Goose.

## 7. P4 — Расширения

### P4.1. Связность Gateway ↔ Work Goose вне LAN

**Статус:** DECISION — исследовать, когда понадобится работа не из одной сети.
**Репозиторий:** — / `docs/setup/`

Варианты: оверлей-сеть (Tailscale/WireGuard), обратный туннель, VPN. Нужна
сверка с корпоративными политиками; `goose serve` в интернет не открывать.

### P4.2. Snikket / XMPP

**Статус:** OPEN — после `P2.3`.
**Репозиторий:** `acp-gateway` (`channels/xmpp/`)

Отдельный аккаунт (`work-goose@...`), allowlist JID, маппинг
`xmpp:<JID> → session`. Самохостинг даёт лучшую границу данных, чем Telegram.

### P4.3. Email gateway

**Статус:** OPEN — после `P4.2`.
**Репозиторий:** `acp-gateway` (`channels/email/`)

Allowlist отправителей, тред письма → сессия, только асинхронные ответы,
подтверждения — через другой канал.

### P4.4. Web UI

**Статус:** OPEN — поверх API `P1.5`.
**Репозиторий:** `acp-gateway`

Новая сессия, чат, статус, список сессий, подтверждения.

### P4.5. Файлы, изображения, голос

**Статус:** CONDITIONAL — когда текстового канала станет недостаточно.
**Репозиторий:** `acp-gateway`

Требует пересмотра политики `allow_file_upload/download`.

### P4.6. Gateway на macOS

**Статус:** CONDITIONAL — когда появится macOS-хост.
**Репозиторий:** `acp-gateway` (`deploy/macos/`)

launchd-агент; ядро уже кроссплатформенное (см. `docs/architecture.md`, §3).

### P4.7. stdio-бэкенд и несколько агентов

**Статус:** CONDITIONAL — когда понадобится второй агент или агент без
сетевого ACP на той же машине, что Gateway.
**Репозиторий:** `acp-gateway` (`agents/`)

Бэкенд `stdio` (Gateway запускает агента процессом: `goose acp`, Gemini CLI,
адаптеры Claude Code / Codex, Kiro), несколько профилей в `agents:`, выбор
агента в каналах. Удалённые stdio-агенты — вне объёма.

## 8. Очередь дальнейшей разработки

1. **P1.2 + P1.1 — mock-агент и AgentClient.** Вместе: mock воспроизводит
   записанный трафик goose 1.53.0, клиент выносится из spike в
   `src/acp_gateway/agents/`.
2. **P1.3 — GatewayCore.** Сессии, jobs, шина, контракт канала — основа для
   всех каналов.
3. **P1.4 — approvals и политика** (включая reject без подтверждающего
   канала из `P2.2`).
4. **P1.5 — демон, HTTP API, CLI `acpgw`.** Первый человеческий канал и канал
   подтверждений (`P2.2`).
5. **P2.1 — Hermes через MCP.** Первый внешний канал по решению владельца.
6. **P3.1 — автозапуск в WSL.** Чтобы Hermes мог рассчитывать на Gateway.
7. **P2.3 — Telegram.** Основной канал подтверждений после CLI.
8. **P3.3 — надёжность и наблюдаемость.**
9. **P3.2 — Windows native.**
10. **P4.2 → P4.3 → P4.4 — Snikket, Email, Web UI.**
11. **P4.1 — DECISION: связность вне LAN.** Когда понадобится.
