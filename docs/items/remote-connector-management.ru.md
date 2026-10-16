# P4.15 — обновление диспетчера и коннекторов через Telegram

**Статус:** RELEASED в stable **0.2.1** — 2026-10-16.
Реализованы обе роли: versions/confirmation/durable operation/status/notifications,
локально включаемый независимый companion, uv managed update и Docker host image
replacement/SQLite backup/known-failure recovery. Публикация GitHub/PyPI/GHCR
выполнена по последующему поручению владельца; [evidence](../releases/0.2.1-publication.md).
Production установки не менялись. Актуальная настройка —
[companion guide](../setup/remote-updates.md). История первого этапа ниже сохраняется.

## Запрос владельца

Из dispatcher управлять подключёнными коннекторами, обновлять их удалённо и
выполнять базовые действия на компьютере, где они работают. Всё должно быть
доступно из Telegram-бота через понятные кнопки. С 2026-10-16 scope обновления включает сам dispatcher, в том числе Docker.
P4.16 модули остаются отложены и не являются prerequisite updater.

## P4.15 — удалённое обновление

- Показать computer/connector version, manager/source, capabilities и доступную
  версию. Выбрать конкретный компьютер и точную версию/канал обновления.
- Общий application service dispatcher, не логика только внутри Telegram handler.
  Typed control request с operation ID, authenticated actor, точным connector
  identity/generation и deadline; connector проверяет локальное разрешение.
- До обновления проверить совместимость, проверяемый источник пакета, права,
  свободное место и idle/drain. Не обрывать активные задания/ожидающие approvals.
- Updater живёт отдельно от обновляемого процесса: сначала durable intent/ACK,
  затем штатный package upgrade, restart, health check и reconnect с результатом.
  Сохранить YAML/env, enrollment key, data и service настройки.
- Состояния queued/preparing/updating/reconnecting/succeeded/failed/unknown;
  отсутствие соединения не равно успеху/ошибке. Повтор кнопки или reconnect не
  повторяет неизвестную установку; status/reconcile читают существующую операцию.
- Rollback к известной версии с явными границами совместимости схемы/данных;
  не обещать recovery, если потеряны права, сеть или updater.
- Один ПК, затем выбор нескольких с ограниченной параллельностью. Offline ПК
  не получать скрытое задание на будущее без отдельного решения владельца.

## P4.16 — подключаемые действия коннекторов

Уточнение владельца 2026-10-15: платформа не поставляет готовые функции
управления ПК. Владелец пишет и локально подключает собственные модули;
коннектор объявляет их возможности диспетчеру. Выбранный для обсуждения
контракт и этапы описаны в [предложении модульной системы](connector-action-modules.ru.md).
Следующий список сохранён как примеры пользовательских модулей, не built-in scope.

Кандидаты первого этапа: состояние ОС/uptime/connector/managed agent,
диагностика соединения, bounded/redacted logs, restart connector и принадлежащего
ему agent. Hermes Desktop — внешний процесс пользователя: владение и способ
его перезапуска требуют отдельного контракта; чужие процессы не останавливать.

Sleep/reboot/shutdown — возможный следующий набор, с явным target в подтверждении,
локальным opt-in, проверкой активных работ и различением accepted/completed/unknown.
Уснувший/выключенный ПК сам не сможет принять wake request: Wake-on-LAN возможен
только после отдельного выбора подходящего всегда включённого узла/сетевой схемы.
Произвольный remote shell, произвольные файлы и скрытое elevation не входят в
базовый scope. Исполнение доступных операций ограничено локальными capabilities.

## Telegram и авторизация

Панель компьютеров: имя, версия, online/offline, операции, текущий результат.
Кнопки привязаны к owner/chat/message, computer identity/generation и operation ID;
устаревшие/чужие кнопки отказывают. Подтверждение reboot/update показывает конкретный
ПК и действие. Отдельный аудит actor/target/version/result без секретов.

