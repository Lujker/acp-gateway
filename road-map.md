# Дорожная карта ACP Gateway

> Статус: единый действующий план ACP Gateway.
> Последняя сверка: 2026-10-13 — VPS/Telegram + два Windows/Hermes connectors,
> поставка uv/Git, CLI/update (`P4.8`); Goose и платформенное расширение — `P4.9`.
> VPS-диспетчер с собственным WebSocket-коннектором
> (`P4.1`) поставлен перед расширениями каналов; готовы MCP-фасад Hermes и
> подтверждения его задач через CLI (`P2.1`, `P2.2`); отдельные цели P3 —
> дистрибуция/настройка (`P3.4`) и CLI управления службой (`P3.5`). План собран
> 2026-10-06 из исходного документа
> ([`docs/archive/2026-10-06-initial-plan.md`](docs/archive/2026-10-06-initial-plan.md))
> с поправками по фактической семантике ACP и решениями владельца; проект
> переименован из «Work Goose Bridge» в **ACP Gateway** и обобщён до любого
> ACP-агента (первый агент — Work Goose); фундамент `P0` закрыт, готовы
> клиент агента, mock-агент, ядро, подтверждения, политика, демон и CLI (`P1.1`–`P1.5`).
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

Язык: публичные документы (README, `docs/`, код, комментарии, сообщения CLI)
— на английском; `road-map.md`, `road-notes.md` и архив исходного плана — на
русском (решение владельца 2026-10-06).

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
- **READY `P1.1`–`P1.5`:** `AgentClient`, mock-агент на записанном трафике,
  ядро: сессии разговоров в SQLite, jobs, шина событий, контракт канала,
  подтверждения через человеческие каналы, аудит и ограничения политики,
  демон, защищённый HTTP/SSE API и настоящий CLI; диалог и отказ действия
  через CLI проверены на реальном Work Goose.
- **READY `P2.1`, `P2.2`:** MCP-фасад и CLI-подтверждения задач Hermes;
  подключение по URL с заголовками, discovery и вызовы registry проверены
  на установленном MCP-модуле Hermes против записанного mock Goose.
- **Проверка интерфейса, 2026-10-12:** найден работающий нативный Hermes Desktop
  на доступном Windows-хосте; backend сообщает **0.21.6**. Отдельный клиент
  прошёл HTTP identity-проверку и WebSocket JSON-RPC `session.active_list`.
  Живых сессий на момент проверки — 0; prompt, общий диалог, approvals и
  второй физический ПК не проверялись. Это основание для `P4.8`, не готовый адаптер.

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
`ruff check` и `ruff format --check` — чисто.
**Репозиторий:** `acp-gateway`
Решения и находки — `road-notes.md`, запись 2026-10-06 «P0.3».

## 4. P1 — Ядро Gateway

### P1.1. AgentClient и профиль агента `goose`

**Статус:** READY — 2026-10-06: `src/acp_gateway/agents/` (клиент, WS-транспорт,
TLS-пиннинг, события, ошибки), `session_mode`/`acp_endpoint` в профиле.
24 интеграционных теста против mock-агента (20 prompts подряд, переподключение
после рестарта агента, обрыв во время хода, отмена на всех стадиях,
подтверждения, TLS) + проверка на реальном goose 1.53.0. Полный прогон —
206 passed, `ruff` чисто.
**Репозиторий:** `acp-gateway`
Решения и находки — `road-notes.md`, запись 2026-10-06 «P1.1, P1.2».

### P1.2. Mock ACP-агент и тестовый стенд

**Статус:** READY — 2026-10-06: `tests/fakes/fake_goose.py` отдаёт полезные
нагрузки из записей реального goose 1.53.0 (`tests/fakes/recordings.py`);
контрактные тесты `tests/unit/test_recorded_contract.py` проверяют каждое
записанное сообщение агента.
**Репозиторий:** `acp-gateway`

### P1.3. GatewayCore: сессии, задачи, шина событий, контракт канала

**Статус:** READY — 2026-10-07: `storage/` (SQLite, миграции, разговор
`(channel, key, agent)` → несколько сессий с активной), `core/` (`GatewayCore`:
jobs `submit`/`wait`/`ask`/`cancel`, `SessionBusy`, `EventBus`),
`channels/base.py`. Маппинг и ответы jobs переживают рестарт (проверено
тестом). 247 passed (+41), `ruff` чисто; на реальном Work Goose ядро
целиком проверяется в `P1.5`.
**Репозиторий:** `acp-gateway` (`core/`, `storage/`, `channels/base.py`)
Решения и находки — `road-notes.md`, запись 2026-10-07 «P1.3».

### P1.4. Approval Manager и политика

**Статус:** READY — 2026-10-07: `ApprovalManager`, запросы подтверждений через
Future, адресные события человеческим каналам, таймауты, отмена, проверки
решений и каналов, миграция `approvals_audit` с маскированием секретов.
`GatewayCore` подключает обработчики через публичный `set_permission_handler`;
лимиты запроса/ответа и флаги политики действуют в ядре. 296 passed (+40),
`ruff` чисто. Настоящий человеческий CLI-канал и сборка из конфига — `P1.5`.
**Репозиторий:** `acp-gateway` (`core/approvals.py`, `core/policy.py`)
Решения и находки — `road-notes.md`, запись 2026-10-07 «P1.4».

Висящие `request_permission` как `Future`; маршрутизация в канал-подтверждатель
(не в исходный канал); таймаут → reject; запрет `allow_always`; reject, если
подтверждающий канал не подключён; закрытие при cancel; `approvals_audit`;
`policy` из конфига. Критерий: конечный автомат approval покрыт тестами, в том
числе таймаут, cancel и отсутствие канала.

### P1.5. Демон, локальный HTTP API и CLI `acpgw`

**Статус:** READY — 2026-10-07: сборка ядра из конфига, daemon lock,
loopback API с отдельным токеном владельца, HTTP/SSE, CLI с потоковым выводом,
восстановлением jobs, выбором сессии, отменой и подтверждениями.
Настоящий `approvals watch` держит человеческий канал; список и сам демон
человеком не считаются. Многоходовый диалог и `reject_once` из интерактивного
CLI проверены на Work Goose, отказ подтверждён аудитом SQLite.
Runbook — [`docs/setup/gateway.md`](docs/setup/gateway.md).
**Репозиторий:** `acp-gateway` (`daemon.py`, `api/`, `cli/`)

