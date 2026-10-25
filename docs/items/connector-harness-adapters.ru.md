# P4.13 — адаптеры других harness для коннектора

**Статус:** PARTIAL — managed Codex/Claude CLI реализованы в source; живая приёмка, Desktop и native chat list/attach остаются.
**Решение 2026-10-23:** работа на паузе, P4.13.1 OPEN — следующий подэтап только при явном возвращении владельца; baseline принят отдельным незакрытым долгом.
Остаток: живая модель/once/deny/Telegram на Windows-хосте и в WSL, Codex helper
lifecycle, Desktop и native chat list/attach-by-ID. Managed Claude Code CLI уже
реализован; Gemini/OpenCode не реализованы, реальные model calls не выполнялись.
[Точка возврата и план приёмки](../plans/p4.13-resumption.ru.md).
P4.9 сохраняет отдельный Goose/platform трек; P4.7 — общий stdio lifecycle.
P4.13 добавляет конкретные native protocol adapters, не объявляет всё ACP.

## Что означает «подключить агента»

| Режим | Что требуется доказать отдельно |
|---|---|
| Managed | Connector запускает собственный процесс/сервер и владеет lifecycle |
| External endpoint | Пользователь запускает документированный local server, connector проверяет identity/auth |
| Resume owned | Продолжение своего сохранённого dialogue по точному ID и проверенным ограничениям |
| Live attach / shared UI | Тот же native runtime; несколько равноправных источников сообщений, согласованное исполнение и однократное разрешение approvals (решение 2026-10-22) |

Managed new dialogue — предлагаемый первый MVP. Resume сохранённого transcript
не равен безопасной параллельной записи в тот же активный Desktop/IDE chat.
Наличие MCP client также не означает, что harness можно управлять как ACP server.
UI automation, ввод в чужой терминал, scraping памяти/credentials и запись в
чужие chat databases не предлагаются как интеграционный контракт.

## Проверенная документация и кандидаты

| Harness | Документированная точка входа | Предлагаемый путь |
|---|---|---|
| Codex | App Server: двунаправленный JSON-RPC, threads/turns/events/approvals; exec — отдельный noninteractive путь | Managed local App Server через stdio, затем owned thread resume; existing app/socket attach отдельный spike |
| Claude Code | Agent SDK запускает Code binary, предоставляет sessions/permissions; CLI -p и JSON/stream-json | Managed SDK/двунаправленный процесс с явными permission callbacks; CLI batch как ограниченный режим |
| Gemini CLI | Headless -p и text/JSON/stream-json events | Сначала bounded managed jobs; интерактивные approvals и ACP проверять отдельно |
| OpenCode | Local HTTP server, sessions/abort/permissions, SSE и OpenAPI | External или managed server adapter, затем проверка existing-server ownership |
| Goose | Существующий проектный ACP backend/probe | Продолжить P4.9 discovery/attach/platform acceptance, не дублировать |