Сначала существующий single-owner deployment. До общего multi-user выпуска
нужны P4.10 workspace/connector scopes и сквозная application authorization.
Человеческие management-команды отделены от agent tool approvals: режим /mode auto
не даёт модели права обновлять пакет, перезагружать хост или выдавать новые scopes.

## Критерии приёмки

В изолированных Linux/Windows installations проверить install→remote update,
сохранность config/key/data/service, busy refusal, health failure/rollback,
повторные клики, foreign/stale targets, потерю ACK/соединения на каждом этапе,
read-only reconcile без replay, revoke и неподдерживаемые capabilities.
Затем владелец проверяет два физических ПК, Telegram UX и reboot/logon/network.

## Открытые решения перед реализацией

Stable/RC update policy и unattended opt-in, способ
обновления file/Git installations, privilege model/способ запуска независимого
updater и ownership внешнего Desktop. Эти вопросы не блокируют запись задачи.

## Порядок разработки — 2026-10-16

1. Read-only каталог версий/capabilities обеих ролей и Telegram панель обновлений;
   совместимость со старыми коннекторами, без включения неизвестных мутаций.
2. Durable operation service, явная локальная update policy, точная stable версия,
   подтверждение target/version, идемпотентный handoff независимому uv updater;
   статус после reconnect и запрет повторного запуска при unknown.
3. Docker dispatcher: отдельный updater на host/companion, immutable image target,
   compose replacement, health/rollback. Обновление uv внутри контейнера не подходит.
4. Объединить подтверждение/историю/результат обеих ролей в Telegram; проверять
   автоматически отказ busy/offline/stale/foreign/duplicate и lost ACK.

macOS проверяется владельцем позже; native macOS update не обещан. Релизы,
обновление реальных компьютеров и production Docker в этой сессии не выполняются.

## Реализация первого этапа — 2026-10-16

- `updates.UpdateInventory` — общий read-only application service; bounded paging
  и до четырёх параллельных запросов версий. Проверка PyPI только явной командой,
  с cache на 60 секунд; источник проверяется существующим stable resolver.
- Connector при handshake предлагает `X-ACP-Update-Inventory: 1`; dispatcher
  подтверждает поле Welcome только запросившему поддержку peer. Старые peers
  получают прежние frames; при неподтверждённой capability запрос не отправляется.
- Typed UpdateInfoRequest/Response проходят через один WS reader/writer;
  epoch, grant/revoke/replacement, timeout и bounded workers проверяются.
  Никаких arbitrary commands/paths/secrets в публичном InstallationReport нет.
- Telegram `/updates`, `/updates_check` и кнопка главного меню показывают dispatcher
  и компьютеры, установленную/доступную stable версию, offline/unsupported.
  Только существующие allowlisted private human chats имеют доступ.
- Capability `can_update` пока всегда false; inventory не разрешает mutations.
  Docker требует внешнего updater; package install внутри контейнера не выполняется.

Следующий кодовый этап: durable update operation и подтверждение target/version,
локальный opt-in, independent uv updater и status после reconnect. После него —
Docker host/companion updater и общий запуск обеих ролей из Telegram.

### Проверки первого этапа

2026-10-16: финальный source suite **1525 passed / 38 skipped**, 0 failures/errors,
`dist/remote-update-inventory-tests.xml`. Включены новые сценарии negotiated/legacy
peers, epoch rejection, grant revoke до/во время запроса, late response после
таймаута, отсутствие ACP при inventory, release lookup cache, Telegram allowlist,
offline/unsupported/temporary failure и отсутствие install mutation в протоколе.

P3.5 targeted regressions после исправлений — **83 passed**,
`dist/remote-update-focused-tests.xml`. Покрыты повторные ошибки записи activity
и успешный maintenance prepare после восстановления, предупреждение reinstall,
сохранение custom env, явные CLI/env overrides и отказ при повреждённой записи.
Полный suite включает эти сценарии и последние дополнения remote inventory.