Один процесс-демон: ядро + API на `127.0.0.1` с bearer-токеном. API: `/health`
(gateway, агенты, каналы, БД), `/sessions`, `/sessions/{id}/messages`,
SSE событий, `/approvals`. CLI: `status`, `sessions`, `new`, `ask` (со
стримингом), `stop`, `approvals` (просмотр, approve/reject). Критерий:
многоходовый диалог и подтверждение действия из CLI против реального Work
Goose.

## 5. P2 — Каналы

### P2.1. Hermes через MCP-фасад (первый канал)

**Статус:** READY — 2026-10-08: MCP-фасад на Streamable HTTP, отдельный
bearer-токен, шесть инструментов на алиас, `thread`, адресация сессий,
асинхронные jobs и отмена. Проверен настоящий установленный MCP-модуль
Hermes (discovery + registry) против mock Goose: память между ходами,
раздельные треды, polling результата и CLI approve/reject. Модель Hermes
и настоящий Work Goose в этом прогоне не вызывались; ручной сценарий
для них описан в [`docs/setup/hermes.md`](docs/setup/hermes.md).
**Репозиторий:** `acp-gateway` (`channels/mcp.py`) + сниппет конфига Hermes

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

**Статус:** READY — 2026-10-08: запрос через настоящий MCP-модуль Hermes
доходит до CLI; approve/reject через `acpgw approvals` и отказ без человека
проверены против mock Goose. Аудит и ограничения остаются в ядре;
MCP не имеет инструментов принятия решений.
**Репозиторий:** `acp-gateway`

Критерий: действие, запрошенное агентом в задаче из Hermes, подтверждается
или отклоняется в CLI; без подключённого CLI — отклоняется с понятным
сообщением в Hermes.

### P2.3. Telegram

**Статус:** PARTIAL — private allowlist, команды, sessions/results,
inline allow_once/reject_once, audit и durable update cursor реализованы.
2026-10-13 в development 0.2.0rc2.dev0 добавлены persistent result outbox,
partial/final status edits и recovery/rollback fence. Проверены real aiogram
dispatcher/types с fake Bot API и ACP mock; ранее getMe и pinned TLS/ACP probe.
Остаток: живая приёмка сообщений, кнопок и статусов владельцем.
[Журнал](docs/items/p3.3-reliability.ru.md). Ограничений на данные через Telegram
нет (решение владельца 2026-10-06); в канал идут запросы, ответы и подтверждения.
**Репозиторий:** `acp-gateway` (`channels/telegram.py`, `docs/setup/telegram.md`)

2026-10-09: `/agent` перечисляет маршруты; отправка итогов и `/result` вынесена
в отдельную bounded queue, чтобы длинные ответы/429 retry не задерживали
approval events и кнопки. Два relay-маршрута проверены через настоящий aiogram
dispatcher с fake Bot API: выбор компьютера, отдельные сессии, allow/reject.

aiogram 3, отдельный бот (не бот Hermes), allowlist user_id; команды `/new`,
`/sessions`, `/status`, `/stop`, `/help`; стриминг редактированием одного
сообщения раз в 1–2 с, разбиение по лимиту 4096, HTML parse mode; inline-кнопки
`allow_once`/`reject_once` с проверкой chat_id, user_id, request_id и
таймаута — Telegram становится основным каналом подтверждений; понятные
ошибки («Work Goose сейчас недоступен»). Критерий: пункты MVP Definition of
Done из исходного плана (раздел 20), выполненные через Telegram.

## 6. P3 — Эксплуатация

### P3.1. Автозапуск в WSL (основной путь)

**Статус:** PARTIAL — 2026-10-08: генерация systemd user unit и управление
через `acpgw service`; unit проверен `systemd-analyze --user verify`.
Есть Windows logon task, удерживающая WSL через `sleep infinity` и не
обходящая отключение службы. Реальный Task Scheduler lifecycle проверен на
текущем Windows/WSL-хосте через временную задачу; исправлен отказ
`WSL_E_DISTRO_NOT_FOUND` из-за кавычек в аргументах. Остаток: проверить
настоящий Windows reboot. User D-Bus восстановлен установкой dbus-user-session
и разрешённым владельцем restart user manager. Полный lifecycle настоящей
systemd службы прошёл для source CLI и standalone binary, включая SIGKILL
recovery и сохранение конфигурации/БД при uninstall.
**Репозиторий:** `acp-gateway` (`service.py`, `deploy/windows/`, `docs/setup/`)

systemd user unit для демона; запуск WSL при входе в Windows (Task Scheduler
→ `wsl -d <distro>`), чтобы ВМ WSL поднималась без открытого терминала.
Критерий: после перезагрузки домашнего ПК Gateway отвечает на `/health` без
ручных действий.

### P3.2. Автозапуск в Windows native

**Статус:** PARTIAL — native Task Scheduler/ACL/locks подтверждены в `P4.8.4`;
остаются reboot/logon и приёмка устанавливаемой поставки.
**Репозиторий:** `acp-gateway` (`deploy/windows/`)

Task Scheduler («при входе», `python -m acp_gateway`), позже — служба
через NSSM. Критерий: то же, что в `P3.1`, для Windows-хоста.

### P3.3. Надёжность и наблюдаемость

**Статус:** PARTIAL — health/probes, JSON log rotation, Telegram throttle,
core/ACP reconnect и session persistence проверены. В 0.2.0rc2.dev0 готовы
bounded SQLite retention (audit opt-in), admission limits всех каналов и
persistent Telegram result outbox с chunk ACK/restart/rollback fence.
Остаток: живая e2e-приёмка Work Goose/Telegram; неопределённые отправки и
agent prompts автоматически не переигрываются.
[Журнал и проверки](docs/items/p3.3-reliability.ru.md),
[retention](docs/setup/retention.md), [delivery/status](docs/setup/telegram-recovery.md).
**Репозиторий:** acp-gateway

### P3.4. Дистрибуция, установка и первичная настройка