Источники, прочитанные 2026-10-13:
- [Codex App Server](https://learn.chatgpt.com/docs/app-server) и
  [noninteractive mode](https://learn.chatgpt.com/docs/non-interactive-mode).
  stdio подходит для локального процесса; WebSocket transport в документации
  помечен experimental/unsupported. Источник интерфейса не доказывает поддержку
  конкретной версии установленного Desktop и его shared writer semantics.
- [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview),
  [permissions](https://code.claude.com/docs/en/agent-sdk/permissions) и
  [programmatic CLI](https://code.claude.com/docs/en/headless).
  SDK — программный запуск harness; SDK permissions и interrupt требуют
  собственного mapping. Resume выбирается по конкретному session ID, не --last.
- [Gemini headless](https://geminicli.com/docs/cli/headless/).
  JSONL содержит session/messages/tool/result events; наличие output stream
  само по себе не подтверждает интерактивную доставку human approvals.
- [OpenCode server](https://opencode.ai/docs/server/).
  Документация различает отдельный serve и сервер TUI: запуск serve при открытом
  TUI создаёт новый server. Existing-server endpoint/discovery нужно задавать
  и проверять явно, а не угадывать port/process identity.

Это оценка технической реализуемости по публичным контрактам, не матрица
поддерживаемых ACP Gateway агентов. Правила provider auth/account/billing,
совместимость CLI/SDK и OS подтверждаются для выбранных версий отдельно.
Не предполагается автоматическое копирование subscription credentials в VPS.

## Архитектура адаптера

Общий connector driver contract поверх native transport:
identity/probe, capabilities, new dialogue, owned resume/list, prompt/events,
human permission bridge, cancel/terminal outcome, model/config observation,
shutdown/health и bounded diagnostic journal. Unsupported операции явно
недоступны. Не превращать stdout diagnostics/tool arguments в assistant answer.

Capabilities разделять: live attach, stored resume, concurrent readers/writer,
cancel with ACK, approval kinds/TTL/expiry, model catalogue/set/default,
idempotent submit/outcome lookup, own scheduling и cost bounds.
Особенности одного harness не выдаются за гарантию остальных; ACP bridge —
один adapter, а не требование форсировать все native API в один wire protocol.

Execution на host пользователя: native Windows и WSL — разные профили.
Pin executable/version/auth profile/cwd и process instance/epoch. Prompt идёт
через RPC/stdin, не shell string. Exec argv явный, stderr/logs bounded/redacted.
Отдельные owned process groups и local ACL/IPC; остановка connector не должна
убивать внешний IDE/Desktop или чужие процессы.

Внешний TLS connector link уже отделён от local agent transport. Agent auth
остаётся на локальном host; публичный native agent listener на VPS не нужен.
Расширение типов backend в config/registry делается после spike/acceptance,
с version negotiation и capability refusal для старых connectors.

## Permissions, resume и сценарии

Per-user/workspace authorization P4.10 до маршрутизации, local connector policy
повторно ограничивает cwd/tools/permissions. Не использовать автоматический
bypass только потому, что CLI batch иначе не умеет показать вопрос человеку.
Unsupported human approval flow означает restricted automation mode либо
отказ в risky operation, а не скрытый auto-approve.

Resume только точного owned dialogue/session. Не применять global --last/--continue:
на одном host это может выбрать чужую или другую беседу. Cancel подтверждается
конкретным turn ID/outcome, а process kill без протокольного ACK — отдельный outcome.
Lost prompt ACK/connection — unknown, без автоматического prompt retry.
Автоматические upstream resume-interrupted-turn настройки в managed профиле
должны быть явно отключены или проверены на совместимость с этим правилом.

P4.11 хранит required capabilities. Сценарий не запускается на profile, где нельзя
выполнить нужные new/resume/approval/model правила; другой агент не подставляется
молча. Для первого scheduled run предпочтителен отдельный owned dialogue.

## Выбор агента одного коннектора — решение владельца 2026-10-20

Владелец запросил будущий пользовательский сценарий: один коннектор ПК подключён
к нескольким локальным агентам, например Hermes и Codex; перед отправкой задания
пользователь явно выбирает адресата и может переключаться между ними без
переподключения компьютера. Claude Code включается в тот же контракт после
реализации его адаптера. Это требование к P4.13, не отдельный новый connector
и не поручение начать реализацию; очередь P4.17 → P4.15.1 не меняется.

Существующая база: `connectors/commands.py` создаёт клиентов по локальным alias,
а Telegram `/agent` показывает маршруты и сохраняет выбранный маршрут в channel
state (`channels/telegram.py`). Новый сценарий расширяет эту базу на native
harness adapters, а не вводит глобальный единственный active agent коннектора.
Текущая база не означает реализованную поддержку Codex/Claude.

Целевое поведение и приёмка при реализации P4.13:

- Один компьютер публикует отдельные маршруты `computer/hermes`, `computer/codex`
  и при наличии адаптера `computer/claude`; подключение к диспетчеру остаётся общим.
- Перед отправкой видны выбранные компьютер, агент и диалог. Выбор через CLI и
  Telegram использует общий routing contract; точный CLI UX уточняется в плане.
- Переключение меняет адресата следующих отправок, не переносит историю или native
  session ID между harness и не меняет адресата уже принятой задачи/approval.
- Диалоги, результаты и подтверждения сохраняют привязку к исходному агенту.
  При возврате показывается его диалог; восстановление после restart доступно
  только при явно проверенной capability, без обещания универсального cold resume.
- Недоступность выбранного агента объясняется пользователю; скрытой подмены другим
  агентом или повторной отправки задания после reconnect нет.
- Проверить Hermes ↔ Codex и затем Claude: выбор до отправки, возврат к диалогу,
  работа/approval прежнего агента после смены выбора, недоступный маршрут,
  reconnect и одинаковые локальные alias на разных компьютерах.

## Предлагаемый порядок

1. Общий driver capability/lifecycle contract и fake native transport tests.
2. Codex managed local App Server spike; version/schema probe, new/turn/events,
   approvals/cancel/owned resume, отдельные native Windows/WSL профили.
3. Claude SDK managed spike с permission callbacks и session continuity.
4. По запросу — Gemini/OpenCode и остальные harness с документированным API.
5. Existing Desktop/IDE attach и shared writers — отдельная приёмка, не обещание MVP.

Этот порядок — предложение для обсуждения. P4.9 по прежнему решению владельца
остаётся DEFERRED до P4.8 поставки; новая идея не активирует его автоматически.
Для Cursor, Copilot, Aider, Kiro и других сначала исследовать официальный
automation/server/SDK контракт; поддержка здесь пока не подтверждалась.

Автотесты: malformed/oversized stream, capability mismatch, exact session/turn,
lost ACK, permission expiry/revoke, process crash/cleanup, foreign process,
stderr secrets, two connectors/users with same alias, OS path boundaries.
Живые: согласованные runtime/provider credentials, отдельный test dialogue,
один разрешённый model call, once/reject/cancel и reopen по recorded ID.
Только после этого кандидат получает статус supported в версии/OS матрице.


## Решение владельца 2026-10-21

Владелец выбрал следующий порядок разработки:
1. Поддержка Codex и Claude Code в CLI и Desktop вариантах.
2. Подключение к существующему чату агента по его ID прямо через Telegram-диспетчер.
3. VK как альтернативный Telegram канал (P2.7).

Это заменяет прежний статус P4.13 как направления без выбранного ближайшего
приоритета и прежнюю очередь только P4.17 → P4.15.1. На момент этого решения
адаптеров ещё не было; ниже записан результат последующей реализации Codex.
Проверки уже опубликованных pairing/updater остаются долгом приёмки, не закрываются
сменой приоритета. Push, релиз и изменения production этим решением не разрешены.

### Границы и открытые вопросы

- CLI, Desktop, owned resume и live shared attach — отдельные проверяемые режимы.
  Создание managed процесса не доказывает поддержку существующего Desktop-чата.
- При подключении по ID выбирать computer/agent/profile и точный native ID;
  ID диспетчера, native session/thread ID и Telegram chat ID не взаимозаменяемы.
  Знание ID само по себе не даёт права доступа к диалогу.
- Перед реализацией уточнить: Desktop означает официальные приложения Codex/Claude,
  а не UI диспетчера; какие агенты входят в первый attach-by-ID (включая ли Hermes).
- Нужны read/idle/busy/unknown и writer/approval ownership, отказ чужим IDs,
  безопасное продолжение после завершённого хода и отсутствие prompt replay.
  Если upstream не даёт нужного управления существующим чатом, зафиксировать
  ограничение; не создавать другой чат под видом подключения к исходному.
- Предлагаемый первый технический шаг: актуальные официальные контракты и
  inventory установленных версий, затем общий contract и Codex spike; Claude Code
  следующий. Модельные вызовы и реальные test dialogues согласовываются отдельно.


## Начало реализации 2026-10-21

Владелец разрешил разработку строго по одному пункту плана. Активен только P4.13;
первый ограниченный подэтап — managed Codex CLI через локальный stdio App Server.
Claude Code, Desktop attach, Telegram attach-by-ID и VK не входят в эту итерацию.
Проверки — test-first, fake native peer, existing AgentClient/connector integration
и реальный локальный handshake без модельного запроса. Работающие службы,
credentials, production, push и релиз не меняются. Живую модельную приёмку
нельзя заменить синтетическими ответами или объявить по одним автотестам.


## Реализация и проверка managed Codex 2026-10-21

Реализован только первый подэтап P4.13 в ветке `feature/p4.13-codex-managed`;
production, установленные службы, аккаунты, push и release не менялись.
Кодовая версия остаётся 0.2.2, но этот адаптер **не входит** в опубликованный 0.2.2.

- `adapters/codex.py`: управляемый stdio App Server, собственные новые диалоги,
  текстовые ходы/stream и item completion без дублирования, once/deny permissions,
  отказ чужим thread/turn/item, withdrawal, bounded queues/frames и закрытие процесса.
- `kind/backend: codex`, allowlisted child environment и manual YAML; существующие
  AgentClient, connector manifest/relay, maintenance accounting, config check/doctor.
  Setup сохраняет ручной профиль, но не редактирует его как Goose молча.
- Реальные схемы установленного Codex **0.163.0**: thread approval `untrusted`,
  reviewer `user`, sandbox `read-only`/`workspace-write`, turn sandbox camel-case.
  Read-only по умолчанию; Gateway не обещает approval каждого разрешённого upstream
  инструмента. Permissions не расширяются до session/always и не bypass-ятся.
- Cold resume/load, импорт native ID и safe native cancel отключены явно. Потеря
  prompt/таймаут не допускают replay или подмены новым диалогом. Закрытие собственного
  процесса не доказывает остановку всех потомков или откат действия.

### Независимое ревью и устранённые дефекты

Первое ревью воспроизвело два дефекта без модели: file approval не включал diff/paths,
а завершившийся child оставлял открытые stdout/stderr, если ими владел потомок.
Оба исправлены с наблюдаемыми RED→GREEN регрессиями: точные action details в
PermissionRequest, fail-closed при неполных/чужих данных и bounded closure локальных
pipe transports независимо от EOF потомка. Чужие процессы не завершаются.
Повторное независимое ревью — без блокирующих замечаний; focused integration:
**41 passed**, дополнительно проверены SIGKILL escalation/cancelled close и семь
негативных/положительных approval probes. Private asyncio subprocess transport API
остаётся платформенной границей; native Windows этим не подтверждается.

### Evidence итогового дерева

- Родительский полный WSL source suite: **1858 passed / 38 skipped**, 457.88s (0:07:37).
  Лог: `p413-parent-full.log` в Hermes scratch текущей сессии.
- Родительские Ruff check, format check и `git diff --check` прошли.
- Реальный Codex через новый AgentClient adapter: initialize успешен, cancel/load
  capabilities false, owned child exit **0**, создано **0** threads, отправлено **0** prompts.
  CODEX_HOME изолирован; модель/подписка не вызывались, credentials не изменялись.
- Первый полный прогон исполнителя дал 1 failure в allowlist doctor async boundary;
  добавлена новая операция **и** behavioral test реального owned runner/closed loop.
  Его повторный suite был 1847/38; числа выше — отдельный прогон родителя после review fixes.
- Первый родительский foreground suite был оборван лимитом обёртки инструмента
  420 секунд без итогового verdict; итоговый прогон выполнен tracked background job,
  не считается повторением «до зелёного» известной test failure.

### Остаток первого managed-подэтапа (историческая запись)

На момент закрытия первого подэтапа P4.13 оставался **PARTIAL**, не production supported.
Нужны были native Windows subprocess проверки, согласованный отдельный test dialogue
и live model/once/deny/Telegram round trip, удобный setup. Ниже записано, что из этого
закрыто следующим подэтапом; Claude Code и Desktop по-прежнему не реализованы. Load/resume/cancel
в этом первом подэтапе не обещаны. Реализация Telegram attach-by-ID и VK не начата.
Следующий выбор — внутри P4.13, по отдельному поручению, без автоматического перехода
к другому пункту очереди или релизу.

[Инструкция экспериментального source-пути](../setup/codex-managed.md).


## Продолжение Codex: native Windows и setup — 2026-10-21

По поручению «продолжай» выбран следующий подэтап внутри P4.13: проверить native
Windows subprocess/cleanup и добавить Codex в обычный мастер профилей. В область
не входят Claude Code, Desktop shared attach, Telegram attach-by-ID, VK, модели,
production и публикация. Native окружение Python отделено от WSL `.venv`.
Предыдущие source/runtime/docs изменения сохраняются; новый полный suite нужен
после интеграции, исторический 1858/38 не считается проверкой нового дерева.


### Подтверждённое ограничение native Codex lifecycle

Настоящий Windows Codex 0.163.0 проверен из официального npm-пакета, полученного
отдельно в scratch с проверкой SHA-512 SRI. Desktop package resources отказали
в прямом запуске; ACL/WindowsApps не изменялись и ограничения не обходились.
Native venv Python 3.12.13 отдельный, WSL `.venv` не переиспользуется.

Initialize с изолированным CODEX_HOME прошёл без threads/prompts. Локальные stdio
handles закрылись, subprocess transport завершился; однако немедленное удаление
тестового cwd дало WinError 32. Исследование scoped Win32 handles подтвердило:
startup `git.exe` (потомок Codex) ещё держит каталог. Baseline — 5/5 воспроизведений;
300 ms EOF grace даёт voluntary exit 0, но блокировка остаётся в 5/5. Родительский независимый baseline: 4/5 попыток WinError 32, каталог освобождался
сам через **1.75–1.84 s**; процессы Helper после этого отсутствовали. Поэтому не
добавлен ложный shutdown fix, broad kill,
обход ACL или заявление о готовом полном native lifecycle. Остановка непосредственного
child не равна гарантии завершения всех агентовых потомков. Диагностические временные
каталоги исследования очищены; полный native acceptance остаётся открытым.

Первый native shared-peer прогон выявил не runtime-дефект, а тестовую ошибку:
100 ms session-creation deadline выставлялся ещё до Windows interpreter handshake.
Тест исправлен: короткий deadline включается после connect, проверяется именно
потеря thread/start, не скорость запуска Python. Shared+Win handle checks после правки: **84 passed / 2 skipped** (native Windows,
144.54 s); WSL shared checks: **41 passed / 4 skipped**. Оба числа — прогоны родителя.

Мастер профилей добавлен отдельным test-first подэтапом. При native запуске нового
full-setup теста обнаружена ещё одна тестовая ошибка: mock общего shutil.which и
subprocess.Popen ломал настоящий Windows ACL verifier. Module-local proxies
оставили его работающим; точная native регрессия стала GREEN (1 passed).
Doctor переведён с устаревшей подсказки manual YAML на `acpgw agents configure`
после наблюдаемого RED→GREEN. Production, модельные вызовы и аккаунты не менялись.


## Итог setup и native Windows 2026-10-21

### Что реализовано

- `agents configure` и обычный `setup` предлагают **Codex** наряду с Goose/Hermes/generic.
  Создание, редактирование, сохранение, добавление нескольких профилей и переключение
  типа/роли выполняются существующим мастером. Дубликат адреса переспрашивается до
  изменения списка; отказ не мутирует профили.
- Для локального профиля запрашиваются alias, рабочий каталог, executable (имя PATH
  или абсолютный native path), необязательная модель и sandbox. `read-only` —
  умолчание; `workspace-write` — явный выбор. Пользовательские `request_timeout`/
  `turn_timeout` и прочие неуредактированные настройки сохраняются. Секрет не
  запрашивается, установка и `codex login` не выполняются.
- При переключении Codex ↔ ACP/Hermes удаляются несовместимые transport/managed поля;
  routed server-профиль требует computer/cwd и не содержит локальный executable.
- `_check_agents` выполняет только initialize и печатает Codex-специфичную подсказку
  (executable/PATH, cwd, отдельный login), не выдавая её за TLS/secret-проверку.
  Doctor направляет в `acpgw agents configure` вместо прежней manual-YAML подсказки.
- Native Windows subprocess-путь проверен отдельно от WSL: Proactor pipes, JSONL,
  approvals, relay и собственный cleanup; четыре Windows-регрессии покрывают
  inherited stdout/stderr, отменённый caller close и сохранность чужих процессов.

### Evidence и известные ограничения

- Native Windows Codex-подмножество (config, setup CLI, managed/relay и Win handles):
  **84 passed / 2 skipped** (144.54 s), отдельный native venv и Python 3.12.13.
  WSL shared-подмножество: **41 passed / 4 skipped** (9.91 s).
- Настоящий standalone Codex **0.163.0** Windows x64 получен из официального npm-пакета
  с проверкой SHA-512 SRI. Initialize с изолированным CODEX_HOME прошёл без threads/
  prompts; модель, login и аккаунт не затрагивались. Прямой запуск из installed Desktop
  package resources был отклонён ОС; ACL/WindowsApps не менялись.
- Родительский независимый baseline подтвердил startup helper `git.exe`: 4/5 WinError 32,
  каталог освобождался через **1.75–1.84 s**; 300 ms graceful EOF не снимал lock (5/5),
  хотя immediate Codex завершался с кодом 0. Это upstream lifecycle, а не повод
  завершать все потомки: процесс-tree kill не реализован и полная native agent
  acceptance не заявлена.
- Интегрированный полный WSL source suite после всех правок:
  **1872 passed / 42 skipped / 4 failed** (533.99 s). Все четыре падения вне Codex:
  три daemon-CLI timeouts и TLS fake-Goose. `test_serve_command_binds_loopback...`
  воспроизводится **2/2 на чистом worktree HEAD** — pre-existing граница ожидания 4 s
  при импорте daemon; остальные два падения проходят отдельно и на текущем дереве,
  и на HEAD. Новых падений P4.13 нет; полный зелёный suite не заявляется.
- Ruff check/format и `git diff --check` чистые. Независимое read-only ревью setup/
  Windows-изменений: **passed**, blocking findings отсутствуют; оно отдельно
  рекомендовало не закрывать native lifecycle до helper acceptance.
- Тестовая ошибка первого native setup-прогона (глобальные mock `shutil.which`/
  `subprocess.Popen` отключали настоящий ACL verifier) исправлена module-local
  proxies; RED native теста 1 passed после правки. Это была тестовая изоляция,
  не изменение ACL-проверки.

### Точка продолжения

P4.13 остаётся **PARTIAL**. Следующий подэтап требует отдельного поручения владельца:
либо live model/once/deny/Telegram на согласованном отдельном test dialogue, либо
Claude Code adapter, либо решение по upstream helper lifecycle. Desktop attach и
Telegram attach-by-ID не реализованы; VK не начат. Push, релиз, production services
и пользовательские аккаунты не менялись.

### Незакрытые вопросы

- Нужен ли явный механизм отслеживания/ожидания startup helper потомков (например,
  контролируемое дерево процессов с отдельным согласием), или это должно остаться
  ответственностью upstream Codex?
- Какие именно Codex CLI/Desktop версии и OS входят в первую поддерживаемую матрицу;
  текущая проверка — 0.163.0 на Linux/WSL и native Windows x64.


## Claude Code и Telegram native chat list — решение 2026-10-21

Владелец явно выбрал: **сразу перейти к Claude Code**, а живую приёмку Codex и
Claude провести затем на этом native Windows-хосте и в WSL. Прежний порядок,
где live Codex acceptance шёл до Claude, заменён этим решением. Codex helper
lifecycle остаётся известным ограничением, не закрывается сменой очереди.
Текущая итерация — managed Claude Code CLI, без живых модельных вызовов,
изменения аккаунтов/production, push и релиза. Desktop не включается автоматически.

Отдельно уточнён следующий пользовательский сценарий P4.13:

1. В Telegram выбрать компьютер и агента, опубликованного его коннектором.
2. Прямо в Telegram запросить **список native чатов этого агента**: native ID,
   название/краткий контекст, рабочий каталог и доступность, если backend их предоставляет.
   Это не только уже созданные диспетчером диалоги.
3. Выбрать чат кнопкой либо ввести его native ID и подключиться к **исходному чату**,
   после чего посылать сообщения и получать ответы/подтверждения через Telegram.
4. Проверять computer/agent/profile, права, текущий busy/unknown и допустимое владение
   записью; native ID сам по себе не является правом доступа. Если backend не допускает
   безопасного режима, показать ограничение, не создавать молча новый чат.
5. Native chat ID, logical dialogue ID диспетчера и Telegram chat ID не смешивать.
   Получение списка/просмотр не запускают модель, prompt или auto-continue.

Сверка текущего кода: Telegram `channels/telegram.py` `/sessions` получает
`self.core.sessions(conv)`, показывает integer dispatcher row IDs; `/switch`
проверяет владение этим dialogue. Codex `session/list` перечисляет только свои
живые dedicated handles. **Discovery/list всех native чатов и их импорт/attach
по ID ещё не реализованы**; эта итерация фиксирует требование, не активирует его
параллельно с Claude adapter. Для Hermes/Codex/Claude существующий stored resume
и attach к открытому UI проверяются отдельно по реальному upstream-контракту.

### Начальные данные Claude

WSL установленный CLI: `claude 2.1.306` (`--version`/`--help`, без модели).
Официальные исходники Anthropic Agent SDK query/transport/client прочитаны через
Context7 и raw GitHub; native stream-json control-протокол отличается от ACP.
В частности initialize/can_use_tool/control_cancel_request и terminal result
требуют своего mapping, exact process/session scope и fail-closed/no-replay.
Wire `manual` в установленном CLI не подменяется прежним default/bypass значением.


### Claude: стартовые model-free проверки 2026-10-21

* Конфигурация `ClaudeCodeSettings`, local `kind/backend: claude_code` и connector
  relay kind добавлены. До реализации первый тест падал с literal_error kind/backend;
  после реализации config-подмножество вместе с Codex: **43 passed**, Ruff зелёный.
  Это проверка конфигурации, не завершённый runtime/relay.
* Два isolated запуска настоящего WSL `claude 2.1.306` ответили success на native
  `initialize`; собственный child завершился exit **0**. **0 user messages, 0 model
  calls**. HOME/USERPROFILE/CLAUDE_CONFIG_DIR изолированы, чужие credential env не
  передавались; пользовательские аккаунты и CLI-настройки не менялись.
* Повторный initialize подтвердил приём `--max-turns 16` и settings с
  `permissions.ask` для Read/Glob/Grep/Edit/Write/Bash. Это НЕ доказывает живой
  can_use_tool callback: реальная once/deny/Telegram приёмка отложена владельцем.
* Источники: официальный Anthropic Python Agent SDK (query.py, client.py,
  subprocess_cli.py) и Context7 `/websites/code_claude`, первичные страницы
  https://code.claude.com/docs/en/settings-reference,
  https://code.claude.com/docs/en/agent-sdk/permissions,
  https://code.claude.com/docs/en/permission-modes.
  `manual` — CLI alias config-value `default`; один manual сам по себе
  авторазрешает часть reads. Явные ask rules нужны для выбранных tools;
  никаких `allowedTools`/bypass/persistent grants не добавлять.
  EndConversation имеет upstream-исключение для deny/ask и не является доступом
  к shell/файлам. Нельзя обещать OS sandbox или полный перехват всех native tools.
* Direct docs `.md` fetch получил 403; web_extract текущего backend не поддержан.
  Документация проверена через primary-source indexed Context7, не через зеркала.

Adapter/subprocess tests и интеграция с AgentClient/connector/CLI/setup находятся
в разработке в непересекающихся файлах. Это один пункт P4.13, не параллельная
реализация следующего native-chat-list/attach или VK. Полная приёмка и общий
suite на этом шаге ещё не выполнены; старые результаты Codex не переименовываются
в результаты Claude.


### Claude: runtime и frontend integration, до итогового ревью

Реализованы native stream-json adapter, отдельный owned child на диалог,
последовательные turns с тем же native UUID, once/deny с исходным tool input,
withdrawal, bounded queues/frames/processes, maintenance admission/accounting,
owned-pipe cleanup. `--no-session-persistence` включён явно; no replay/load/cancel.
AgentClient, connector manifest/relay/launch/maintenance, config check, doctor и
wizard используют тот же backend `claude_code`, не параллельный gateway.

Первичный родительский повтор config/CLI/wiring/frontends: **53 passed in 7.40s**.
Общий WSL suite и native Windows объединённый набор запущены на итоговом дереве;
их итог ещё не получен и сюда не подставляется результат исполнителей.
Ruff `check src tests`, `format --check src tests` и `git diff --check` прошли.
Статический scan всех 12 изменённых/untracked source `.py` по hardcoded credentials,
shell=True, os.system, eval/exec и pickle не нашёл совпадений; это не замена ревью.
Независимое read-only ревью отправлено, итог ещё не получен.

Настоящий WSL Claude 2.1.306 проверен уже **через новый AgentClient/adapter**, а не
только отдельным wire-пробником: initialize success, один dedicated child,
**0 managed sessions, 0 model prompts**, `loadSession=false`. После close owned
child завершился exit **143** (SIGTERM/native wrapper), transport closed;
изолированный каталог удалён. Это проверка init/cleanup, не успешный real turn и
не доказательство отсутствия всех upstream helpers. Локальные аккаунты/production
не менялись, commits/push/release не выполнялись.


### Claude: независимое ревью не принято, два fix required

Родительский native Windows config/CLI/wiring/setup/frontends/adapter набор до
review-fixes завершился: **120 passed in 63.11s**. Это Python synthetic subprocess,
не реальный Windows Claude CLI и не живая модель. Общий WSL suite ещё выполняется;
даже зелёный результат до исправлений не закрывает найденные дефекты.

Read-only reviewer проверил новые untracked adapter/tests и shared diff; verdict
**passed=false**. Два дополнительных synthetic probe воспроизвели:

1. **P1:** native assistant frame с top-level `error: authentication_failed` и
   diagnostic text попадает в `agent_message_chunk`; поздний failed result уже
   не отзывает опубликованный текст. Нужно отбрасывать/обезличивать error-marked
   assistant до выдачи текста.
2. **P2:** `_permission_response` удаляет pending permission до ожидания child
   write lock. Native `control_cancel_request` между этим удалением и actual write
   больше не находит запрос, не публикует withdrawal, а старый allow уходит позднее.
   Это доказательство stale forwarding, не доказательство native tool execution.
   Decision должен оставаться revocable с финальной проверкой под write lock.

Выделен отдельный fix worker только на adapter и regression tests, без изменения
планового объёма. Нужны RED обеих репродукций, GREEN, новый родительский прогон
и повторное независимое ревью. В setup guide добавлено предупреждение не давать
этому промежуточному snapshot реальный model/tool access. Результаты старых
48/53/120 tests не заменяют отсутствующую защиту от этих двух сценариев.


### Claude: два review-fix реализованы, итоговая перепроверка идёт

Изменены только adapter и его integration tests. Error-marked assistant отвергается
до dedup/content публикации с generic failure. Queued permission остаётся pending
до commit; под child write lock проверяются exact pending identity, owner,
child/session/current turn, done и closed. Между удалением pending и stdin.write
нет await. Уже отправленное решение не может быть отозвано задним числом.

Fix worker получил RED **8 failures**, затем GREEN **8 passed** для standalone/
streamed-id diagnostic и accept/decline × native cancel/terminal result/close.
Родительский повтор именно этих регрессий: **8 passed in 3.75s**. Ruff по всему
репозиторию и format-check **355 files**, `git diff --check` прошли.
Новый полный WSL suite и объединённый native Windows набор запущены после завершения
последнего writer; независимое повторное review отправлено. Их результаты ещё
не получены — блокеры не объявляются принятыми лишь по словам fix worker.

Исторический полный WSL suite ДО fixes завершился **1977 passed, 42 skipped,
489.20s** (log `p413-claude-parent-full.log`). Ранее упоминавшиеся четыре падения
Codex-подэтапа в этом прогоне не повторились; причины всех старых падений этим не
доказываются. Этот зелёный результат не заменяет новый post-fix прогон.


## Итог Claude Code managed CLI 2026-10-21

Кодовый managed CLI-подэтап завершён после исправлений, но весь P4.13 — **PARTIAL**.
В исходниках: отдельный native child/UUID на диалог, последовательные turns,
text streaming, once/deny с исходным input, withdrawal, bounded resources,
uncertain/no-replay fencing, connector relay/maintenance, config check,
обычный setup/edit/type-switch и initialize-only doctor/preflight.

Родительские проверки после последнего runtime writer:

* **Полный WSL suite: 1985 passed, 42 skipped, 496.32s (0:08:16).**
  Log: `p413-claude-fixed-parent-full.log` в Hermes scratch.
* **Native Windows объединённый Claude config/CLI/wiring/setup/adapter/frontend
  набор: 128 passed, 50.61s.** Log: `p413-claude-fixed-parent-native.log`.
  Это реальный Python subprocess/Proactor pipes с synthetic peer, не установленный
  настоящий Windows Claude и не модель.
* Ruff check по всему репозиторию, format-check **355 files**, git diff --check —
  успешно. Две review-регрессии родителем перепроверены отдельно: **8 passed**.
* Повторное независимое review `deleg_eca7412c`: **passed=true**, security_concerns
  и logic_errors пусты. Рецензент сообщил о 8 targeted cases и 13 дополнительных
  in-memory probes: дубли human responses пишутся один раз; cancel до commit
  не пишет decision. Неблокирующая рекомендация — сделать дополнительные probes
  постоянными regression tests. Это не утверждение отмены уже отправленного tool.

Реальный WSL Claude **2.1.306** прошёл initialize через новый AgentClient/adapter
в изолированном HOME/config без credentials: **0 prompts и 0 managed sessions**,
owned process closed. Это проверка wire/startup, не login/model approval acceptance.
Без model calls, изменения аккаунтов/production, commits/push/release.

Остаток по решению владельца: совместная живая приёмка Codex/Claude на этом native
Windows-хосте и в WSL. Native Windows CLI, реальные turns/once/deny/Telegram пока
не приняты; Codex Git-helper lifecycle остаётся открытым. Далее требуется отдельный
контракт Desktop и **Telegram: компьютер → агент коннектора → список native чатов
с ID → подключение к выбранному исходному чату**. Текущие `/sessions`/`/switch`
и adapter-owned list это не реализуют. `--no-session-persistence` Claude включён;
его managed handles не являются сохранёнными CLI/Desktop чатами. Нет cold resume,
arbitrary load, безопасного native cancel или shared Desktop writer. VK не начат.


## Сохранение состояния для продолжения — 2026-10-21

По поручению владельца сохранена [самодостаточная точка возврата](../plans/p4.13-resumption.ru.md):
реализованные механизмы и файлы, post-fix evidence, отсутствие commits/releases,
известные ограничения и открытые решения, NOT RUN матрица реальной приёмки,
требования Telegram native chat list/attach и порядок до VK. Статус P4.13 — PARTIAL;
кодовый CLI-подэтап завершён, live acceptance/desktop/existing chats не выполнены.
В заголовке исправлен устаревший остаток «Claude Code», противоречивший итогам ниже:
его managed CLI уже реализован, это не повод разрабатывать адаптер заново.

Очередь в конце road-map сохранена без перестановки. При возвращении сначала читать
план и проверять ветку/dirty diff, не терять untracked adapters/tests. Следующий
согласованный шаг — приёмка Codex/Claude CLI на native Windows и WSL с отдельной
test-конфигурацией; сохранить доступы/сервисы/production и не начинать модельные
вызовы только на основании документационного поручения. Scratch-логи и venv
временные; итоговые числа и границы проверок сохранены в репозитории.

Это documentation-only запись: runtime/tests не редактировались, приложение
повторно не тестировалось. Проверка сохранения файлов и навигации — в конце плана.

## Продуктовая прожарка 2026-10-22 — multi-source native chat

Владелец уточнил целевой сценарий: один исходный чат читается и управляется из
CLI/Desktop, Telegram и VK одновременно, без обязательной ручной передачи контроля.
Первоначальное решение о поочерёдном управлении и ожидании завершения turn отменено;
busy сам по себе не блокирует attach/отправку. Следовать native поведению агента,
не создавать скрытую gateway queue и не подменять исходный чат новым процессом.
Текущая таблица режимов выше исправлена; прежние dated implementation entries сохранены.

Подтверждённые границы профиля, явное resume без replay, permissions, preview,
подписки, Telegram/VK и минимальный VK scope, а также отменённые решения и
исследовательский gate записаны в [единой точке возврата, раздел 10](../plans/p4.13-resumption.ru.md#10-решения-продуктовой-прожарки-2026-10-22).
Найденные официальные Remote Control/Channels и App Server multi-client mechanisms —
DOCUMENTED кандидаты, не live evidence установленного Windows/WSL/Desktop.
Q2 о допустимости частичного выпуска остаётся нерешённым до capability matrix.

Это documentation/research-only уточнение. Код, тесты, статусы PARTIAL/OPEN, очередь
приёмка CLI → native list/attach → VK и исторические результаты 1985/42 WSL,
128 native не изменены. Новых model calls, установок, commits или deployment нет.

## Windows-коннектор и WSL agents — требование владельца 2026-10-22

Владелец сообщил о текущем и рабочем ПК: Windows 10 host connector, Codex/Claude
Code в WSL и ChatGPT/Claude Desktop на Windows. Подтверждено требование учитывать
смешанный режим наряду с native Linux, macOS и чистым Windows без WSL.

Исходники проверены: CodexAcpTransport.open/ClaudeCodeAcpTransport._spawn_owned
запускают CLI в окружении коннектора, CodexSettings/ClaudeCodeSettings не задают
WSL distribution/user. Штатного Windows → WSL bridge ещё нет. Прежние независимые
Windows/WSL проверки не являются mixed acceptance. Официальные OpenAI/Anthropic
документы подтверждают возможность Windows UI поверх WSL2 runtime в upstream,
но не доказывают установленную совместимость либо доступ Gateway к такому процессу.

Модель computer/environment/installation/profile/runtime/UI, целевая платформенная
матрица, ограничения auth/paths/lifecycle и mixed acceptance cases записаны в
[точке возврата, раздел 11](../plans/p4.13-resumption.ru.md#execution-environments).
Один Windows connector с local WSL runner — кандидат исследования, не принятая
реализация. Обычные Chat/Work/Cowork не включены молча в coding-agent контракт.

Документация уточнена, статусы и очередь не переставлены. Код/тесты/сервисы,
профили, default distro, credentials и установки не менялись; модельных вызовов нет.

<a id="safe-preflight-2026-10-22"></a>

## Безопасный preflight Windows/WSL — 2026-10-22

**Статус P4.13 остаётся PARTIAL.** Выполнена разрешённая владельцем диагностика текущего ПК без вмешательства в работающие задачи. Runtime-код не менялся; это не live/model/Telegram/Desktop acceptance и не реализация Windows-коннектора → WSL.

### Фактическое окружение

- Windows 10, build 19045; Ubuntu-22.04 на WSL2, Linux kernel 6.18.33.2-microsoft-standard-WSL2; явно выбран Linux user `user`.
- В WSL найдены и отдельно запущены `--version`: **Codex CLI 0.163.0**, **Claude Code 2.1.308**. Старое evidence для Claude 2.1.306 остаётся историческим.
- В Windows PATH `codex` и `claude` не найдены. Это не утверждение об отсутствии bundled CLI внутри Desktop и не требование устанавливать вторые CLI.
- Через read-only Appx inventory найдены пакеты **Claude 2.19675.1.0** и **OpenAI.Codex 26.1016.6573.0**. Отдельный ChatGPT пакет этим способом не обнаружен; это не доказательство, что ChatGPT нигде не установлен. GUI не открывался, его mode/runtime/history не исследовались. Имена пакетов не доказывают shared attach.
- Тестовые интерпретаторы: WSL project `.venv/bin/python` 3.12.10; native Windows isolated test environment 3.12.13. Системный WSL Python 3.10.12 не использовался для adapter tests/AgentClient initialize.

### Реальные безмодельные проверки

| Проверка | Codex | Claude Code | Что доказано |
| --- | --- | --- | --- |
| `AgentClient.connect()` внутри WSL, реальный executable, изолированные HOME/config/cwd | Initialize успешен; owned child закрыт, exit 0 | Initialize успешен; owned child закрыт, exit 143 после штатного close адаптера | Совместимость initialize текущих managed adapters с этими installed CLI; не выполнение user turn |
| Native Windows parent → `wsl.exe` → реальный Linux CLI, JSONL по stdin/stdout | Initialize/initialized успешны; после EOF exit 0 | Control initialize успешен; после EOF exit 0 | Сам путь stdio через Windows/WSL работоспособен при явных distro/user/cwd и чистом env |

Во второй проверке Windows parent непосредственно обменивался protocol frames с CLI, а не только запускал WSL pytest. Переданы только initialize/control frames; **0 model prompts, 0 user turns**, пользовательские чаты не открывались. Для Codex initialize отправлен UTF-8 title. Credentials не переносились; Linux процессы получили новый HOME/config и `env -i`. Оба wire-процесса завершились по EOF без targeted forced cleanup; исчезновение exact owned Linux PID проверено после запуска. Наличие зарегистрированного Windows connector, его config/setup/relay, потеря wrapper, restart/reconnect и approval lifecycle этим harness **не проверены**. First-class bridge в коде по-прежнему отсутствует.

### Выборочные автоматические проверки

Во всех прогонах выбран один и тот же набор: `test_codex_managed.py`, `test_codex_windows.py`, `test_claude_code_managed.py`, `test_codex_config.py`, `test_claude_code_config.py`. Fake peers работают в новых scratch-директориях, HOME/config/TMP изолированы, pytest cache/bytecode writes выключены. Сторонние pytest plugins не автозагружались, `pytest_asyncio.plugin` подключён явно. Это **не полный suite**.

- **Windows, последний полный прогон этого набора: 142 passed, 2 skipped.** Лог `windows-tests-0faa1b82cc.log`.
- Первый Windows прогон: **1 failed, 141 passed, 2 skipped**. В `test_manager_process_capacity_is_global_and_race_safe[True]` общий fake log содержал один PID вместо двух. Изолированный повтор — **1 passed**, затем полный выбранный набор прошёл. Причина пока не установлена; повторный green не считается исправлением нестабильности.
- **WSL, последний прогон: 1 failed, 139 passed, 4 skipped**, лог `wsl-tests-f7f1f6f4d1.log`. В `test_live_codex_doctor_uses_owned_runner_and_only_initializes` fake log содержит `['initialize']` вместо `['initialize', 'initialized']`. Не объявлять это исправленным или автоматически считать только проблемой теста; требуется отдельная локализация close/drain/журнала fake peer.
- Предыдущий выполненный WSL прогон: **2 failed, 138 passed, 4 skipped**, лог `wsl-tests-0fe802bfbe.log`: тот же doctor case и нестабильный Claude capacity case. Во всех повторных WSL test runs использован `--capture=sys`, см. ограничение ниже.
- Самый первый WSL запуск завершился internal error pytest до выполнения тестов: fd capture не смог выполнить `TemporaryFile.truncate()` в Windows-backed `/mnt/c` scratch.

**Отдельно воспроизведён дефект окружения, не приложения:** минимальный `tempfile_probe.py` в том же DrvFS scratch выполняет write/seek/truncate. Анонимный `TemporaryFile` даёт `FileNotFoundError`, errno 2; обычный именованный файл проходит. `--capture=sys` позволил выполнить pytest, но не доказывает работоспособность обычного fd capture/нативного Linux filesystem. Этот результат нельзя путать с доказательством причины оставшихся protocol-log failures. Не менялись глобальные TMPDIR/pytest settings, исходные тесты не ослаблялись и падающие cases не исключались.

Исторические full-suite результаты **1985 passed / 42 skipped WSL** и **128 passed native** от 2026-10-21 не переименованы в текущие. Новый focused WSL run не полностью green.

### Сохранность и границы

- Перед тестами зафиксированы SHA-256 всех tracked и nonignored untracked файлов репозитория. После всех probes и до документирования: **412 файлов до / 412 после, changed=[], added=[]**. Накопленная незакоммиченная работа сохранена; runtime/test исходники не менялись.
- Работавший до диагностики Claude PID 14530 в WSL присутствует после проверок. Наблюдавшиеся Windows `pythonw.exe` PID 14636, 23832, 7944, включая установку ACP Gateway, также присутствуют после проверок. Это проверка конкретных процессов, не заявление о непрерывной проверке здоровья всего ПК.
- Не выполнялись `wsl --shutdown`, termination distro, broad kill, restart/deploy/update/commit, GUI control, изменение default distro, рабочего connector config, login или перенос credentials. Закрывались только процессы, созданные probes/tests. Scratch retained для evidence; пользовательские данные не очищались.
- Не выполнялись authenticated model turns, Telegram round trip, native history listing/resume, attach к занятому CLI/Desktop, проверки другого рабочего ПК, bare Linux или macOS. Auth готовность рабочих профилей намеренно не проверялась через чтение credentials/смену аккаунта.

### Возврат к работе

1. Локализовать недетерминированный общий fake log и воспроизводимый WSL doctor close/initialized case, отдельно от DrvFS capture. Не скрывать failed evidence новым green run и не обходить проблему правками рабочих процессов/профилей.
2. В том же P4.13 продолжать explicit Windows→WSL launch/config/lifecycle для **новых managed чатов**. Успешный wire harness снижает неопределённость stdio, но не выбирает автоматически всю bridge архитектуру и не закрывает forced-loss/ownership handling.
3. Для model/Telegram acceptance отдельно согласовать тестовый проект/минимальные модельные расходы, использовать новые безопасные чаты; существующий пользовательский чат и Desktop затрагивать только в соответствующем согласованном подэтапе. Не требовать повторного login без фактической необходимости.

### Локальные evidence

Корень: `C:/Users/vladz/AppData/Local/hermes/cache/scratch/p413-safe-preflight/` (в WSL `/mnt/c/Users/vladz/AppData/Local/hermes/cache/scratch/p413-safe-preflight/`). Это временное хранилище: может автоматически очищаться. Долговечный итог — эта запись журнала.

- `inventory.py`, `windows-inventory.json`, `wsl-inventory.json`, соответствующие `*-after-inventory.json`;
- `run_tests.py`, `windows-tests.log` (первый failed run), логи повторов, `*-tests-result.json` (последний результат, не агрегат повторов);
- `real_initialize.py`, `real-wsl-initialize.json`;
- `windows_wsl_wire.py`, `windows-wsl-wire.json`, `wire-*/owned.json`;
- `tempfile_probe.py`, `tempfile-probe.json`;
- `repo-before.json`, `repo-pre-docs-verification.json`, `safety-verification.json`.

<a id="windows-wsl-managed-launch"></a>

## Windows→WSL managed launch — в работе 2026-10-22

По разрешению владельца начат один подэтап P4.13: конфигурация и owned lifecycle
новых managed Codex/Claude Code чатов из Windows-коннектора. Native history/resume,
shared Desktop attach, VK, deployment и модельные расходы не включены.

Добавлены optional `codex.wsl`/`claude_code.wsl`: явные distribution/user, Linux
python/executable/cwd, optional Linux HOME и backend config home, explicit PATH
добавления. Host filesystem не проверяет Linux пути; недопустимые/root/implicit
identity и Windows/relative/traversal paths отклоняются. Native defaults сохранены.
Адаптеры направляют WSL spawn/close в отдельный launcher, без native fallback;
неподтверждённый cleanup сохраняет uncertainty.

Промежуточное TDD evidence: первый schema case RED 1→GREEN 1; strict validation
RED 40→GREEN (85 с существующими backend config tests); adapter routing RED 2→GREEN 2;
optional HOME override RED 2→GREEN 44 config tests. Native Windows config/routing
87 passed получены до добавления optional HOME; итоговый integrated suite ещё не выполнен.

Архитектура в реализации: embedded stdlib Linux supervisor без установки Gateway
в Ubuntu, explicit user/cwd/env, новый owned process group, EOF/signal cleanup,
защита от PID reuse; supervisor/forced-loss evidence пока pending. UI/setup/doctor
и независимое ревью также в работе. Этот checkpoint не объявляет bridge готовым.
Реальный Windows AgentClient→WSL model-free probe подготовлен в scratch,
но пока не запускался на новой реализации. Рабочие коннекторы/профили не обновлялись.

### CLI wiring подэтапа — промежуточная проверка 2026-10-22

Проверены полученные wizard/doctor/config-check изменения: явные `codex-wsl` и
`claude_code-wsl`, Linux cwd без host lookup, сохранение home/agent_home/PATH/python,
снятие несовместимых backend settings и отсутствие переноса WSL settings на сервер.
Doctor выделяет WSL среду до native проверок и в live режиме использует только
существующий owned initialize. Родительский объединённый CLI/config/routing subset
в WSL: **132 passed**. Launcher/initialize в этих cases подменён тестовым peer/mock;
это не реальный mixed-runtime smoke. Supervisor и итоговая приёмка ещё pending.

### Полный результат первого WSL-ревью и текущий checkpoint

Получен полный `deleg_84c097ee`: verdict **passed:false**, S1 и L1–L3.
L1 (не-нормализованный RuntimeError) исправлен и покрыт adapter regression;
L2 (stall с exit 0/receipt) исправлен: A/B pre-fix exit 0 + receipt, fixed exit 126
без receipt. L3 (cwd мастер принимает traversal/colon/nonprintable и падает при
финальной валидации) исправлен общим `_wsl_path`: RED 3 → GREEN, объединённый
CLI subset **70 passed**, Ruff check и diff check чисты.

**S1 ещё открыт:** перенос токена в окружение Python через `/usr/bin/env` не
убирает assignment из Windows argv `wsl.exe`. Это не считать закрытием receipt
integrity. Нужно убрать токен из argv всей цепочки и отдельно определить same-UID
границу; повторное независимое ревью `deleg_bf47c12a` ожидает доставки.

Полный WSL прогон **2111 passed,48 skipped** получен **до** L3, не окончательный
post-review verdict. Native Windows focused **306 passed,27 skipped** относится
к более раннему дереву; full Windows запуск не завершился до timeout 900 секунд,
его собственное pytest-дерево очищено, пользовательские процессы не трогались.
Native Windows unit run с лимитом 2400 секунд ещё выполняется и содержит failures;
не заявлять native full/unit green. Нужны разбор результатов, окончательные
регрессии после всех правок и принятое повторное ревью. Подэтап IN PROGRESS,
P4.13 PARTIAL; публикации/production/model/Telegram acceptance не было.

### Повторное ревью deleg_bf47c12a — не принято

Вердикт `passed:false`: S1 остаётся открыт как для Windows argv, так и для Linux
`/proc/<supervisor>/environ`; `os.environ.pop` не удаляет exec-time environment block.
L2 исправлен только для live-loop stall: final drain после native exit/EOF всё ещё
мог отбросить хвост и выдать clean receipt. Независимая synthetic репродукция:
800000 bytes → 655360 доставлено, rc 0, close_wsl True (3/3); EOF-ветка также
теряет данные. L1 normalization повторным ревью подтверждён; L3 parent исправил
после snapshot повторного review (CLI 70 passed).

Запущен отдельный fix-agent `deleg_776b5b12`: только launcher/supervisor и два
профильных test-файла, строгий RED→GREEN; scope — supervisor-generated BEGIN/CLEAN
handshake без токена в argv/env/payload, ограничение доступа к supervisor через
PR_SET_DUMPABLE и non-zero/no-receipt при неполном bounded drain. Это план правки,
не утверждение выполненного решения. После него нужны parent mixed smoke,
регрессии и независимая проверка; native/Telegram/model/production scope не расширен.

Native Windows **unit** прогон завершён: **25 failed,1372 passed,45 skipped,2 errors**
(1476 s), до окончательных правок. Это не принятая native regression; причины
не разобраны полностью и не объявлены автоматически baseline/platform-only.
Failed logs сохранены в scratch; полный WSL 2111/48 также остаётся историческим
до финальных правок. Подэтап IN PROGRESS, P4.13 PARTIAL.

### Read-only разбор native Windows unit failures

Отчёт `deleg_3e4c5b6c` сохранён в локальном scratch:
`p413-windows-triage/windows-unit-failures-triage.json`. Parent сверил все
25 FAILED + 2 ERROR node IDs с полным логом и пересчитал категории:
11 — platform assumptions в tests; 12 — harness/mocks/PATH/UNC/ACL (включая
оба setup errors); 1 — потенциальный Windows DB restore defect; 3 —
нерешённые maintenance IPC failures. Это классификация симптомов, не baseline
сравнение и не подтверждение отсутствия Windows-регрессий. Исторический
native Git read-only probe воспроизвёл UNC dubious-ownership exit128;
Git safe.directory и пользовательские конфигурации не менялись.

Важное ограничение безопасности: четыре connection-edit tests доходили
до настоящей логики чтения статуса Task Scheduler. Full native suite
**не повторять без предварительного deny-by-default isolation audit**:
временный HOME/cwd и systemd mocks не изолируют Windows task. По отчёту
не выполнялись tests/service controls и не было записей в repo. Следующие
безопасные направления — изолированные mock-boundary, SQLite handle и IPC
пробы; это не разрешение менять рабочие службы, ACL или updater.
S1/L2 fix-agent остаётся отдельной текущей работой, подэтап IN PROGRESS.

### S1/L2 fix2 получен, parent проверка продолжается

`deleg_776b5b12` изменил только launcher, supervisor и два профильных test-файла.
Parent сверил все четыре SHA-256 с отчётом. Теперь capability генерируется
в supervisor после PR_SET_DUMPABLE=0; BEGIN до Popen, CLEAN только после полного
drain, parent проверяет порядок и EOF. Токен не передаётся в argv/env/payload.
Final-drain loss/ошибки и partial receipt дают nonzero без clean verdict.
Это состояние реализации, не принятое security-review заключение.

Worker evidence: S1 6 RED → 52 GREEN; L2 2 RED → 54 GREEN, scoped Linux92,
native Windows60/1, L2 repeated3. Parent full WSL и безопасный WSL-focused
native subset запущены отдельно; пока результаты pending. Полный native unit
не повторяется без isolation audit, предыдущий 25failed/2errors не отменён.

Parent повторил реальный Windows AgentClient→explicit WSL initialize для обоих
установленных CLI: оба success, launcher_exit0, cleanup_uncertain0, prompts0,
пользовательские histories не открывались; доказательство — scratch
`p413-wsl-implementation/real-gateway-result.json`. Это не model/chat round trip.
Новый snapshot14/static scan0 передан в независимый `deleg_afaa46f7`.
Ruff check чист, format-check нашёл одну недостающую пустую строку в wizard.py;
устранение и окончательная проверка pending. Подэтап IN PROGRESS/P4.13 PARTIAL.

### 2026-10-22 — независимое ревью fix2 принято

`deleg_afaa46f7`: passed:true, security_concerns=[]/logic_errors=[], все14
snapshots совпали с source при review. Worker-reviewer дополнительно выполнил
Linux163 и Windows64/1; эти числа не складываются с parent suite. S1/L1/L2/L3
больше не воспроизводятся. Лимиты — не sandbox против root/hostile same-UID
signals, deliberate escaped groups исключены.

Parent полный WSL: **2148 passed,48 skipped** за348.99s. После него Ruff
добавил одну пустую строку в wizard.py; AST идентичен reviewed snapshot,
остальные13 source hashes совпадают. lint/format/diff зелёные. Для строгого
final-tree evidence повторён безопасный Windows subset: **135 passed,34 skipped**
за21.93s (`windows-tests-450b7e8e04.log`); новый полный WSL прогон ещё выполняется.
Initialize-only smoke обоих реальных CLI остаётся exit0/uncertainty0, prompts0.
Данные full native failure/triage не отменяются. Новые источники границы
реализованы и source review принят, P4.13 всё ещё PARTIAL.

### 2026-10-22 — final-tree checkpoint Windows→WSL завершён

Последний parent full WSL после всех source/format правок: **2148 passed,48 skipped**
за358.35s, exit0, `wsl-tests-92d8cc4fd2.log`. Parent сверил summary и manifest
всех265 Python source/test-файлов: изменений за прогон нет. Native безопасный
WSL subset после форматирования: **135 passed,34 skipped**, exit0. Реальные
installed Codex/Claude initialize через Windows AgentClient: оба success,
launcher exit0/cleanup_uncertain0, prompts0. Независимый `deleg_afaa46f7` принят;
единственное отличие wizard.py после snapshot — formatter blank line, AST тот же.

Source-checkout подэтап **новые managed Windows→explicit WSL чаты** завершён;
не parent P4.13 целиком. Сохранены old failures и failed-review/A-B логи.
Общий native unit остаётся непринятым (25failed/2errors), требует safe isolation
и разбора, не замаскирован passing subset. Native ID/catalog/resume/live Desktop,
model/account/Telegram round trip, другие ОС/ПК и release/deploy не выполнены.
Следующий пункт очереди без решения владельца не активируется.

### 2026-10-22 — новый подэтап: безопасный разбор native Windows failures

Владелец разрешил продолжить рекомендованным порядком: Windows test isolation
и разбор всех25 FAILED/2 ERROR до живой приёмки. Статус подэтапа **IN PROGRESS**,
родитель P4.13 PARTIAL. Полный native suite запрещён до проверки mock boundaries.
Первый acceptance gate — deny-by-default для настоящих Scheduler/service операций,
с регрессией, доказывающей блокировку до внешнего вызова, и безопасным isolated
probe. Далее классификация27 исходных node IDs, SQLite/IPC reproduction и
точечные исправления RED→GREEN без ослабления privacy/ownership checks.
Не менять рабочие службы, updater/tasks, ACL пользовательских файлов и профили;
никаких model/account/Telegram/deploy/commit действий. Исходный Windows→WSL
checkpoint сохраняется. Scratch: `p413-windows-remediation/` (before-manifest
и triage-input). Native общий green не заявляется до реальной проверки.

### 2026-10-22 — первый Windows isolation gate, parent проверка

`deleg_9b23e11b` вернул три результата. Изменены только tests/conftest.py и новые
external_effect_guard.py, test_external_effect_isolation.py, fakes/isolation_probe.py.
Parent проверил manifest: прочие существовавшие source/test файлы не изменены,
кроме уже собственных roadmap/journal записей. Guard блокирует внешние process,
service/Scheduler/updater и native-manager границы до collection/execution;
точечные opt-ins — fixed disposable helper и read-only ACL для fixture-owned paths.
Это accident guard, не OS sandbox; filesystem/registry/socket и hostile mutation
не входят в гарантию. Native filelock/ntdll намеренно fail closed до аудита.

Parent native Windows **53 passed** (3.74s, windows-tests-b70f3a187d.log),
WSL **53 passed** (0.55s, wsl-tests-392db42691.log): 42 selftests + 11
audited windows_acl tests. Lint/format/diff зелёные. Независимый `deleg_80d24a88`
проверяет guard и совместимость глобального hook; принятие пока pending.
Разрешён только проверенный scoped набор, не все27 original failures и не
полный native/WSL suite. Исторические2148/48 предшествуют изменению conftest.

SQLite: native scratch18 cases показали, что test_database_rollback... сохраняет
своё соединение после sqlite3 transaction context. При open handles замена
падает WinError5/32, при explicit close проходит DELETE/WAL; snapshot свои
backup handles закрывает. Минимальный следующий test fix — deterministic close
всех трёх blocks без skip/GC/retry, после безопасного audit/run. Production
restore не исправлялся: отдельно воспроизведён риск partial restore, если внешняя
connection остаётся открытой (code/entry меняются раньше DB). Обычный lifecycle
имеет Store.close/process-exit/lease, но эксклюзивность против внешних клиентов
и другого installation, использующего тот же DB, не доказана. Hardening требует
отдельной регрессии/design, не считать риск закрытым исправлением test handles.

IPC: controlled0.95s/check в трёх ACL checks на atomic publication воспроизвели
исходные409/timeout; timing evidence показало freshness preflight, а не отказ
controller, и успешную reservation без своевременного ACK. Реальный ACL без
injected latency прошёл; исходная историческая причина остаётся недоказанной.
Следующий test-harness fix — module-local deterministic ACL seam с сохранением
нативных ACL/security tests. Runtime hot-path/timestamp/response-loss hardening
не выполнен и не подменяется увеличением deadline. Отчёты sqlite/ и ipc/ сохранены.

Чеклист27 node IDs сохранён: 4 имеют diagnostic evidence, ни один ещё не объявлен
исправленным. Подэтап IN PROGRESS; production/service/model/Telegram untouched.

### 2026-10-22 — isolation gate: отклонённое ревью и исправление обоих дефектов

Независимое ревью `deleg_80d24a88` вернуло `passed:false` с двумя блокирующими
находками, и обе подтвердились фактами, а не только мнением:

1. **HIGH — guard ломал обычные зависимости.** `tests/external_effect_guard.py`
   запрещал любые `ctypes.dlopen/dlsym/call_function`. Установленный
   `filelock 4.0.12` грузит `ntdll` и вызывает `NtCreateFile`/`LockFileEx` при
   импорте, а production `maintenance_worker.lease()` вызывает
   `kernel32.LockFileEx` для обычной файловой блокировки. RED native:
   `test_installed_file_lock_dependency_works_under_the_guard` и
   `test_production_file_lease_on_fixture_owned_path_works` падали с
   `ctypes.dlsym` (45 passed). Дополнительно выяснилось, что запрет импорта
   pywin32 ломал **collection**: официальный `mcp` тянет `pywintypes` →
   `_win32sysloader` → `win32api`, поэтому `tests/unit/test_service.py` и
   `tests/unit/test_connector_maintenance.py` вообще не собирались (2 ERROR).
2. **MEDIUM — разрешённый helper нельзя было остановить по таймауту.** PID не
   регистрировался как owned capability, `os.kill` запрещён, поэтому
   `subprocess.run(timeout=10)` не был границей: RED WSL
   `test_approved_helper_stall_is_bounded_and_cleaned_up` падал на
   `os.kill` denial (46 passed), а `Popen.__exit__` ждал завершения ребёнка.

Исправление сделано по TDD (RED → GREEN), только в четырёх test-файлах:

- **FFI-allowlist вместо тотального запрета.** Разрешено *связывание* только
  тех символов, которые реально используют установленный `filelock 4.0.12` и
  `lease()`: `ntdll` — `NtCreateFile`, `RtlDosPathNameToNtPathName_U_WithStatus`,
  `RtlFreeUnicodeString`, `RtlNtStatusToDosError`; `kernel32` — `CloseHandle`,
  `GetFileInformationByHandle`, `GetProcessTimes`, `LockFileEx`, `OpenProcess`,
  `UnlockFileEx`. Вызов `ctypes.call_function` разрешён только для адресов этих
  точек входа; адреса вычисляются до установки audit hook. Любая другая
  библиотека/символ/адрес по-прежнему отказ (есть регрессии:
  `kernel32.CreateProcessW`, `ole32` остаются запрещены).
- **Owned child PID.** `fork_exec` записывает PID запущенного ребёнка в permit,
  `os.kill` разрешён только для этого PID; `os.killpg` и любой чужой PID
  остаются запрещены (`test_unowned_process_signal_stays_denied`).
- **pywin32.** `win32service*`, `win32com`, `pythoncom`, `comtypes`, `wmi`,
  `win32ts` остаются неимпортируемыми; `win32api`/`win32process` импортируются
  (нужны `mcp`), но при загрузке их process entry points (`CreateProcess`,
  `ShellExecute`, `WinExec`, `TerminateProcess`, `CreateRemoteThread` и др.)
  заменяются на отказ.
- **Probe.** Добавлен фиксированный режим `stall` для проверки bounded cleanup;
  argv остаётся строго шестиэлементным, режим — из закрытого набора, hash
  helper'а закреплён (новый SHA-256 в guard).

Доказательства (не полный suite): RED WSL 1 failed/46 passed, RED native
2 failed/45 passed; GREEN native **60 passed** (14.41 s,
`windows-tests-1907b9b706.log`), GREEN WSL **59 passed, 1 skipped** (11.12 s,
`wsl-tests-e671c07a60.log`); полная **collection** native — **2245 тестов,
0 ошибок** (было 2 ERROR), подмножество семи ранее падавших модулей — 87
собрано; `ruff check tests/`, `ruff format --check tests/`, `git diff --check`
зелёные (158 файлов отформатированы). Повторное независимое ревью
`deleg_2a8a4bee` запущено; до его вердикта gate не принят, полный native suite
по-прежнему не разрешён. Остаточные границы, которые нельзя выдавать за
sandbox: файловые записи/сокеты/реестр не перехватываются, разрешённый
`NtCreateFile` может открывать файлы, pywin32-атрибуты теоретически можно
переназначить, audit hook остаётся на процесс.

### 2026-10-22 — второе ревью: фиктивный ctypes-контроль и переход к opt-in gate

Ревью `deleg_2a8a4bee` тоже вернуло `passed:false`. Часть находок подтвердилась
собственной проверкой:

1. **`ctypes.call_function` не существует как событие аудита в CPython 3.12.**
   Проверено на native 3.12.13 и WSL 3.12.10: прямой вызов
   `ctypes.pythonapi.Py_GetVersion()` даёт только `ctypes.dlsym`, а вызов
   `CFUNCTYPE(c_char_p)(address)()` — **ни одного** события. Значит проверка
   адреса в `_lock_call` была мёртвым кодом, а утверждение «invocation разрешён
   только для аудированных адресов» — ложным. Ревьюер дополнительно показал,
   что через `WinDLL('kernel32')._handle` + `ctypes.string_at` можно разобрать
   таблицу экспорта и вызвать `CreateProcessW`. Это неустранимо для
   Python-уровня: **контроль существует только на связывании** (`dlopen`/`dlsym`),
   а не на вызове.
2. **Разоружение pywin32 обходится** прямым `ExtensionFileLoader` re-exec, и
   **fail-open**, если `win32api`/`win32process` импортированы до guard.
3. **Gate стоял на весь suite**, поэтому 39 модулей с легитимным `subprocess`
   не могли исполняться: «deny-by-default» делал набор тестов фактически
   неработоспособным, а не узким.

Что изменено в ответ (только test-инфраструктура):

- gate стал **opt-in**: `tests/conftest.py` импортирует и устанавливает guard
  только при `ACPGW_STRICT_TEST_ISOLATION=1`; обычный запуск suite сохраняет
  прежнее поведение;
- в strict-режиме `pytest_collection_modifyitems` **отказывает** любому модулю
  вне аудированного набора (`ACPGW_STRICT_TEST_MODULES`, по умолчанию
  `test_external_effect_isolation.py` + `test_windows_acl.py`) — проверено:
  запуск `tests/unit/test_connector_commands.py` в strict-режиме завершается
  exit 4 и сообщением «strict isolation refuses unaudited modules»;
- удалён мёртвый `_lock_call` и адресный allowlist, из deny-набора убрано
  несуществующее событие `ctypes.call_function`; allowlist остался только на
  связывание (filelock/lease);
- `install()` теперь явно fail-closed, если `win32api`/`win32process` или
  manager-модуль уже загружены до установки;
- docstring модуля переписан: прямо перечислены границы (binding-only,
  файловые/socket/registry записи не перехватываются, pywin32 можно
  перезагрузить, hook остаётся в процессе), запрещено выдавать gate за sandbox.

Проверки после реформинга: strict native **59 passed** (14.13 s,
`strict-native-final.log`), strict WSL **58 passed, 1 skipped** (10.95 s,
`strict-wsl-final.log`), `ruff check tests/`, `ruff format --check tests/`
(158 файлов) и `git diff --check` зелёные. Независимое ревью уже opt-in версии
ещё **не проводилось** — gate не принят. Из 27 узлов по-прежнему не закрыт ни
один; полный suite не запускался и не разрешён.


## Windows isolation review и аудит 27 случаев — 2026-10-23

Владелец разрешил завершить текущий Windows-remediation подэтап P4.13.
Независимое ревью `deleg_97aa2873` отклонило opt-in gate: **S1** — подменяемый
allowlist/suffix matching, **S2** — отказ только после импорта тестов, **S3** —
prebound ctypes сохраняют capability до установки guard, **S4** — неперекрытые
обычные pywin32 process-control exports, **L1** — copied ContextVar сохраняет
истёкший fixture scope. Известные обходы через чтение памяти/ExtensionFileLoader
не использовались как новые findings. Ни одна опасная native-функция не вызывалась.

Reviewer повторил только прежние два аудированных модуля: Windows **59 passed**,
WSL **58 passed / 1 skipped**; это не отменяет findings и не даёт разрешения
на broad suite. Хеши пяти исходных review-файлов сохранены и совпали до/после.
Полный verdict: scratch `p413-resume-20261023/isolation-review.json`.

Второй read-only аудит механически сверил **27/27** уникальных node IDs с
исходным checklist: **25 FAILED / 2 ERROR**, запусков и закрытых случаев нет.
Разделены UNC Git trust, POSIX assumptions, Linux Docker/bootstrap, quoting,
загрязнение ACL/discovery mocks, отсутствующие native Scheduler mocks, тестовые
SQLite handles и IPC latency. Последние два не подменяются общим диагнозом:
SQLite test-owned handles не доказывают production leak; ACL latency — достаточный
воспроизведённый механизм, но историческая причина исходного IPC-падения не доказана.
Отдельные незакрытые риски — partial restore с external SQLite handle и native
ACL preflight legacy setup. Полный аудит: scratch `failure-audit.json` рядом с verdict.

На исправление **только пяти findings gate** назначен отдельный исполнитель
`deleg_3bafb4ec` с безопасным RED→GREEN; результаты ещё не получены. Затем нужны
родительская проверка final snapshot и независимое повторное ревью. До этого
исходные 27 проблемных тестов и broad suites не запускать. P4.13 **PARTIAL**,
Windows-remediation **IN PROGRESS**; live model/Telegram/Desktop/VK и production
в текущую работу не входят. Очередь не переставлена; commits/releases не выполнялись.


## Parent-проверка исправлений isolation gate — 2026-10-23

Исполнитель `deleg_3bafb4ec` исправил S1/S2/S3/S4/L1 в пяти test-only файлах:
`tests/conftest.py`, `tests/external_effect_guard.py`, `tests/fakes/isolation_probe.py`,
`tests/strict_isolation_runner.py`, `tests/test_external_effect_isolation.py`.
Канонический allowlist нельзя расширить окружением; direct/recursive collection
отсекается до импорта неаудированных тестов. Контролируемый запуск
`python -I -S -B tests/strict_isolation_runner.py` ставит guard до pytest/plugins,
отказывается от preloaded ctypes и поддерживает только аудированный CPython 3.12.
Сохранены необходимые filelock/MCP bindings; win32job импортируется для MCP,
но его callable API разоружён. Shared active token отзывает copied scope/permit.

Родитель после чтения полного handoff и проверки source hashes повторил только
два аудированных модуля: **native Windows 91 passed**, exit 0, 25.27s;
**WSL 67 passed / 24 skipped**, exit 0, 15.48s. Все 24 skip относятся к pywin32,
не к отключению ACL/ownership. Пять исходных хешей совпали с handoff после обоих
прогонов; Ruff check/format и git diff check зелёные. Production source и
`tests/unit/test_windows_acl.py` этим исправлением не менялись.

Повторное независимое ревью `deleg_ca2119a9` **PENDING**, поэтому gate ещё не
принят. Исходные 27 Windows cases не запускались и не закрыты; full suite,
model/Telegram, native IDs/Desktop/VK, commit/release/deploy не выполнялись.
Это accident guard, не sandbox; filesystem/socket/registry/account effects
по-прежнему требуют аудита конкретных тестов. Логи и RED/GREEN:
scratch `p413-resume-20261023/{guard-fix-report.json,parent-native-guard.json,parent-wsl-guard.log}`.


## Isolation gate: повторное ревью и второй цикл — 2026-10-23

Независимое повторное ревью `deleg_ca2119a9` отклонено. Прежние S1/S3/S4/L1
закрыты в проверенном scope; S2 не закрыт: pytest импортирует новый
`tests/unit/conftest.py` на пути к разрешённому ACL-модулю до collection filter.
Новый R1: config `addopts=-p marker_plugin` расширяет набор плагинов вопреки
контракту runner. Оба counterexample воспроизведены только безвредными import
markers в synthetic tree. В реальном дереве таких новых support/plugin файлов
не обнаружено; это не утверждение о фактическом запуске чужого кода на машине.

Узкие Windows91 / WSL67+24skip повторены reviewer, но не отменяют findings.
Полный verdict и canary evidence: scratch `p413-resume-20261023/isolation-rereview.json`,
`rereview-independent/results.json`. На точечный второй автоматический цикл
назначен `deleg_f859b010`: support-import chain до pytest, config addopts и
conftest plugin confinement, RED→GREEN безвредными markers. До завершения и
принятия повторного ревью исходные 27 cases/full suite остаются заблокированы;
ни один из27 не закрыт. При неудаче второго цикла требуется явная эскалация,
не третий архитектурный workaround. Статусы и очередь не меняются.


## Isolation gate: второй цикл реализован, final review pending — 2026-10-23

S2-ancestor-conftest/R1-config-plugin исправлены: runner до pytest проверяет
полную support ancestor chain разрешённых модулей; неизвестные conftest и
package initializers запрещены, canonical `tests/conftest.py` hash-pinned.
Изменение conftest, включая pytest_plugins, требует нового review/pin.
Config addopts перекрыт фиксированными reviewed flags до расширения plugin options.
Добавлены 24 harmless marker cases (четыре вида × две позиции × три выбора);
против прежнего runner получены 24 RED, после исправления все GREEN.

Родитель после проверки final_hashes повторил два аудированных модуля:
**Windows 115 passed**, exit0,37.82s; **WSL 91 passed /24 skipped**, exit0,16.84s.
Все24 skips прежние pywin32-only. Source hashes совпали после обоих прогонов.
Production и canonical conftest не менялись. Полный report и parent logs:
scratch `p413-resume-20261023/{guard-fix-cycle2-report.json,parent-native-guard-cycle2.json,parent-wsl-guard-cycle2.log}`.

Final independent review `deleg_310a94e4` **PENDING**. Gate пока не принят;
ни один historical27 case не запускался/не закрыт, broad suite не выполнялся.
При новом blocker после второго цикла — эскалация, не бесконечное расширение guard.
P4.13 PARTIAL; текущий Windows-remediation IN PROGRESS, остальные границы неизменны.


## Isolation gate принят; первая группа historical27 — 2026-10-23

Final independent review `deleg_310a94e4`: **PASS**, security_concerns и
logic_errors пусты. Все прежние S1/S2/S3/S4/L1 и R1 закрыты в явно указанном
scope; parent проверил совпадение всех восьми reviewed source/support hashes.
Reviewer повторил Windows **115 passed**, WSL **91 passed /24 skipped** и
**40/40** независимых harmless canaries без import markers. Ранее выполненные
parent115/91+24skip остаются последними parent результатами того же snapshot.
Verdict: scratch `p413-resume-20261023/isolation-cycle2-review.json`.

Принят только accident guard для двух аудированных модулей; это не утверждение
безопасности full suite. Добавление других exact module paths требует аудита
их imports/fixtures/эффектов и повторного review изменённого boundary.
Неблокирующее предложение — сохранить default/above-confcutdir canaries в helper;
сейчас их отдельно выполнил reviewer, новый helper rewrite не активирован.

Следующая активная группа в том же P4.13 Windows-remediation:
**win-f13 (SQLite)** и **win-f04/f05/f06 (IPC)**. `deleg_070dd888` назначен на
воспроизведение, точечные RED→GREEN и аудит exact module admission. Ни один из27
пока не закрыт. Сохранены отдельные риски external SQLite handle/partial restore
и недоказанная историческая причинность IPC latency. Остальные случаи в checklist
не потеряны, broad execution не разрешён. Live/production/commit/release неизменны.


## Первая группа SQLite/IPC: parent verification, review pending — 2026-10-23

`deleg_070dd888` закончил частичную работу по win-f13/win-f04/f05/f06.
win-f13: точный native WinError5 воспроизведён; три тестовых sqlite connections
обёрнуты в closing с сохранением транзакций/исходных assertions. Дополнительные
DELETE/WAL cases проверяют успешный restore и отдельно документируют существующий
partial-restore риск с external open handle; последний runtime-дефект не исправлен.

Исходные три IPC signatures воспроизведены успешным, но медленным ACL double;
историческая причинность по-прежнему неизвестна. Behavior harness изолирован
module-local клонами atomic/helper globals, native ACL policy это не доказывает.
Отдельный runtime-дефект: ошибка RESPONSE.replace после выполненного admission
навсегда теряла ACK. Теперь watcher хранит вычисленный ответ и повторяет только
publication, без повторного apply/нового token. Реальный atomic replace seam
прошёл RED→GREEN. Ни freshness5s, ни ACL policy/client budgets не ослаблены.

**Остаток:** slow ACL publication всё ещё старит heartbeat и даёт timeout/late ACK
уже принятой операции. Cancellation во время to_thread может завершить запись
после отмены; harness drain не является production repair. Не закрывать эти риски
зелёным fast-double тестом. Исследование минимального ACL/scheduling дизайна
назначено read-only, реализации или изменения policy этим не разрешено.

Parent после полного чтения изменённых пяти файлов повторил ровно пять
аудированных модулей (три новых+guard+ACL): native **147 passed**,exit0,106.03s;
WSL **123 passed /24 прежних Windows-only skips**,exit0,23.22s. Все12 source/support
hashes совпали с final child snapshot после обоих прогонов; Ruff lint/format/diff
зелёные. Эти totals пересекаются со старым guard subset: не складывать их.

Независимое review исходников и расширения exact audited set, плюс read-only
IPC design исследование: `deleg_ebbbd113`, **PENDING**. Ни один historical27
окончательно не закрыт; win-f13 ждёт review, три IPC остаются PARTIAL с указанным
runtime остатком. Остальные23 пока не активированы. Полный checkpoint/logs:
scratch `p413-resume-20261023/{first-four-remediation.json,parent-first-four.json,parent-first-four-wsl.log}`.
Production/services/accounts/models/Telegram/nativeID/Desktop/VK/commits не менялись.


## Решение владельца по ACL-backend — 2026-10-23

Владелец разрешил вариант **A**: общий read-only Windows ACL-backend на уже
установленном `pywin32` вместо запуска PowerShell на каждую проверку.
Условия решения:

- policy проверки не меняется: owner current SID/SYSTEM/Administrators, OWNER
  RIGHTS только при доверенном owner, отказ при absent/NULL DACL, запрет Allow
  иных SID для files и mask 852310 для directories; reparse отказ;
- freshness 5s, клиентские бюджеты, instance/request identity, once-only и
  uncertainty/no replay не ослабляются; увеличивать timeout запрещено;
- обязательны отдельный security-parity corpus против текущего SCRIPT-поведения
  и живой замер на Windows (нулевое число PowerShell-стартов, отсутствие
  deadline-отказов, возраст status в пределах freshness);
- один и тот же проверщик используется другими callers, поэтому проверять их
  поведение, а не только IPC;
- hard timeout: нельзя заменить killable PowerShell на неограниченный
  in-process native call; если ограниченное время отклика не подтверждается,
  допустим bounded native verifier child с фиксированным entry point;
- scheduler/heartbeat/protocol redesign, durable exactly-once и поддержка
  произвольных remote filesystems в этот patch не входят.

Исследование с обоснованием и acceptance-матрицей: scratch
`p413-resume-20261023/ipc-latency-design.json`. Начало реализации — только после
завершения текущего ACK-1 исправления, чтобы не смешивать незакоммиченные
изменения одного и того же IPC-пути. Три IPC-случая остаются PARTIAL до
прохождения parity/замеров и независимого ревью.


## Остановка на Windows-remediation — 2026-10-23

Работа приостановлена владельцем. Принято: isolation gate (Windows115,
WSL91+24skip, независимый PASS) и расширение аудированного набора. Закрыт по
факту только тестовый lifetime SQLite (win-f13). Три IPC-случая остаются
PARTIAL: поведение изолировано, отдельный дефект потери ACK исправлен, но
ревью нашло ACK-1 (кэш ответа не связан с аргументами и исходным `expires`);
повторный фикс остановлен до записи, хеши совпадают с проверенным снимком.
Медленная ACL-публикация, поздний ACK после timeout, partial restore с внешним
handle и отменяемость `to_thread` не исправлены. Владелец разрешил вариант A —
общий read-only ACL-backend на pywin32 при неизменной policy/freshness/бюджетах.
Полный остаток, порядок продолжения и evidence —
[раздел 16 точки возврата](../plans/p4.13-resumption.ru.md#resume-2026-10-23).
Ни один из 27 случаев не объявлен полностью закрытым; очередь не переставлена.

## Решение владельца об объёме проверок — 2026-10-23

Внешний обзор хода P4.13 (Claude Code) показал: 22–23.10 почти всё время ушло на
isolation guard (около 1500 строк, четыре цикла ревью), повторные прогоны и
ревью. Из 27 Windows-падений закрыто одно, частично — три; живая приёмка не
началась. Владелец согласился и ответил на вопросы обзора:

- ревью — по риску, одно и только для production-кода безопасности/IPC/ACK/ACL;
- 23 оставшихся падения сначала сравнить с `main`: общие с `main` — долг, не
  блокер; новые — регрессии P4.13;
- разрешены WIP-коммиты и `push` ветки `feature/p4.13-codex-managed`
  (без merge/релиза);
- полный Windows-набор разрешено запускать локально на этом ПК, обязательный
  аудит каждого модуля отменён.

Порядок шагов — [раздел 0 точки возврата](../plans/p4.13-resumption.ru.md#owner-directive-2026-10-23),
постоянные правила работы — [`.hermes.md`](../../.hermes.md). Код не менялся.

## Рабочий режим удалённой разработки P4.13.1 — решение 2026-10-23

Вопрос обзора: managed Codex/Claude в текущих настройках неудобны для удалённой
разработки. Claude спрашивает про каждое чтение, лимит 16 шагов, `--safe-mode`
отключает `CLAUDE.md`/MCP/skills, чат не сохраняется. Codex только читает, без
сети. У обоих нет отмены и восстановления. Можно ли это убрать?

Ответ владельца: да. Два режима для каждого managed-агента — «Безопасный»
(текущий) и «Рабочий». В рабочем включён native auto mode: Claude
`--permission-mode auto`, Codex `approvalsReviewer: auto_review` с
`workspaceWrite`/`on-request`. Цель — полноценная удалённая разработка с ПК с
коннектором любым агентом. Подэтап идёт до живой приёмки; очередь пересобрана.

Режим — свойство профиля (два профиля на агента), а не новая команда Telegram.
Используются уже существующие `/mode`, `session/cancel` и `session/load` ядра.
Флаги проверены по установленным Claude Code 2.1.309 и Codex 0.163.0. Код не
менялся. Владелец уточнил: auto mode Claude доступен на любой подписке. План,
стыковка со списком native-чатов и принятые риски —
[`p4.13-work-mode.ru.md`](../plans/p4.13-work-mode.ru.md).

## Решения владельца по приёмке и очереди — 2026-10-23

После ревью плана владелец принял все рекомендации и уточнил порядок проверки:

- **Без отдельных стендов.** Других пользователей у личного диспетчера нет:
  новые версии проверяются сразу на нём и на ПК владельца, затем полный релиз
  и раскатка штатным обновлением.
- **Коннектор основного Windows-ПК — из checkout**, а не из uv-релиза. Путь уже
  поддержан (`scripts/install.ps1`/`update.ps1`), новой разработки не нужно.
  Остальные ПК обновляются релизами.
- **Приёмка P4.13 объединена с остаточной P4.17/P4.15.1.** Диспетчер
  обновляется до rc из ветки, потому что 0.2.2 не понимает `kind:
  codex`/`claude_code`.
- **Q2 прожарки — да.** Подключение по ID разделено на 4a (список и продолжение
  закрытых чатов, выпускается сразу) и 4b (живое общее подключение к открытому
  чату через механизмы вендоров: исследование, затем решение).
- **P4.9 Goose — CONDITIONAL; P2.7 VK — DECISION** (минимальный VK или сначала
  P4.10) после приёмки P4.13.
- **Уборка документации** — отдельный пункт очереди для Hermes.

Порядок — [очередь road-map](../../road-map.md#9-очередь-дальнейшей-разработки),
заход приёмки — [план P4.13.1, раздел 6](../plans/p4.13-work-mode.ru.md).


## Продолжение по итогам обзора — 2026-10-23

Владелец поручил записать согласованные уточнения и продолжить текущий P4.13.
Восстановление выделено в проверяемый подэтап P4.13.1; будущий выбор native-чата
явно показывает рабочий режим. Старый дефект main влияет на выпуск, если реально
ломает целевую связку или ведёт к потере данных; прочий старый долг не блокирует.
Актуализированы краткие сводки очереди и разрешения Windows-прогонов.
Начат шаг 1 раздела 0: `rtk proxy uv run --frozen ruff check` — All checks passed;
`rtk proxy uv run --frozen ruff format --check` — 372 files already formatted.
Далее — проверка состава индекса, WIP-коммит/push ветки и отдельный ACK-1.
Isolation guard заморожен; live/model/release/deploy не начинались.


## ACK-1 — 2026-10-23, review pending
Pending ACK привязан к operation, эффективным аргументам (с различением JSON-типов) и исходному expires; ID остаются tombstone до остановки watcher, повторного apply нет.
RED: `rtk proxy uv run --frozen pytest tests/unit/test_connector_maintenance_ack.py -k exact_effective_identity -q` — 7 failed (изменённый запрос получал старый ACK); GREEN — 7 passed.
RED: тот же модуль, `-k "original_expiry or tombstone" -q` — 2 failed / 1 passed (повторный apply после другого ID); GREEN — 3 passed. `-k json_argument_types -q` — 1 failed → 1 passed.
Один scoped прогон на платформу: цели `tests/unit/test_connector_maintenance.py tests/unit/test_connector_maintenance_ack.py -q`; отдельные платформенные basetemp, WSL temp/HOME на Linux FS.
WSL: `rtk proxy uv run --frozen pytest` с этими целями — 35 passed, 4.14s; Windows Python 3.12.13: `rtk proxy dist/rc8-verification/windows-venv/Scripts/python.exe -m pytest` с теми же целями и PYTHONPATH на текущий src — 35 passed, 23.07s.
`rtk proxy uv run --frozen ruff check src/acp_gateway/connectors/maintenance.py tests/unit/test_connector_maintenance_ack.py` — All checks passed; `ruff format --check` тех же файлов — 2 files already formatted; `git diff --check` — успешно.
ACL/freshness 5s/client budgets не менялись. Slow ACL/late ACK, cancellation in to_thread, external SQLite handle и прежний REQUEST replace race остаются вне этого исправления; tombstone только в памяти watcher.
Статус: review pending — одно независимое ревью родителем; без commit/push. Isolation guard не менялся; ACL-backend, baseline, work-mode, live и release не запускались.


## ACK-1 принят — 2026-10-23
Независимое security/logic ревью: PASS, блокирующих замечаний нет; source diff после ревью не менялся.
Результаты исполнителя приняты без дублирования: WSL 35 passed, Windows 35 passed; lint/format/diff-check успешны.
Исправление закрывает подмену cached ACK изменённым запросом и повторный apply обработанного ID; durable exactly-once не заявляется.
Неблокирующий тест retry отказа 409 оставлен улучшением, не условием закрытия ACK-1.
Изменения сохраняются отдельным коммитом в рабочей ветке; следующий шаг — ACL-backend A, без baseline/work-mode/live/release.


## ACL-backend A — 2026-10-23, review pending
Read-only pywin32 verifier: фиксированный standalone child `sys.executable -I windows_acl.py`, JSON stdin, прежний timeout 10s/kill/reap; frozen entry сохраняет bounded spawn с join/terminate/kill. Policy, freshness 5s, client budgets и ACK-1 не изменены; pywin32 объявлен прямой Windows-зависимостью.
RED: `rtk proxy uv run --frozen pytest tests/unit/test_windows_acl.py::test_fixed_native_child_checks_batched_paths_without_shell -q` — 1 failed (PowerShell); GREEN — 1 passed. Generic spawn дал 3 native IPC failures (status age 7.153s/409/deadline); fixed entry — 3 passed без ослабления checks.
Parity: точная копия прежнего SCRIPT в tests-only oracle проверена против HEAD; 64 реальных descriptor-case (32×file/directory) + 4 missing/junction comparisons, 0 различий; owner/absent-DACL/error branches дополнены synthetic-тестами. Object ACE и inherit-only нормализуются как .NET, callback Allow проверяется по mask; неизвестные ACE fail closed.
Windows A/B, одна file+directory batch Python 3.12.13: PowerShell 1.139834s → fixed native child 0.424801s (−62.73%); отвергнутый generic-spawn prototype 0.711589s. Финальные 3 native maintenance IPC cases прошли, status age 1.902/1.955/2.022s < 5s, production PowerShell starts = 0; это evidence для win-f04/f05/f06, закрытие после review.
Цели: `T="tests/unit/test_windows_acl.py tests/unit/test_windows_acl_native.py tests/unit/test_connector_maintenance.py tests/unit/test_hermes_discovery.py tests/unit/test_windows_setup_paths.py tests/unit/test_credential_windows_boundary.py"`; WSL: `HOME=/tmp/p413-acl-home TMPDIR=/tmp rtk proxy uv run --frozen pytest $T -q -s --basetemp=/tmp/p413-acl-final-wsl -o cache_dir=/tmp/p413-acl-cache-wsl --durations=5` — 72 passed, 1 failed, 76 skipped, 4.72s.
Windows: `R=//wsl.localhost/Ubuntu-22.04/home/user/projects/acp-gateway; P=$R/dist/rc8-verification/windows-venv/Scripts/python.exe; PYTHONPATH="$R/src" TMP="$TMPDIR" TEMP="$TMPDIR" "C:/Program Files/rtk/rtk.exe" proxy "$P" -m pytest $(printf "$R/%s " $T) -q -s --basetemp="$TMPDIR/p413-acl-final-win" -o cache_dir="$TMPDIR/p413-acl-cache-win" --durations=5` — 144 passed, 1 failed, 4 skipped, 146.37s; TMPDIR — собственный Windows scratch, не system temp.
Единственный общий failure — старый тест считал весь tmp_path пустым вопреки autouse environment directories; assertion перенесён в собственный output/. Только `tests/unit/test_windows_setup_paths.py::test_atomic_acl_failure_writes_no_secret_and_keeps_original -q` повторён теми же launchers с отдельными repaired basetemp: Windows 1 passed (11.55s), WSL 1 passed (0.92s). Итог уникального scope: Windows 145 passed/4 skipped, WSL 73 passed/76 skipped; широкого повторного прогона нет.
`rtk proxy uv run --frozen ruff check` — All checks passed; `rtk proxy uv run --frozen ruff format --check` — 374 files already formatted; `rtk git diff --check` — успешно. Production/source после этих scoped проверок не менялся; isolation guard заморожен.
Статус: review pending, без commit/push; frozen binary startup/latency не проверены собранным артефактом, arbitrary remote FS и прежние cancellation/SQLite/REQUEST-replace риски вне шага. Baseline/work-mode/live/model/release/deploy не начинались.


## ACL-backend A — 2026-10-23, единственный review-fix, re-review pending
Первое ACL-ревью не принято: copied independent updater запускался base Python без pywin32 и отказывал на load_policy; прежняя запись review pending не означает PASS.
Только copied runtime явно выбирает updater_windows_acl.py как windows_acl.py: сохранён прежний bounded PowerShell verifier из ce580af, policy/timeout 10s/error-contract без изменений; application/IPC остаётся native pywin32 child без fallback.
RED Windows: `PYTHONPATH="$R/src" "C:/Program Files/rtk/rtk.exe" proxy "$R/dist/rc8-verification/windows-venv/Scripts/python.exe" -m pytest "$R/tests/unit/test_updater_acl_runtime.py::test_policy_load_under_isolated_base_interpreter" -q --basetemp="$TMPDIR/updater-acl-red"` — 1 failed, private owned policy отвергнут copied native helper.
GREEN/scoped Windows: тот же launcher, `tests/unit/test_updater_acl_runtime.py -q --basetemp="$TMPDIR/updater-acl-green"` — 3 passed, 9.60s; R — текущий checkout, TMPDIR — owned Windows scratch.
Probe запускает sys._base_executable -I, подтверждает отсутствие acp_gateway/win32api и игнорирование poisoned PYTHONPATH: реальный private file/directory принят, public ACL test-owned файла отвергнут. Timeout test fail-closed; copy_runtime/runtime_current consistency и stale detection покрыты.
WSL: `TMPDIR=/tmp rtk proxy uv run --frozen python -m pytest tests/unit/test_updater_acl_runtime.py -q --basetemp=/tmp/acpgw-updater-acl-review-fix` — 2 passed, 1 skipped, 0.09s; HOME/temp из autouse fixture на Linux FS.
`rtk proxy uv run --frozen ruff check src/acp_gateway/update_runner.py src/acp_gateway/updater_windows_acl.py tests/unit/test_updater_acl_runtime.py` — All checks passed; `ruff format --check` тех же путей — 3 files already formatted; `git diff --check` — успешно.
Без dependency bundling/global install/смены interpreter; worker main, update, services/Scheduler не запускались, guard и прочие пункты не менялись; ACL corpus/maintenance/A-B не повторялись. Возврат родителю на финальное re-review, без commit/push; новый blocker — эскалация владельцу.


## ACL-backend A принят — 2026-10-23
Финальное независимое ревью после одного fix-cycle: PASS, security/logic findings отсутствуют; production после проверок не менялся.
Основной application/IPC verifier — bounded pywin32 child; независимый copied updater явно сохраняет прежний stdlib/PowerShell support, без fallback в hot path.
Приняты 68 oracle comparisons без расхождений, Windows A/B 1.139834s → 0.424801s; win-f04/f05/f06 закрыты по реальным IPC cases с status age 1.902/1.955/2.022s <5s и 0 PowerShell starts в hot path.
Исходные scoped результаты: Windows 144 passed/1 failed/4 skipped, WSL 72 passed/1 failed/76 skipped; исправленный test-only assertion отдельно 1 passed на каждой OS, общего повторного green-run не было.
Fix updater: Windows 3 passed; WSL 2 passed/1 skipped; результаты приняты без дублирования, lint/format/diff-check успешны.
Frozen binary latency не проверялась; deferred SQLite/to_thread/REQUEST-replace риски сохранены.
Следующий шаг — baseline main/ветка на Windows и полный WSL ветки, без work-mode/live/release.


## Baseline P4.13 — 2026-10-23, INCOMPLETE по main
Main `ce771d6a80066b16fbb9b59eab8eb3071426249e` совпал с local main, origin/main и `git ls-remote`; ветка `c8a528664444dad8a451334b25214390c6ab430e`. Native Python обоих Windows venv 3.12.13, WSL locked Python 3.12.10.
Main — отдельный detached Windows worktree, свой `.venv`; native `uv sync --frozen --python "$Pb" --group dev` из main. `Pm=$M/.venv/Scripts/python.exe`, `Pb=$R/dist/rc8-verification/windows-venv/Scripts/python.exe`; M — этот main worktree, R — текущий checkout.
Windows main: `PYTHONPATH="$M/src" "C:/Program Files/rtk/rtk.exe" proxy "$Pm" -m pytest -q --tb=short --junitxml="$J" --basetemp="$B" -o cache_dir="$C"` — INCOMPLETE: после 69 progress-symbols (один F) >644s без прогресса, адресно остановлен только owned pytest PID; wrapper exit 4294967295, terminal 127, это НЕ pytest verdict, totals отсутствуют.
Windows ветка: `PYTHONPATH="$R/src;$S" "C:/Program Files/rtk/rtk.exe" proxy "$Pb" -m pytest -p p413_observe -q --tb=short -o faulthandler_timeout=120 --junitxml="$J" --basetemp="$B" -o cache_dir="$C"` — exit 1, **2052 passed / 131 failed / 3 errors / 223 skipped**, 1615.40s.
WSL ветка: `rtk proxy uv run --frozen pytest -p p413_observe -q --tb=short -o faulthandler_timeout=120 --junitxml="$J" --basetemp="$B" -o cache_dir="$C"` — exit 1, **2164 passed / 16 failed / 0 errors / 229 skipped**, 436.79s. HOME/TMPDIR/basetemp — Linux-native `/tmp/p413-baseline-20261023-wsl-branch`; Windows HOME/temp/cache — собственные scratch directories, strict mode выключен; J/B/C уникальны для каждой цели, S — scratch observer (только nodeid/outcome), без правок tests/guard.
XML и reports программно сверены: обе ветки по 2409 items; collection-only main — 1832 (exit 0, не повтор suite), 587 branch-only items и 10 main-only. Windows: 98 failing/error new-test IDs (97 UNC sys.executable validation в Claude/Codex, 1 `test_connector_maintenance_ack.py::test_response_publication_failure_retries_ack_without_reapplying[True]`, PermissionError); ещё 36 failing IDs существуют на main, но их main verdict неизвестен — долг/регрессии НЕ классифицированы окончательно.
Общее Windows/WSL падение ветки: `test_goose_probe.py::test_goose_probe_tls_requires_exact_pin_without_persisting` — assertion не учитывает autouse environment directories. Остальные 15 WSL failures — `test_windows_maintenance_startup.py`: waits[delayed,starting-instance], refuses[missing,exited,multiple,no-task-instance,changed-definition,foreign,invalid-instance,malformed,null,oversized,public], generation guards[values0,values1]; `Exec format error` Windows PowerShell, инфраструктура interop, не доказанные code regressions.
В Windows несколько connection/setup tests дошли до реального Scheduler **status** и получили ownership refusal; признаков фактической смены служб/задач не наблюдалось, управляющие команды вручную не вызывались. Native-maintenance fixture подменяет Scheduler fake-объектом; task-mutation opt-in не включался. Windows/WSL прогоны ветки частично пересекались, timing failures не изолированы.
В main worktree DrvFS даёт mode-only diff (0/0 numstat; `git -c core.filemode=false status --short` пуст); shared checkout до записи журнала чист. Full suite каждой цели запускался ровно один раз, без exclusions/новых xfail; production/tests не менялись, commit/push и последующие шаги не выполнялись. Baseline остаётся заблокирован неполным main, полный список долга и подтверждённых branch regressions не заявляется. После suite обнаружены и адресно завершены 4 owned process IDs (два тестовых serve и их interpreter children) из `test_daemon_cli.py::test_serve_command_binds_loopback_and_releases_lock`; повторная process-проверка пуста, рабочий коннектор не затрагивался.


## Точечное сравнение main — 2026-10-23, PARTIAL
Main `ce771d6`, native Windows Python 3.12.13 из собственного venv; source/tests не менялись. Из 36 общих failing IDs реально исполнены 32: **8 passed / 23 failed / 1 setup error**; ещё 4 остановлены до запуска по незаизолированному Scheduler seam, не pytest skips/failures.
Команда каждого batch: `PYTHONPATH="$M/src;$S" "$Pm" -m pytest -p p413_main_observe -vv --tb=short -o faulthandler_timeout=15 --junitxml="$J" --basetemp="$B" -o cache_dir="$C" <точные node IDs>`; S — только наблюдатель, внешние owned-process bounds 40–150s, без full-suite rerun.
Unit batch 18 IDs — **17 failed / 1 error**, exit 1; integration batches: handshake/probe/IPC **1 passed / 2 failed**, graceful-stop **4 passed**, connector-process **1 passed / 1 failed**, daemon CLI **2 passed / 3 failed**, exits 1/0/1/1. Все batch завершились сами, timeout verdict не подменял pytest failure.
Классификация всех 36: **21 старый долг**, **1 подтверждённая harness-регрессия** (`test_goose_probe...exact_pin_without_persisting`: 12 autouse environment directories против exact listing), **1 setup/infrastructure**, **7 branch-only кандидатов**, **2 masked/unknown**, **4 safety-blocked**. Кандидаты — graceful-stop[None,hold,permit,reconnect], connector-process[revoke], daemon CLI[approve-hi,multi-turn]; UNC startup/deadlines смешаны с code changes, происхождение не доказано.
Git setup: исходный `test_repository_files_are_clean` дал exit 128 из-за Linux gitdir в Windows worktree; только scoped `GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG_*` (safe.directory/core.filemode) — **1 passed**, pytest exit 0. Это не source fix и не обнаруженный secret; после scoped setup итог 32 IDs — **9 passed / 22 failed / 1 error**.
Masked: connector-process[sigterm] на main доходит до native termination (exit 1 вместо 0), ветка падает раньше registration; executors Goose на main даёт missing-PowerShell fixture error, ветка доходит до PATH assertion. Эти разные механизмы не объявлены одинаковым старым долгом.
Не запускались pairing-service-refresh, setup-sections[connection-edit,connector-base], wizard[connect-later False]: systemd mocks не перекрывают `windows_service._request`; `connection_service.py` после status может disable/start/менять flags. Реальные Scheduler/service управляющие команды не вызывались; два оставшихся owned fixture child завершены адресно, process readback пуст.
Main hang не воспроизведён: `test_hello_cannot_impersonate_another_computer` — **1 passed in 4.73s**, соседняя исходная последовательность collection[63..69] — **7 passed in 20.39s**, exits 0; bounds 45/65s. Исходного hang traceback нет; faulthandler/task dumps завершившихся медленных тестов не являются его trace. Full main остаётся INCOMPLETE; минимальный следующий repro при необходимости — bounded исходный prefix до index69 с observer, не full suite. Commit/push, production/tests/guard и рабочие установки не менялись.


## Test-only harness cleanup — 2026-10-23
Goose TLS probe создаёт cert/key в собственном `probe/` и по-прежнему проверяет точное содержимое каталога без фильтрации; production, frozen guards, `tests/conftest.py` и hash pins не менялись.
Native Windows setup: `UV_PROJECT_ENVIRONMENT="$V" uv sync --project "$R" --frozen --group dev --python "$Pbase"` — exit 0; V — отдельный owned Windows-native venv, Pbase — native Python 3.12.13, R — текущая ветка. Repo `.venv`, base/global dependencies не менялись; imports и дочерние Python используют `$R/src` через PYTHONPATH, sys.executable теперь локальный `.exe`, validation не ослаблена.
Программно выбраны ровно 107 canonical IDs: 98 новых failing/error IDs + 7 branch-only candidates + connector-process[sigterm] + Goose. Windows один прогон: `PYTHONPATH="$R/src;$S" "$V/Scripts/python.exe" -m pytest -p p413_main_observe -vv --tb=short -o faulthandler_timeout=15 --junitxml="$J" --basetemp="$B" -o cache_dir="$C" <107 exact IDs>` — **106 passed / 1 failed / 0 errors / 0 skipped**, 150.87s, pytest exit 1; внешний bound 600s не сработал, reports/JUnit/collection сверены.
Исход→результат: 3 setup errors→passed, 103 call failures→passed, 1 call failure→failed. Все 97 UNC fixture failures и все 7 candidates прошли с native interpreter; новый ACK PermissionError не воспроизвёлся (1 passed), исправленным не объявляется. Goose — 1 passed в этом же Windows scope, отдельного повтора не было.
Остался `tests/integration/test_connector_process.py::test_computer_cli_relays_prompt_to_shared_daemon_runtime[sigterm]`: registration и два turns завершились, line155 `assert process.wait(timeout=5) == 0` получил exit1, как main; прежний ранний registration symptom снят, наблюдается старый native termination contract, не новая source regression.
WSL после Windows: `HOME=/tmp/p413-goose-native-cleanup/home TMPDIR=/tmp/p413-goose-native-cleanup/temp rtk proxy uv run --frozen pytest tests/integration/test_goose_probe.py::test_goose_probe_tls_requires_exact_pin_without_persisting -q --tb=short --junitxml=/tmp/p413-goose-native-cleanup/junit.xml --basetemp=/tmp/p413-goose-native-cleanup/pytest-temp -o cache_dir=/tmp/p413-goose-native-cleanup/cache` — **1 passed in 1.72s**, exit 0; Linux-native temp/HOME, без параллельного Windows прогона.
`ruff check tests/integration/test_goose_probe.py` — All checks passed; `ruff format --check` — 1 file already formatted; `git diff --check` — успешно. Owned-process observer: дополнительных завершений children не потребовалось. Службы/Scheduler, live/model/account/deploy, main/full suites не запускались; commit/push не выполнялись.
За пределами шага остаются 21 старый Windows debt, executors Goose masked case, 4 safety-blocked IDs, 15 WSL interop failures и исходный main hang/INCOMPLETE; Git secret-check повтор не выполнялся. Полный all-green baseline не заявляется.


## Граница продолжения после targeted remediation — 2026-10-23
Native Windows venv снял UNC confound: 107 выбранных IDs дали 106 passed/1 failed; production не менялся.
Оставшийся sigterm failure совпал с main после успешных registration/turns; Goose regression закрыта test-only исправлением на Windows/WSL.
Исходные full-run failures не переписаны задним числом; ACK PermissionError не воспроизведён, не объявлен исправленным.
Full-main baseline INCOMPLETE; main hang не воспроизведён, четыре Scheduler IDs не запускались, executors masked и WSL interop остаются долгом/unknown.
Дальнейший выбор — принять targeted comparison как достаточный для P4.13.1 либо продолжить baseline; требуется решение владельца, не молчаливое закрытие шага.


## Решение владельца: сохранить и продолжить позже — 2026-10-23
Владелец согласился перейти к рабочему режиму P4.13.1, но прямо поручил сейчас только записать решение, разработку не начинать.
Targeted comparison признан достаточным: полный main baseline остаётся INCOMPLETE, старый Windows/interop долг и unknown cases не закрыты, но не блокируют следующий подэтап.
Не продолжать baseline-расследования автоматически; возвращаться по отдельному решению или при реальной помехе целевому сценарию/риске данным.
При явном возвращении — P4.13.1, шаг 1: профили safe/work и setup; затем адаптеры, остановка хода, сохранение/восстановление чата.
После реализации — согласовать бюджет и обновление целевой связки, проверить реальную задачу через Telegram, остановку и продолжение; затем релиз.
Список сохранённых native-чатов — далее, shared Desktop/Telegram attach — отдельное исследование; VK/multi-user не активированы.
Эта запись — только документация: код, тесты и рабочие установки не менялись; новые прогоны/ревью/модельные вызовы не запускались.

## P4.13.1: возобновление и профили — 2026-10-25
Владелец явно возобновил разработку по плану; пауза снята для исходников и целевых проверок.
Шаг 1: safe/work в конфиге и setup, предложение двух отдельных профилей; safe остаётся default.
Work defaults: timeout 3600, Claude max_turns 100, Codex workspace-write; явно заданные лимиты сохраняются.
RED: `rtk test uv run --frozen pytest -q tests/unit/test_managed_profiles.py` — 6 failed до реализации.
GREEN: тот же модуль плюс codex/claude config/cli и wsl config/cli — 165 passed в WSL.
Native Windows с локальным base Python и существующими venv dependencies — 161 passed / 4 failed: setup ACL helper с `-I` не видит зависимости venv; новые profile checks прошли. Общий green не заявляется.
Одно независимое ревью выявило замену явных default-valued лимитов; исправлено с регрессионным тестом. Ruff check/format clean.
Далее — native параметры адаптеров, cancel и persisted resume; установки, релизы и модельные вызовы не менялись.

## P4.13.1: параметры адаптеров — 2026-10-25
Шаг 2: Codex work использует on-request/auto_review и сеть в workspace-write, thread явно persistent.
Claude work: auto, default tools, user/project/local settings, hooks/MCP/skills; оставшиеся вопросы проходят once/deny, включая WebFetch. Safe-параметры сохранены.
RED: два новых adapter work tests — 2 failed; GREEN: codex_managed, claude_code_managed и wsl_adapters — 105 passed в WSL.
Windows тот же adapter scope плюс четыре setup-сценария — 102 passed / 5 failed / 2 skipped; падения на ACL helper при неполном pywin32 bootstrap временного окружения, не на параметрах work.
Одно независимое ревью: блокеров нет; Ruff check/format clean. Полный baseline не запускался.
Далее — cancel/load и локальная привязка сохранённых native ID; live model/Telegram и обновления установок не выполнялись.

## P4.13.1: отмена и scoped persisted resume — 2026-10-25
Шаг 3: ACP cancel/load для обоих адаптеров; Codex turn/interrupt + thread/resume, Claude interrupt ack + terminal result и --resume исходного UUID. Persistence включена и в safe.
Локальный SQLite bindings + OS lease связывают native ID с alias, mode, cwd, native home либо WSL distribution/user/profile; чужие ID и второй владелец отклоняются.
Attached/busy фиксируются до native resume/prompt. Только подтверждённый shutdown возвращает idle; потеря процесса/неподтверждённый cleanup и unknown prompt не разрешают повтор или resume.
RED: новый managed_sessions module отсутствовал; новые cancel/resume сценарии добавлены к fake peers. Итоговый WSL: affected adapter/client/binding modules — 162 passed; дополнительные interrupt ack/result negative cases — 3 passed.
Windows: affected adapter/relay/wiring и четыре setup-сценария — 122 passed / 2 POSIX skips; после review fix managed_sessions + wsl_adapters — 11 passed. pywin32 bootstrap временного venv исправлен; прежние environment failures не требуют production-правок.
Одно независимое ревью: найден unconditional lease release при unknown WSL cleanup; исправлено durable attached fence до native работы, regression tests для обоих адаптеров. Повторного review-цикла не было.
Ruff check/format и diff whitespace clean. История исходного Claude чата проверена после нового AgentClient; relay reconnect не повторяет prompt.
Это source/fake-peer evidence; authenticated model, Telegram, штатное обновление dispatcher/connector и полный baseline не запускались.

Windows negative interrupt: первый прогон 1 passed / 2 failed на startup deadline 150ms, до проверяемой отмены. Deadline перенесён после initialize; повтор только двух сценариев — 2 passed / 60 deselected. Production-код не менялся.