Ruff check/format, `git diff --check`, secret guard изменённых файлов пройдены;
155 локальных Markdown file links без обрывов. Native Windows/macOS и живой
Telegram не запускались; приёмка предварительно закрыта решением владельца.
В песочнице asyncio/thread IPC проверки зависали; окончательные тесты выполнены
в разрешённом запуске вне песочницы. Production службы не изменялись.

### Точка продолжения

Первый inventory этап готов в исходниках. Следующая активная задача — persisted
update operation с точным target/version и человеческим подтверждением. Далее
independent uv updater для managed служб с reconnect/status, затем независимый
Docker host/companion updater с health/rollback и объединение запуска обеих ролей
в Telegram. Unknown outcome не разрешает повторную установку. P4.16 не начинать.

## Продолжение — confirmed operations и независимый companion

Две независимые базы: `update-control.db` на dispatcher хранит actor/target/version,
исходную версию, generation/epoch binding, expiry и состояние подтверждения; локальный
`update-spool/operations.db` — immutable intents, атомарный claim, результат и heartbeat
companion. Они не откатываются вместе с `gateway.db`. Gateway подтверждает intent до
remote await; ACK loss переводится в unknown. Status вызывает только read-only запрос.

Повтор operation ID сверяет actor/version/current_version; busy/denied сохраняются,
не становятся скрытой очередью позже. Старый connection epoch запрещён для execute,
но status после reconnect разрешён при прежнем computer grant generation. Actor
Telegram привязан к bot namespace и private chat; чужие/старые кнопки не запускают
обновление. Подтверждение одноразовое и истекает через 5 минут.

Отдельно согласуется update-operations capability. Inventory-only peers получают
can_update=false; неизвестные management frames не отправляются. Новый version RPC
не означает права установки. YAML `updates.enabled` по умолчанию false, companion
должен быть живым и соответствовать роли/target/backend. Policy Docker также проверяет,
что конкретная версия подготовлена на host до подтверждения и перед enqueue.

`acpgw updater --policy PATH --init --role connector|dispatcher` создаёт private native
uv policy без запуска/установки; `acpgw updater --policy PATH` копирует stdlib helper,
SQLite journal и lease helper вне uv environment и запускает base interpreter.
Companion имеет независимый жизненный цикл от обновляемого сервиса. Политика задаёт
локальные пути/команды, remote request их не содержит. Native Linux/Windows uv registry
путь использует существующий coordinator, exact version и tagged operation result.
Неизвестный/чужой result не считается успехом; claim после restart не исполняется вновь.

Docker host companion получает только локально разрешённые immutable image IDs/digests,
сохраняет старый image ID и SQLite snapshot после idle reservation. Пересоздаётся только
gateway service с прежними mounts/networks/config/env. На известной неудаче health:
stop new → restore SQLite → old image → health; timeout или recovery failure — unknown,
без конкурирующей автоматической установки/rollback. Docker socket в dispatcher не
добавляется. Образы должны быть заранее подготовлены; автоматическая сборка/публикация
артефактов этим MVP не выполняется. Старую unknown Docker operation разбирают локально.

`/updates` предлагает подготовку/подтверждение, `/update_history` и `/update_status ID`
показывают сохранённый исход после restart/reconnect. Фоновый status/reconcile отправляет
итог/unknown в исходный разрешённый private chat. Перед Bot API send сохраняется unknown
notification fence, после ACK — delivered; неоднозначная доставка автоматически не
дублируется и не повторяет installer. `/mode auto` не даёт management authority.

Границы: одна цель, single-owner, новые stable major.minor.patch. macOS, batch, RC,
Git/file installs и multi-user не включены. Native Windows/macOS и живой Docker-engine
update не объявляются проверенными. Остаточная приёмка по-прежнему предварительно
закрыта решением владельца; возвращаться по сообщениям о проблемах.

### Проверки confirmed operations/companion — 2026-10-16

Финальный полный source прогон: **1563 passed / 38 skipped**, 0 failures/errors,
`dist/remote-update-operations-tests.xml`. После него дополнительная проверка
mailbox validation и затронутых operation/worker сценариев: **35 passed**,
`dist/remote-update-mailbox-tests.xml` (включая четыре новые проверки идентификатора
перед host paths/commands). Последний targeted operation/UI/runner прогон: **19 passed**,
`dist/remote-update-final-tests.xml`; более широкий targeted прогон — 64 passed,
`dist/remote-update-operations-focused.xml`.