**Статус:** PARTIAL — 2026-10-08: установка из checkout одной командой
`sh scripts/install.sh` через изолированный uv tool; `acpgw setup` создаёт
приватные config/.env и отдельные токены, не заменяя существующие файлы.
Добавлена самостоятельная Linux-сборка PyInstaller, версия, SQL-миграции,
архив, checksum и build metadata. Локальный x86_64/glibc 2.35 бинарник
проверен вне checkout без Python в PATH: CLI, daemon/API/SSE/MCP, pinned TLS
ACP, approvals/cancel, restart persistence и frozen service commands.
Linux x86_64 подтверждён полным smoke в чистом Ubuntu 22.04 без Python:
[CI run 37748465084](https://github.com/Lujker/acp-gateway/actions/runs/37748465084),
архив с checksum доступен в артефактах CI. Остаток: другие целевые платформы
и версионированный выпуск. Решение владельца: сборка локально вручную,
GitHub Release и загрузка файлов вручную; Actions только по необязательному
`workflow_dispatch`, без push/PR/tag/release триггеров.
Рецепт — `docs/setup/binary.md`.
Штатная установка без checkout, updater и выпуск пакета/образа выделены в P3.6.
**Репозиторий:** `acp-gateway` (`scripts/`, сборка и release artifacts)
**Решение владельца:** 2026-10-08 — удобство установки как у Goose/Hermes.

- Основная цель: собрать и установить самостоятельный `acpgw` со всеми
  CLI-командами и runtime-зависимостями, без отдельной установки Python.
  Сначала Linux/WSL, затем Windows вместе с `P3.2`; macOS по триггеру `P4.6`.
- Поддерживаемая альтернатива: скачать репозиторий, выполнить одну команду
  установки и `setup`; одинаковые команды настройки и эксплуатации у
  бинарника и установки из исходников.
- Сборка включает SQL-миграции и прочие runtime-ресурсы; конфиг, токены,
  pins и БД живут вне дистрибутива. Обновление/переустановка сохраняют их.
- Документировать зависимости установщика, поддерживаемые ОС/архитектуры,
  обновление и удаление; выпускать версии и контрольные суммы артефактов.

Критерий: на чистой целевой машине установка без разработки проекта,
`--help`, `setup`, daemon/API/MCP и команды службы работают; бинарный путь
не требует Python, путь из checkout требует только заявленных prerequisites.
Повторная установка и обновление не теряют конфиг и данные.

### P3.5. Понятный CLI и управление службой

**Статус:** PARTIAL — 2026-10-08: help со стартовым маршрутом; `setup`;
`service install/enable/disable/start/stop/restart/status/uninstall` для
Linux/WSL. Windows keepalive task проверена настоящим Task Scheduler.
Полный user-service lifecycle и crash recovery подтверждены. Остаток: reboot;
native Windows (`P3.2`); macOS по `P4.6`.
**Репозиторий:** `acp-gateway` (`cli/`, `service.py`, platform adapters, docs)
**Решение владельца:** 2026-10-08 — автозапуск и его контроль должны быть
понятными командами программы, а не только ручными действиями в runbook.

- `--help` объясняет первичную настройку и каждую команду; отдельный help
  службы объясняет отличие enable/disable от start/stop.
- Установка службы, включение/выключение автозапуска, старт/остановка,
  перезапуск, статус и удаление доступны из CLI без знания systemctl или
  Task Scheduler. Удаление службы сохраняет пользовательские данные.
- Статус показывает установлена ли служба, включён ли автозапуск и запущена
  ли она; работает и при остановленном демоне, отдельно от API health.
- Общий пользовательский контракт на поддерживаемых платформах; backend
  платформы скрыт за CLI, ограничения и диагностика объясняются понятно.

Критерий: полный цикл install → enable → status → disable → enable →
uninstall подтверждён на целевых ОС, включая перезагрузку и понятный отказ
при недоступном менеджере служб; конфиг/секреты/БД сохраняются.

### P3.6. Штатная установка, обновление и удаление

**Статус:** PARTIAL — 2026-10-13: расширены isolated wheel update/rollback и
crashed-installer/WAL recovery проверки; [журнал](docs/items/p3.3-reliability.ru.md).
Ранее, 2026-10-09: первый Linux/WSL этап реализован:
wheel/sdist и release bundle, uv CLI version/update/uninstall/rollback, runtime lease,
backup окружения/entry point/SQLite с WAL, health check и восстановление при ошибке,
собственный Compose без build и опциональный вложенный образ.
Общий набор: 722 passed,
3 optional Hermes skipped; wheel/bundle lifecycle,
Git checkout/direct Git ref, автономный Linux-бинарник и Docker IP/WSS с двумя
mock Goose прошли. Комплект с вложенным образом проверен через docker load
и запуск Compose без checkout. Выпущен публичный prerelease
[v0.1.0](https://github.com/Lujker/acp-gateway/releases/tag/v0.1.0): bundle,
wheel/sdist и пять native-сборок (19 assets, серверные SHA-256 сверены).
GHCR `ghcr.io/lujker/acp-gateway:0.1.0` (Linux x86_64) доступен без авторизации;
проверены anonymous pull по digest и запуск скачанного образа.
[PyPI v0.1.0](https://pypi.org/project/acp-gateway/0.1.0/) опубликован через
Trusted Publishing из тех же wheel/sdist, без пересборки; SHA-256 скачанных файлов
совпадают с GitHub Release. Полная native-поддержка служб и автоматическое
обновление ещё не выполнены.
Изолированная Linux/WSL установка из PyPI прошла setup, enrollment, daemon/API,
runtime guard, `update --check`, обновление до указанной версии из реестра,
rollback и uninstall. Проверены сохранность настроек/ключей и записей компьютеров.
Нативные Linux ARM64, macOS ARM64/Intel и Windows x86_64 прошли wheel lifecycle,
installer, update/recovery/rollback/uninstall и frozen CLI (manual run
[37945106618](https://github.com/Lujker/acp-gateway/actions/runs/37945106618),
все jobs passed; macOS Intel повторён после сетевого таймаута PyPI).
Windows внутренние команды явно ставят операцию в очередь с durable result и
`update --status`; PowerShell-установщик ждёт проверенного результата helper.
Приоритет перед серверной приёмкой:
пройти её через штатную установку, затем отдельно подтвердить Git-путь.
**Решение владельца:** установка без checkout, понятные внутренние команды
обновления/удаления; Python-пакет через uv и готовый Docker-образ для VPS.
Ручной выпуск сохраняется: без автоматических push/PR/tag/release builds.

1. **Python-пакет и установка без checkout.** Собрать wheel/sdist с SQL и
   runtime-ресурсами; поддержать установку опубликованного пакета, файла wheel
   и Git ref через uv. Настройки/токены/БД остаются вне окружения программы.
2. **CLI жизненного цикла.** `version`, `update --check`, `update`, выбор версии
   или источника, `uninstall`. Определять uv-установку; чужие окружения,
   checkout, бинарник и Docker не изменять ошибочным способом. Первый этап —
   обновление вручную после остановки процессов, сохранение данных.
   **Реализовано:** отдельный helper вне заменяемого окружения, приватный snapshot,
   проверка version/config/SQL, автоматическое восстановление при ошибке установки
   или health check, явный `update --rollback` с согласованной БД. Установщики
   используют тот же runtime guard при повторной установке. Откат БД заменяет
   изменения после сохранённого snapshot; это описано в инструкции.
   Windows updater выполняется после выхода исходного CLI, чтобы ОС освободила
   python.exe; постановка в очередь и подтверждённый результат различаются явно.
3. **Дистрибутив для VPS.** Готовый образ и самостоятельный deployment bundle
   с Compose, nginx, подготовкой приватных настроек и инструкцией. Запуск без
   клонирования; Git/source build — отдельный поддерживаемый сценарий.
4. **Ручной выпуск.** Локальная сборка и smoke; wheel/sdist, deployment bundle,
   checksums; инструкции публикации PyPI, GitHub Release и Docker registry.
   Фактическая доступность публичных артефактов проверяется отдельно.
   **Реализовано:** сборка отказывает dirty checkout (preview — отдельный флаг),
   checksums относятся к текущему выпуску, опциональная ручная публикация GHCR
   только после успешных проверок платформ и Docker. PyPI — ручной workflow с
   Trusted Publishing, environment `pypi`, проверкой SHA256SUMS готовых release
   assets и dry-run по умолчанию. Пароль/постоянный API-токен не требуются.
   **Выпущено:** публичные GitHub Release, GHCR и PyPI; для Git-приёмки использовать
   опубликованный `v0.1.0`.
5. **Приёмка обоих способов.** Изолированная установка → setup → daemon/API →
   обновление → повторный запуск → удаление; сохранность ключей, сессий и SQLite,
   ошибки загрузки/активного процесса. Отдельный прогон wheel и Git checkout.
   Затем настоящий VPS, два компьютера, Telegram и WSS по runbook.
6. **Бинарники и платформы.** Установочные скрипты Linux/macOS/Windows,
   сборки x86_64/ARM64, проверки native Windows/macOS и их менеджеры служб;
   связать с P3.2/P4.6. Установка пакета не равна проверенной поддержке ОС.
   **Реализовано:** общий native build, shell/PowerShell-установщики; ручная
   CI-матрица Linux x86_64/ARM64, macOS Intel/ARM64, Windows x86_64. Native smoke
   проверяет wheel daemon, runtime guard, installer, update/recovery/rollback/
   uninstall; frozen smoke — version/setup/config. Windows ARM64, полный frozen
   ACP smoke на Windows/macOS, подпись ОС и native службы пока не подтверждены.
7. **Автоматизация обновления.** Подписанные артефакты, подготовка новой версии,
   окно без заданий, backup/health check, совместимость схемы и протокола,
   восстановление при сбое. Фоновая проверка и автообновление — opt-in.
   Backup/health/recovery ручного uv updater готовы. Подписанные каналы,
   фоновая проверка, автоматическое окно без заданий, protocol-совместимость
   и самообновление бинарников ещё не реализованы.
8. **Интерактивный первый запуск — PARTIAL, 2026-10-11.** В текущем исходном
   коде реализованы мастер `setup`, `config edit`, запуск сохранённой роли
   через `start` и консоль `menu` (также `acpgw` без аргументов в терминале).
   Меню управляет службами, компьютерами, диалогами, approvals и проверкой
   обновлений. Скрытый ввод, сохранение ключей, приватные backup/откат файлов,
   enrollment, MCP Hermes, ACP initialize/Telegram getMe и проверка портов
   покрыты локальными проверками. Ручная публикация новой версии, native службы
   и живая VPS/два компьютера/Telegram-приёмка остаются впереди.
   Инструкция: [interactive.md](docs/setup/interactive.md).
   **Расширено 2026-10-11:** `doctor` (offline/live/JSON), `config edit --section`,
   `agents install/provider/serve/service`, `connect`; мастер готовит локальный
   Goose-профиль с отдельным ключом и сразу подключает компьютер к внешнему
   диспетчеру. Updater учитывает службы Goose. Docker CLI laboratory проверяет
   реальный терминальный мастер, два исходящих компьютера, ACP mocks,
   диагностику ошибок, маршрутизацию и reconnect без model calls.
   Suite: **759 passed, 3 optional Hermes skipped**; Docker lab passed.
   [Настройка хостов](docs/setup/host-cli.md), [лаборатория](docs/setup/cli-lab.md).
   Реальный upstream Goose/provider и native службы требуют отдельной приёмки.
   После `uv tool install` мастер
   выбирает роль: локальный Gateway, Gateway на VPS или исходящий коннектор.
   Собирает настройки по шагам, скрывает ввод секретов, проверяет Goose/ACP,
   подключение к серверу, Telegram и MCP для Hermes. Сохраняет конфигурацию,
   показывает итоговый план и позволяет включить подходящий автозапуск.
   Для двух компьютеров — понятная регистрация/перенос enrollment без ручного
   редактирования YAML. Повторный запуск редактирует настройки без потери ключей.
   Проверяет существующие службы и занятые порты, не создаёт дубликаты.
   В опубликованном v0.1.0 этого мастера нет: `setup` создаёт шаблон и секреты, затем требуется
   настройка по инструкции. Установка/авторизация Goose или Hermes, выдача
   Telegram bot token и DNS/TLS требуют отдельных действий пользователя;
   мастер должен объяснять их и проверять результат.

Критерий первого этапа выполнен: для установки и эксплуатации не нужен checkout;
внутренние команды uv-пути проверены вне репозитория с публичным пакетом из PyPI;
deployment bundle работает с готовым образом. Живая VPS/два компьютера/Telegram
приёмка остаётся отдельным этапом и не подменяется локальными тестами.

## 7. P4 — Расширения

### P4.1. VPS-диспетчер и собственный WebSocket-коннектор

**Статус:** IN PROGRESS — 2026-10-08: по поручению владельца начать следующий
этап по рекомендованным приоритетам. Linux/WSL-коннектор перенесён перед
native Windows; первоначальная граница — один владелец и сетевой ACP.
Первый шаг реализован: протокол control frames v1 и локальные команды
`computers enroll/list/rotate/revoke`; отдельные ключи компьютеров, SQLite schema 4.
Следующий шаг реализован: отдельные foreground `dispatcher`/`connector`,
регистрация, heartbeat и отключение при ротации/отзыве.
После решения владельца поддержаны plain WS по IP/порту, root/subpath для nginx,
опциональный WSS (CA/hostname либо pin) и newest-wins с новым epoch.
Реализован первый ACP relay: ingress в `serve`, маршруты `computer/agent` в core,
PolicyTransport на компьютере, ответы/permissions/cancel, один reader/writer,
bounded queues, native keepalive, epoch fencing и reconnect без replay prompts.
Проверен на recorded Goose mock, включая owner API и MCP. Живая приёмка на
реальном агенте, несколько физических компьютеров и восстановление результатов ещё впереди.
Добавлены отдельная Linux/WSL user-service коннектора, status/logs и политика
остановки при fatal config/access ошибках. `computers status` через owner API
различает online computer и готовность маршрута; schema 5 хранит последнее
подключение без секретов/трафика. Автотесты — на нас, живая приёмка — на владельце;
работающие службы и бинарник на этом этапе не обновляются.
Два mock-компьютера с одинаковым alias проверены в общем runtime: owner API/MCP,
раздельная история/сессии/approvals, disconnect/revoke/rotate/replacement,
подмена alias/epoch и перегрузка не затрагивают второй узел. Диагностика маршрута
показывает причину неготовности, потоки/лимит и последнюю stream error без
сырого текста. Несколько физических компьютеров остаются живой приёмкой.
Решения ревью приняты владельцем: ingress в serve, ACP в конверте alias/stream/epoch,
newest-wins, один demux-reader/keepalive и bounded queues, адрес computer/agent.
TLS временно выключен для тестов; домен/root/subpath и nginx предусмотрены.
**Репозиторий:** `acp-gateway` (режимы запуска, транспорт, каналы, эксплуатация)
**Решение владельца:** 2026-10-08; схема — `docs/architecture.md`, §3.2;
открытые вопросы — `road-notes.md`, запись 2026-10-08 «P4.1».
Актуальные этапы — [`docs/items/connector.md`](docs/items/connector.md).

**Уточнение владельца 2026-10-12:** ближайшая живая цель — VPS и два Windows-ПК
с уже работающим нативным Hermes Desktop. Существующий релей остаётся основой;
подключение к backend Desktop, обнаружение экземпляров и их сессий выделены
в `P4.8`. Goose в WSL/на хосте остаётся вторым проверочным адаптером. Старое
ограничение ближайшего этапа только Linux/WSL и сетевым ACP больше не определяет
очередь; stdio и durable recovery не блокируют первый Desktop attach-маршрут.

2026-10-09: Dockerfile, отдельный Compose-проект, приватные runtime файлы,
IP:18766 и точный WSS path за nginx готовы. Docker smoke с двумя mock Goose,
nginx/WSS и L4/PROXY mirror прошёл. Конфигурация nginx проверена в отдельном контейнере.
Работающие службы и установленный бинарник не менялись. Runbook — [`docs/setup/vps-acceptance.md`](docs/setup/vps-acceptance.md).

- Постоянный Gateway на VPS со стабильным IP и/или доменом принимает запросы
  из Telegram и других каналов, маршрутизирует их и подтверждения выбранным
  компьютерам и агентам, хранит маппинг сессий и аудит.
- Собственный коннектор — вариант запуска той же программы (рабочее имя
  `acpgw connector`), устанавливающий исходящее WebSocket-соединение
  к VPS. VPN, Tailscale, SSH- и frp-туннели не входят в решение.
- Коннектор запускается автоматически при включении компьютера, переживает
  потерю сети, сон и выключение; при возвращении переподключается с прежней
  идентичностью. Сервер различает доступность компьютера и готовность агентов.
- Один компьютер может предоставлять несколько агентов; пользователь выбирает
  компьютер/агента в канале, сохраняя отдельные сессии и адресацию ответов и
  запросов подтверждений.
- Целевой агент — любой ACP-совместимый агент с учётом его объявленных
  capabilities и транспорта. Goose и Hermes — кандидаты для проверки,
  а не обязательные зависимости или закрытый список поддерживаемых агентов.
  Предусмотреть локальное подключение к сетевому ACP и мост к stdio.
- Рабочие файлы, инструменты и credentials остаются на компьютере агента;
  через VPS проходят запросы, ответы и данные подтверждений. Подтверждения
  маршрутизируются человеку; коннектор не разрешает действия самостоятельно.

Критерий: один VPS обслуживает несколько компьютеров и агентов, выбор и
подтверждения работают через каналы, отключение одного компьютера не мешает
другим, повторное подключение сохраняет идентичность и маппинг сессий;
поведение незавершённых задач и восстановления проверено по согласованным
при старте пункта правилам. Проверить Goose и Hermes как тестовые кандидаты,
общий контракт — на независимом ACP mock-агенте.

### P4.2. Snikket / XMPP

**Статус:** OPEN — после первого рабочего маршрута `P4.8.5` на основе `P4.1`.
**Репозиторий:** `acp-gateway` (`channels/xmpp/`)

Отдельный аккаунт (`work-goose@...`), allowlist JID, маппинг
`xmpp:<JID> → session`. Самохостинг даёт лучшую границу данных, чем Telegram.

### P4.3. Email gateway

**Статус:** OPEN — после `P4.2`.
**Репозиторий:** `acp-gateway` (`channels/email/`)

Allowlist отправителей, тред письма → сессия, только асинхронные ответы,
подтверждения — через другой канал.

### P4.4. Web UI

**Статус:** OPEN — после `P4.3`, поверх API `P1.5`.
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

**Статус:** CONDITIONAL — когда понадобится явно управляемый дочерний агент
без сетевого интерфейса. Несколько сетевых профилей уже поддержаны;
подключение к существующим backend относится к `P4.8`.
**Репозиторий:** `acp-gateway` (`agents/`)

Бэкенд `stdio` (Gateway запускает агента процессом: `goose acp`, Gemini CLI,
адаптеры Claude Code / Codex, Kiro), несколько профилей в `agents:`, выбор
агента в каналах. Удалённые stdio-агенты — вне объёма `P4.7`, их подключение
через коннектор относится к `P4.1`.

### P4.8. VPS-диспетчер → два Windows/Hermes-коннектора → Telegram

**Статус:** IN PROGRESS. Уточнение владельца 2026-10-13: завершить пригодную
для пользователя поставку этой схемы. Goose и другие agent/platform combinations
вынесены в P4.9. Все реальные установки и проверки собраны в отдельном P4.8.5,
с разделением действий агента и пользователя.

**Основные способы эксплуатации:** глобальный uv tool либо Git-копия с лёгкими
скриптами. На VPS — Linux/systemd user без обязательного Docker; на ПК — native
Windows Task Scheduler рядом с работающим Hermes Desktop. Обновление штатной
CLI-командой/скриптом сохраняет конфигурацию, ключи, SQLite и состояние службы,
без ручного удаления, нового enrollment и повторного первоначального setup.

**Подробный план и матрица сценариев:**
[поставка и разделение ответственности](docs/items/p48-delivery-plan.ru.md).
Принятый постоянный выбор диалога и предложения для будущих пользователей/входов:
[модель диалогов и аккаунтов](docs/items/dispatcher-accounts-and-dialogues.ru.md).
Техническая история и результаты предыдущих этапов сохранены в [журнале](road-notes.md).

#### Граница MVP

- Использовать P4.1: исходящий WSS, enrollment, маршруты computer/agent, ACP relay,
  jobs/sessions/approvals, Telegram. Агентские credentials остаются на своих ПК.
- Connector подключается к уже запущенному Hermes, не управляет его установкой,
  процессом, настройками или пользовательскими диалогами. Его собственные
  диалоги диспетчера нужно оставлять для записи только диспетчеру.
- При idle VPS reconnect живые connector/Hermes теперь сохраняют тот же managed
  dialogue через local session manager. Restart dispatcher сохраняет pointer и
  восстанавливает этот живой handle. Pending drop блокирует его без replay;
  потеря Hermes attachment/restart connector и старые disabled bindings требуют
  явного `/new`. Обычный reconnect не является cold resume после остановки Hermes.
- Активный logical dialogue сохраняется за пользователем и меняется только
  явными командами, не offline/reconnect/error. Если backend продолжить нельзя,
  выбор остаётся и показывается причина. Hermes — целевой агент; Telegram —
  основной текущий adapter общего ядра, будущий Web UI не меняет модель диалогов.
- Unsafe cancel, общий write Desktop/Telegram и cold resume не входят в MVP:
  нужен upstream ownership/turn contract. Ограничения показываются в CLI/Telegram.
- Windows и WSL — разные окружения. В этой поставке connector нативно в Windows.
- Docker остаётся дополнительным вариантом. Registry-публикация новой версии —
  отдельное действие; до неё доступен проверенный wheel/Git ref, не старый 0.1.0.

#### P4.8.1 — Контракт и ограничения Hermes

**Статус:** основа реализована. Есть discovery/probe, identity/auth, роли/профили,
выделенные диалоги в существующем Desktop 0.21.6 и recorded compatibility fixtures.
Сведения о safe cancel, восстановлении и approval settlement публикуются явно.
Общий write и upstream lease не считаются реализованными.
Артефакты: [attach](docs/setup/hermes-attach.md),
[контракт владения](docs/items/hermes-shared-control.ru.md).

#### P4.8.2 — Dedicated adapter, ACP relay и сохранение диалогов

**Статус:** основа реализована. Ограниченные RPC, connection-local handles,
фильтрация чужих событий/вопросов, once/deny, отзыв approval, exact cwd policy,
unknown-outcome quarantine и отсутствие replay/adoption/interrupt.
SQLite v5 сохраняет profile/runtime/stored ID/handle и историю атомарно;
sessions показывают attached/busy/uncertain/restore-unavailable.
Остаток этого слоя определяется реальными дефектами из приёмки P4.8.5;
добавлять неподтверждённый upstream recovery в обязательный MVP не требуется.

#### P4.8.3 — Завершённый CLI и настройка целевых ролей

**Статус:** READY как кодовая основа single-owner кандидата 0.2.0rc1.
Setup server/connector, saved role/launch, doctor, service, sessions/jobs/
approvals и model show/list/set/default/reconcile реализованы. Общие core/API,
logical selection/revision и route/confirmation guards используются CLI/Telegram.
Native Scheduler/ACL и installed-wheel CLI проверены; живые UX/автозапуск и
VPS/Telegram — P4.8.5. Полноценные accounts/workspaces относятся к P4.10.
[Runbook](docs/setup/p48-acceptance.md), [детали](docs/items/p48-delivery-plan.ru.md).

#### P4.8.4 — Поставка и штатное обновление обеих ролей

**Статус:** PARTIAL — кодовая основа и локальный кандидат 0.2.0rc1 готовы.
Wheel/sdist/bundle/locked requirements/checksums: dist/p48-0.2.0rc1;
source ref 3da8dffbd5d265c0b135e3aa1bd212e8e618c779, без публикации.
Uv/Git scripts, maintenance reservations и Linux/Windows service coordinators
реализованы; config/env/enrollment/SQLite сохраняются, setup не повторяется.
Source WSL: 1152/28; installed Linux wheel: 1152/28;
native source: 287/2; installed native wheel: 374/2, все 23 Scheduler cases.
Bounded session journal, exact known model-outcome recovery и explicit idle/
detached retention готовы. Prompt-loss/cold restore остаются fenced без replay.
**Остаток:** fresh target uv/Git installation, реальные systemd/Task Scheduler
package/service update/rollback и жизненный цикл хостов проверяются в P4.8.5.
[План/evidence](docs/items/p48-delivery-plan.ru.md), [runbook](docs/setup/p48-acceptance.md).

**Провайдер/модель Hermes — READY как кодовая основа кандидата 0.2.0rc1.**
Show/list/set/default/reconcile, CLI/Telegram, durable intent/operation ID,
pending/unknown, confirmation/route/revision guards реализованы.
Полный WSL source suite: **1152 passed / 28 skipped**, ruff clean.
Bounded connector journal и explicit idle-handle retirement готовы;
known model-loss reconciliation не повторяет mutation, prompt-loss остаётся fenced.
Реальный Hermes/provider/compute-host и пакетная приёмка — P4.8.5.
[Контракт и evidence](docs/items/hermes-model-control.ru.md),
[практический порядок проверок](docs/setup/p48-acceptance.md).

#### P4.8.5 — Отдельный этап установки и приёмки

**Статус:** PARTIAL. Вся установка и проверка текущего трека относятся сюда.
Сценарии и ответственная сторона перечислены в
[полной матрице](docs/items/p48-delivery-plan.ru.md).

**Подтверждено:** Linux source + isolated wheel 937 passed / 17 skipped;
native Windows installed wheel 221 passed / 3 POSIX skips, все 13 Scheduler
cases; discovery ACL, OWNER RIGHTS при trusted owner, ownership/locks,
graceful stop/force/instance fencing, mock ACP/relay/bindings/recovery/setup.
Hermes Desktop 0.21.6 ready до/после; live selector без сохранения, модель не вызывалась.
Это не полный uv tool update lifecycle и не живая VPS/Telegram приёмка.
После selection guards, saved connector launch и idle Hermes session manager
текущие исходники, включая checkout scripts, Windows wizard paths/ACL и task
setup/rollback, dispatcher/connector maintenance и Linux/Windows service updater,
прошли **1113 passed / 28 skipped** после model transport/core/API
(23 native opt-in + 5 compatibility).
Новый wheel не собирался; native
221/3 относится к предыдущей сборке. Consolidated Windows runner расширен
Telegram, saved-launch, live-manager и checkout helper cases для следующей пакетной приёмки.
Отдельно прошёл isolated Linux uv tool lifecycle текущего checkout через новые
scripts: install/run/update/repeated install/uninstall, config/env/key и
enrollment registry сохранены. Windows checkout и service update lifecycle открыты.
Отдельный native Windows source paths/ACL профиль: **20 passed**; disposable
private mkdir/write/read/public ACL rejection и fresh/repeat wizard save прошли
без сети, tasks и изменений Hermes. Это не новая installed-wheel приёмка.
Windows source Scheduler + wizard профиль: **28 passed**, все 19 Scheduler
cases выполнены на временных задачах, включая rollback после настоящего start.
После Windows coordinator native source профиль: **49 passed**, включая все
**23 Scheduler cases** и четыре сочетания enabled/running. Consolidated runner
теперь требует минимум 23 Scheduler cases без skips/failures. Полный установленный
Windows uv package/service update ещё не проверен.
Maintenance core/API/CLI профиль: 39 passed на Linux и 39 passed в native Windows
на текущих исходниках. Реальный Linux CLI/loopback daemon prepare/status/resume,
duplicate refusal и owner recovery прошли изолированно без служб/модели/update.
Connector manager/IPC/graceful source профиль в Windows: **26 passed**;
Linux real connector CLI/WS с fake Hermes прошёл status/prepare/resume, busy
refusal и продолжение того же чата. Новый installed-wheel прогон остаётся открытым.
Real isolated uv tool + dispatcher + fake systemctl: managed update и rollback
при invalid wheel, восстановление running/enabled, сохранение env/key/registry
прошли; smoke повторён после Windows изменений. Настоящий systemd lifecycle
и полный установленный Windows uv/task update ещё не приняты.

**Агент проверяет:** чистые изолированные uv/Git установки Linux/Windows,
скрипты/CLI, saved settings и ACL, управление службами, настоящий package update
и failure/rollback на тестовых данных; автоматизированные relay/Telegram сценарии.
При доступе к целевым хостам агент также выполняет установку VPS/двух connectors,
WSS/enrollment/Telegram настройку и собирает результаты живого маршрута.

**Пользователь обеспечивает:** VPS/SSH, домен/TLS и необходимые права хоста,
bot token/user ID и ключи через приватные файлы, доступ к двум Windows-ПК и
запущенный Hermes. Визуально проверяет Desktop/Telegram, отвечает на реальные
approval cards, выполняет reboot/logon, sleep/wake и физическую смену сети.
Доступные агенту операции не передаются пользователю только из-за прежнего handoff.

**Сценарии:** оба способа установки; VPS user service без интерактивного SSH;
setup и подключение каждого ПК; реальные запросы/approvals; home/work isolation;
idle WSS, disconnect, restart каждого слоя, key rotate/revoke; update/rollback/
uninstall с сохранностью данных; reboot/logon и sleep/network. Старый Docker
runbook [VPS/Hermes](docs/setup/vps-hermes-acceptance.ru.md) остаётся дополнительным.

**Готовность к передаче:** доработки P4.8.3–P4.8.4 и доступные агенту установки/
обновления выполнены. **Готовность Windows MVP:** дополнительно прошёл реальный
маршрут VPS → home/work Hermes → Telegram и целевой lifecycle. Непроверенные
живые/физические сценарии остаются открытыми в отчёте.

#### P4.8.6 — Перенесено в P4.9

Прежний Goose/platform milestone исключён из критериев завершения Windows MVP.

### P4.9. Расширение attach: Goose и платформенная матрица

**Статус:** DEFERRED по решению владельца 2026-10-13; после целевой P4.8 поставки.
Переиспользовать discovery/adapter/lifecycle contracts, не менять границу Hermes MVP.

- Уже готов read-only Goose explicit-endpoint probe: numeric loopback,
  X-Secret-Key из env, точный TLS pin, только initialize, bounded replies.
  Recorded Goose 1.53.0 mock evidence сохранён; live Desktop не проверен.
- Найти подтверждённый export/discovery/rediscovery контракт существующего Goose
  Windows/WSL; не извлекать key через scraping памяти/логов. Интегрировать в setup.
- Исследовать existing sessions/live attach/ownership, approvals/cancel/recovery
  для конкретных установленных версий Goose и других добавляемых агентов.
- Отдельный этап установки/приёмки P4.9: агент проверяет mocks и доступные native
  hosts; пользователь обеспечивает отсутствующие хосты и физические действия.
  Для Hermes/Goose Windows/WSL/Linux/macOS фиксировать discovery/auth, new/live
  attach/resume, approvals/cancel, reconnect/autostart отдельно. macOS LaunchAgent
  связан с P4.6. Архитектурная возможность не равна подтверждённой поддержке ОС.

Runbook: [Goose probe](docs/setup/goose-attach.md). Дальнейшие каналы управления,
offline queue и полный durable recovery остаются отдельными связанными треками.

### P4.10. Аккаунты и собственные коннекторы в общем диспетчере

**Статус:** PLANNED — подтверждено владельцем 2026-10-13. Цель: один dispatcher
и bot для нескольких независимых пользователей; каждый добавляет свои PCs и
управляет своими agents/dialogues/approvals. Это не текущая allowlist модель.

Принятый старт — invite-only личные workspaces; командные memberships,
sharing и биллинг отдельно после решения. User identity независима от Telegram,
Web UI/CLI/API обращаются к общему application layer с authenticated actor.
Owner API token остаётся административным, не раздаётся пользователям.

В P4.8 заранее заложить logical dialogue/active context и owner/workspace/actor
контракты, чтобы session manager и enrollment не закрепляли single-owner модель.
В P4.10 довести сквозную авторизацию storage/core/relay/events/approvals/API,
миграцию прежних ресурсов в default workspace, scoped dynamic routing,
invitations/pairing/revoke, per-user limits и audit. Поля owner_id сами по себе
не означают готовую изоляцию; второй независимый пользователь допускается только
после её полной реализации и приёмки.

Установка/проверки отдельным этапом: агент тестирует два аккаунта с одинаковыми
home aliases, чужие IDs/callbacks/events, pairing/revocation и миграцию; пользователь
предоставляет два Telegram accounts/PCs для живого smoke. Полные варианты,
предложения UX и сценарии: [диалоги и аккаунты](docs/items/dispatcher-accounts-and-dialogues.ru.md).

## 8. Очередь дальнейшей разработки

Точка продолжения P4.8 после перерыва: [checkpoint от 2026-10-13](docs/items/p48-resume.ru.md).
Зафиксированы source/API и durable model state, оставшаяся очередь, ограничения Hermes,
доказательства проверок и отдельный этап установки/приёмки.

**Следующий этап:** кандидат 0.2.0rc1 и отдельный P4.8.5 — установка/приёмка.
Кодовые доработки model defaults, CLI/Telegram, exact model recovery,
bounded session journal и explicit retention готовы; WSL source: 1152/28.
Владелец сначала устанавливает сам по [инструкции](docs/setup/self-install-0.2.0rc1.ru.md).
Начать с предоставляемого владельцем VPS (IP:port и domain/path), Telegram bot
и текущего Windows/Hermes, затем второй connector. Проверить native autostart
Hermes/connector, CLI обеих ролей, реальные model/approval и update/rollback.
Goose/platform combinations остаются P4.9, cold/shared write recovery не входят
в выбранный Windows MVP. [Runbook](docs/setup/p48-acceptance.md).
Перечень ниже — незакрытые смежные задачи, не prerequisites всего трека.

1. **P4.8.5 — живая установка и приёмка кандидата 0.2.0rc1.** VPS IP/domain-path,
   бот, текущий Windows/Hermes+connector/автозапуск/CLI, затем второй PC;
   [порядок проверок](docs/setup/p48-acceptance.md).
2. **P3.4/P3.5 — установка, настройка и контроль службы.** Основа Linux/WSL
   и самостоятельная Linux-сборка подтверждены чистым контейнерным CI;
   выпускать версии вручную из локальной сборки; Actions только по ручному запуску.
3. **P3.1/P3.5 — настоящий Windows reboot.** D-Bus восстановлен; systemd и
   Task Scheduler lifecycles уже подтверждены на текущем хосте.
4. **P2.3/P3.3 — живая приёмка Telegram.** Команды/подтверждения реализованы;
   persistent delivery и status edits готовы в development 0.2.0rc2.dev0,
   [журнал](docs/items/p3.3-reliability.ru.md). Кандидат установки — 0.2.0rc1.
5. **P4.1 — VPS-диспетчер и собственный WebSocket-коннектор.** Основа для `P4.8`:
   один владелец, ACP-релей. Enrollment и WS/WSS-регистрация
   готовы; инъекция транспорта в AgentClient реализована и проверена на mock ACP
   без WebSocket; локальная политика ACP реализована и проверена (методы,
   точные cwd, режимы, MCP/capabilities, live once-only approvals). Решения ревью
   приняты; TLS выключен для IP/port тестов, paths и будущий nginx поддержаны.
   Первый guarded ACP relay в serve готов и mock-tested.
   Два узла и изоляция mock-tested; служба и диагностика готовы.
   Дальше: живая приёмка в `P4.8.5`, затем recovery и stdio по их триггерам.
6. **P3.2 — Windows native**, в составе `P4.8.4`, вместе с
   Windows-частями `P3.4/P3.5`; Scheduler/ACL/locks уже проверены,
   остаются целевая поставка/update и сценарии P4.8.5 reboot/logon/sleep/network.
7. **P4.2 → P4.3 → P4.4 — Snikket, Email, Web UI.** После первого рабочего
   маршрута `P4.8.5`, не блокируют разработку адаптеров.

Условные `P4.5`–`P4.7` сохраняют свои триггеры.