Проверены private actor/once-only confirmation, cancel/expiry/version/epoch changes,
lost ACK → unknown → read-only reconcile, local opt-in/heartbeat/version allowlist,
busy/denied dedup, protocol legacy negotiation и exact operation response identity.
Настоящий отдельный base-interpreter process выполняет fake installer и после
restart не воспроизводит claimed operation. Docker command fakes работают с
настоящей временной SQLite: WAL-compatible snapshot, schema/data restoration,
immutable old image recovery и timeout без конкурирующего rollback. CLI policy
provisioning создаёт private file, сохраняет config/env/result paths, не стартует
installer и не перезаписывает существующий policy. Уведомления проверены на
private chat, restart persistence и ambiguous send без автоматического дублирования.

Ruff check/format, `git diff --check`, secret guard и 163 локальных Markdown file
links пройдены. Живой Docker engine, native Windows/macOS и provider/Telegram
не запускались. Релизы, push, production package/service/container updates не
выполнялись. macOS остаётся на будущую проверку владельца; приёмка не возобновлена.

### Продолжение согласованного плана — 2026-10-16

Готовы в исходниках отдельные systemd user unit / Windows Task Scheduler logon task
для companion: `updater ... install|enable|start|stop|restart|disable|status|uninstall`.
Установленная копия stdlib helpers запускается base Python вне заменяемого uv
environment. Первая установка не включает автозапуск; setup/menu получили updater
раздел и отдельный opt-in settings step. Reinstall требует остановленного worker.
Остановка привязана к instance и ждёт завершения installer; timeout не вызывает
force-kill. Сигнальный обработчик не захватывает threading locks. Удаление autostart
сохраняет policy/runtime/journal.

`diagnose` показывает установку/health и сохранённые операции. Локальный `resolve`
для существующего executor unknown требует остановленного companion, явного
подтверждения завершения внешних installer/rollback, exact version и service health.
Outcome и audit сохраняются атомарно; установка/rollback не повторяются. Connector
health проверяется через локальный maintenance IPC. Потерянный dispatch до записи
executor и недоступный grant автоматически не разрешаются.

`allow-image` проверяет наличие заранее подготовленного immutable image на Docker
host и атомарно регистрирует version → image ID без ручного редактирования JSON.
Одна версия не переназначается молча. Pull/build/publish и автоматический approved
manifest delivery не включены. Policy изменения применяются после restart companion.

Проверки: полный source **1578 passed / 38 skipped**, 0 failures/errors,
`dist/updater-autostart-full-tests.xml`. После последних guard/setup изменений:
**42 passed**, `dist/updater-final-focused-tests.xml`; широкий targeted прогон —
**78 passed**, `dist/updater-lifecycle-tests.xml`. Отдельный base-interpreter process
проверен для foreground и установленной копии; сигнал завершает его штатно, heartbeat
очищается, claimed operation после restart не воспроизводится. Busy stop не обрывает
installer. Проверены immutable image registration и audited recovery/health refusal.
Windows Task Scheduler/systemd операции подменены: живого изменения служб нет.

Локально подготовлен development-кандидат **0.2.1.dev20261016** в
`dist/candidate-0.2.1.dev20261016/`: wheel, sdist, guide snapshot, source/artifact manifest
и SHA256SUMS. Метаданные и 97 packaged Python sources совпадают со snapshot.
`scripts/build_candidate.py` собирает offline из временной копии и не меняет версию
checkout. Это не новый stable/RC release и не версия для Telegram stable-updater.
Следующий внешний шаг — отдельное решение о версии/публикации и развёртывании.

Приёмка остаётся предварительно закрытой; macOS ждёт проверки владельца.
Batch/RC/Git/file и shared ownership не включены. P4.11/P4.16 остаются отложенными.
