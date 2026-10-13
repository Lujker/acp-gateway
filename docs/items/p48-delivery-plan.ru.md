# P4.8: поставка VPS-диспетчера и двух Windows/Hermes-коннекторов

Точка возвращения после перерыва: [P4.8 resume checkpoint](p48-resume.ru.md)
(состояние на 2026-10-13, после model transport/core/owner API и durable state).

Уточнение владельца от 2026-10-13. Целевая схема: dispatcher на Linux VPS,
Telegram как канал управления, home/work connectors в нативной Windows рядом
с уже работающим Hermes Desktop. Диалоги диспетчера отдельные в том же Hermes.
Goose и расширение платформенной матрицы вынесены в P4.9.

Дополнение владельца: Hermes — основной целевой агент, Telegram — текущий вход
в общее ядро. Активный logical dialogue пользователя сохраняется и меняется
только явной командой. Reconnect не должен вынуждать создавать новый диалог
при живых connector/Hermes и известном состоянии хода. Требования и предложения
по будущим аккаунтам: [постоянные диалоги и аккаунты](dispatcher-accounts-and-dialogues.ru.md).

Этот документ заменяет прежнюю смешанную очередь разработки/приёмки P4.8.
Все реальные установки и проверки перечислены только в разделе приёмки ниже.
Разработка не считается завершённой только на основании mocks; готовность к
передаче и готовность живого развёртывания фиксируются раздельно.

## Что уже реализовано

- Hermes discovery, identity/auth, observer, dedicated adapter и ACP relay.
- Разовые human approvals, отказ от unsafe cancel/adoption/resume/replay.
- Сохранение идентичности диалога и истории; состояния attached/busy/uncertain/
  restore-required/restore-unavailable. Новый managed connector сохраняет idle
  диалоги при потере только VPS-связи; после потери Hermes attachment нужен `/new`.
- Setup ролей server/connector, Telegram, enrollment, локальные Hermes profiles.
- CLI service lifecycle: Linux systemd user и Windows Task Scheduler.
- CLI version/update/status/rollback/uninstall для uv tool installations:
  snapshot, health check и откат при ошибке. Windows maintenance вынесен в helper.
- Preview wheel/sdist с locked dependencies; глобальная установка нового P4.8
  ещё не обеспечена новым опубликованным релизом.

Evidence: полная Linux-регрессия 937 passed / 17 skipped в исходниках и wheel;
native Windows профиль 221 passed / 3 POSIX skips, все 13 Scheduler cases прошли.
Настоящий Hermes 0.21.6 observer/selector прошёл без model calls. Это не приёмка
полного uv tool update, реальных approvals или маршрута VPS/два ПК/Telegram.
После selection guards, saved connector launch и idle Hermes session manager
текущие исходники прошли 967 passed / 17 skipped; новый wheel/native прогон
этих изменений ещё не выполнялся.
После добавления checkout scripts: **976 passed / 17 skipped**, Ruff чистый.
Это source regression; прежний wheel/native результат не обновлялся.
После Windows wizard paths/ACL: **988 passed / 17 skipped** в полной source
регрессии. Native Windows source профиль paths/ACL: **20 passed**, плюс
disposable ACL/write/read и fresh/repeat wizard smoke без подключений/tasks.
Новый wheel и его consolidated native acceptance ещё не выполнялись.
После Windows task setup/rollback: **1000 passed / 23 skipped** в source suite;
23 skips — 19 native opt-in и 4 installed-Hermes compatibility. Native source
Scheduler + wizard профиль: **28 passed**, все 19 Scheduler cases выполнены.
Это отдельная проверка исходников; предыдущий wheel/native отчёт остаётся историческим.
После maintenance admission: **1017 passed / 23 skipped**; профиль core/API/CLI
39 passed на Linux и **39 passed** в native Windows. Реальный изолированный
Linux CLI/daemon smoke прошёл prepare/status/resume/duplicate refusal/recovery.
Автоматический service update ещё не реализован; новый wheel не собирался.
После connector maintenance: **1030 passed / 23 skipped**; профильный Linux набор
38 passed, native source manager/IPC/graceful — **26 passed**. Linux настоящий
connector CLI/WS с fake Hermes прошёл prepare/resume, busy refusal и тот же chat.
Это не новая installed-wheel или живая Hermes/VPS приёмка.
После Linux service coordinator: **1044 passed / 23 skipped**. Real isolated uv
tool + dispatcher + fake systemctl прошёл update и invalid-wheel rollback с
восстановлением running/enabled и сохранением env/key/enrollment. Настоящий
systemd сценарий и финальная пакетная приёмка ещё открыты.
После Windows service coordinator: **1055 passed / 27 skipped**; 27 skips —
23 native opt-in и 4 installed-Hermes compatibility. Native Windows source
профиль: **49 passed**, включая все **23 Scheduler cases**; четыре сочетания
enabled/running проверены настоящими временными задачами. Это проверка исходников
и Scheduler-контракта; полный установленный Windows uv package/task update
остаётся P4.8.5. Linux isolated uv + dispatcher + fake systemctl smoke повторён
успешно после Windows изменений (`/tmp/acpgw-managed-update-2lhpyyqn/report.json`).

После первого Hermes model driver layer: **1079 passed / 28 skipped**, 152.79 s;
28 skips — 23 native Scheduler и 5 installed-Hermes compatibility. Native source
driver + managed-stream wire профиль: **61 passed**, 15.02 s. Это fake Hermes
loopback, без настоящего model switch; API/CLI/Telegram model controls ещё открыты.

После model transport/core/owner API: **1113 passed / 28 skipped**, 161.97 s;
focused Linux **195 passed**, native Windows source профиль **195 passed**,
42.60 s. Relay использует настоящие loopback WS против fake Hermes, owner API —
ASGI. Durable per-dialogue state теперь готов; CLI/Telegram/defaults и
явное unknown reconciliation остаются в разработке;
реальный Hermes/provider switch и финальная пакетная приёмка не заявляются.

После durable state: **1125 passed / 28 skipped**, 159.85 s в WSL;
focused storage/core/live restart/owner API **62 passed**, 5.32 s.
Ruff check/format: 219 files, clean. Native Windows результат выше исторический;
новая migration/state и живое переключение Hermes отдельно от этого прогона.

## Что агент должен доработать до передачи пользователю

До session manager заложить channel-neutral logical dialogue/active context,
authenticated actor и проверяемый owner/workspace scope. Это основа для будущего
Web UI и нескольких пользователей, не заявление о готовой multi-user поддержке.
Аккаунты и self-service onboarding приняты отдельным P4.10; первый режим —
личные пространства по приглашению. Командный доступ и биллинг отложены.

1. **Скрипты для Git-копии на Linux и Windows.** Довести существующий install.sh,
   добавить Windows checkout installer и лёгкие launcher/update scripts с
   одинаковой передачей CLI-аргументов и корректной обработкой путей с пробелами.
   Выбрать один эксплуатационный режим: Git-копия служит источником пакета,
   устанавливаемого в uv tool; скрипт запускает именно эту установку. Не смешивать
   её с editable uv run, которым текущий updater управлять не умеет. Обновление
   из выбранного checkout/ref не должно сбрасывать пользовательские изменения
   или незаметно переключаться на PyPI. Установка и обновление — разные операции;
   обновление не должно снова запускать первоначальную настройку.
   **Код реализован:** install/run/update для sh и PowerShell используют общий
   stdlib helper и именно uv tool entry point. Locked export, guarded update,
   синхронное ожидание Windows helper, отказ чужой установки и отсутствие
   повторного setup. Проверены fake-tool lifecycle и shell quoting. В P4.8.5
   также прошёл настоящий isolated Linux checkout install/run/update/repeated
   install/uninstall с сохранением config/env/key и enrollment registry.
   Настоящий Windows checkout lifecycle и enabled/running services ещё открыты.

2. **Завершённый Windows connector setup.** Из сохранённых connection options
   получать launch для службы без повторного ручного ввода WSS URL/computer ID/key.
   Подготовить постоянные data/log paths, приватные config/env/key и понятную ACL
   диагностику. Не менять ACL внешнего Hermes и не менять произвольные файлы
   автоматически. Создание новых собственных каталогов должно обеспечивать
   правильную приватность. Добавить завершённый путь регистрации/включения
   logon task и повторной настройки с сохранением прежних данных.
   Выполнено: service install без launch options читает saved connection рядом
   с выбранным config; start использует ту же проверку. Partial explicit options
   отклоняются без смешивания с saved key/pin. Общий parser роли вынесен из CLI.
   Windows wizard теперь задаёт постоянные data/log paths рядом с config,
   сохраняет custom absolute paths, создаёт недостающие private directories
   после Save, проверяет ACL перед чтением и до записи secrets. Existing ACL
   не меняет; unsafe paths отклоняются. Проверка native Windows на disposable
   paths прошла. **Установка task из мастера реализована:** опция перед Save,
   snapshot только disabled/idle owned task с теми же config/env; install/enable
   с control-ID fencing. При ошибке новый task штатно останавливается,
   восстанавливается прежнее disabled определение (либо отсутствие task), затем
   config/env/state. Concurrent task/file changes прерывают автоматический откат.
   Шесть новых native disposable сценариев прошли; полная живая приёмка
   Hermes/VPS/двух ПК остаётся отдельным P4.8.5.

3. **Законченный CLI для обеих ролей.** Проверить и устранить пробелы в setup,
   configure/connect, start, status, doctor, service start/stop/restart/enable/
   disable/logs/uninstall, computers enroll/list/status/rotate/revoke и sessions/
   jobs/results/approvals. Большинство команд уже есть; новые синонимы не нужны.
   Вывод должен различать живой процесс, подключение к VPS и доступность Hermes.
   Сохранённая роль/config должны использоваться согласованно в меню, скриптах,
   службе и maintenance. Неподдержанный Hermes cancel объясняется явно.
   Реализован первый слой постоянного выбора: core.submit_selected не создаёт
   chat скрыто, core.switch_dialogue проверяет постоянный ID и ownership;
   Telegram использует оба. Неудачный /new другого агента сохраняет прежний route.
   Revision в SQLite не даёт запоздавшему созданию затереть более поздний выбор.

4. **Довести обновление как штатную операцию CLI.** Использовать существующий
   snapshot/updater вместо нового механизма. Добавить управляемый цикл сохранения
   состояния службы → штатная остановка/disable → update → health check →
   восстановление определения службы при необходимости → прежнее enabled/running
   состояние. На Windows дождаться фактического результата helper, а не принимать
   queued за успех. При ошибке восстановить работоспособную установку и показать
   результат. Сохранять config, Telegram token, computer keys, SQLite и connection
   recipe; не переоткрывать setup. Активные jobs/approvals блокируют автоматический
   цикл до явного завершения: update не должен прерывать инструмент или повторять
   неизвестный prompt. Rollback базы имеет отдельные явно описанные последствия.
   Первый слой реализован: dispatcher maintenance reservation в общем core,
   owner API и CLI `maintenance status/prepare/resume`. Prepare атомарно закрывает
   admission только при отсутствии jobs/approvals/in-flight session creation;
   queued requests повторно проверяют gate после conversation lock. Token
   приватно сохраняется, owner recovery явный. Это ещё не auto service update:
   wiring в updater и сохранение enabled/running открыты. Connector local
   status/prepare/resume реализованы для Hermes: instance-fenced IPC, heartbeat,
   atomic manager activity check, запрет новых streams/new/load/prompt при
   reservation. Pending и uncertain, включая lost attachment, блокируют prepare;
   другие agent profiles пока unsupported. Это ещё не автоматический stop/update.
   **Linux coordinator реализован:** `update --with-services` для выбранного
   owned systemd unit и точных config/env. Reserve → disable/stop → existing
   updater/health/rollback → восстановление enabled/running и startup check.
   Поддержаны четыре сочетания state; parallel internal mutations блокирует
   отдельный inherited coordinator lock. `update --status` учитывает весь цикл.
   Настоящий isolated uv update/invalid-wheel rollback с real daemon и fake
   systemctl прошёл. Windows Task Scheduler coordinator и настоящий systemd
   acceptance ещё открыты. Определение службы не мигрирует между installations.
   **Windows coordinator реализован в исходниках:** snapshot owned task/config/
   env/data/control ID/instance, reserve → graceful disable/stop → синхронное
   ожидание package helper → прежние enabled/running и local readiness. Queued
   не считается success; pending marker/lock допускают только свой inner helper.
   Четыре native state combinations/definition fencing прошли. Новый installed
   uv package lifecycle остаётся отдельной приёмкой P4.8.5.

5. **Инструкция для Linux VPS без Docker.** Основной путь — uv tool
   либо Git-копия со скриптами, setup server/Telegram, enrollment home/work и
   systemd user. Описать WSS через свой proxy либо встроенный TLS, persistent
   paths, логи и автозапуск после reboot без интерактивного SSH (user lingering
   требует настройки хоста). Docker остаётся дополнительным вариантом. Подготовить
   краткую памятку команд для VPS и Windows и исправить устаревшие Goose/Docker
   примеры, если они показаны как основной путь этого трека.
   **Инструкция подготовлена:** [Linux VPS без Docker](../setup/vps-native-hermes.ru.md)
   описывает uv/Git, server/Telegram, WSS proxy либо native TLS, enrollment,
   systemd/lingering, оба Windows connector и текущий ручной service update.
   Реальное развёртывание и reboot проверяются отдельно по P4.8.5.

6. **Постоянный выбор и сохранение чата при VPS reconnect — PARTIAL.** Первый
   connector-owned session manager реализован: attachment живёт независимо
   от remote stream. Новый managed dialogue получает restore=connector-live;
   локальный session/load возвращает только известный idle handle, проверяет cwd
   и отсутствие другого владельца, не вызывает Hermes resume/activate. Dispatcher
   сверяет binding с SQLite. Старые stream IDs/permission responses не оживают.
   Проверены реальный CLI процесс + WS reconnect + fake Hermes, новый ACP stream,
   restart core/SQLite и отказ foreign IDs/другого manager/provenance mismatch.
   Гарантия пока ограничена живыми connector + Hermes и завершённым предыдущим
   ходом. Drop с pending prompt/approval навсегда блокирует этот handle в текущем
   manager, даже если позднее пришло completion; запроса/решения replay нет.
   **Готово как source:** bounded event/result journal, conservative in-flight
   outcome diagnostics и explicit idle/detached handle retirement.
   **Остаётся:** установленная и живая приёмка P4.8.5. Потеря самого
   Hermes attachment, restart connector и cold resume — отдельные случаи.
   Активный logical dialogue не меняется при ошибке, offline, reconnect или
   рестарте. Если продолжить нельзя, показывается причина без скрытого /new.
   Создание/переключение — только явные пользовательские действия; выбор и
   операция привязаны к user/workspace, независимо от Telegram delivery address.

7. **Провайдер и модель Hermes — READY как кодовая основа кандидата.** По решению
   владельца 2026-10-13 добавить show/list/set для текущего диалога и отдельный
   default новых диалогов, общий core/API для CLI/Telegram, scoped persistence,
   guards busy/maintenance/stale selection и reconcile после reconnect. Credentials
   остаются в Hermes на хосте. Chat не меняется скрытно; queued/deferred не означает
   applied. Начато исследование установленного контракта: `model.options`,
   session-scoped `config.set`, compute-host deferral и confirmation. Production
   show/list/set теперь подключены к managed stream/relay/core/owner API. CLI/Telegram, scoped defaults и exact known-outcome reconciliation готовы.
   Unknown upstream failure/prompt-loss/cold restart остаются fenced. Durable
   checkpoint intent/requested/observed/pending/unknown и restart fences готовы,
   owner GET model-state работает без сети. Первый driver layer готов:
   owned show/list/set, session scope, projected catalogue, confirmation tokens,
   configuring/pending/unknown state и maintenance gating. Synthetic wire tests
   подтверждают сохранение chat identity; model.options пока означает observation,
   не доказательство реально обслужившего provider. Подробные требования, последовательность и
   граница доказательств: [управление моделью](hermes-model-control.ru.md).
   Установка/живая проверка входят в P4.8.5 отдельно от разработки.

8. **Финальная актуальная поставка после функциональных доработок.** По решению
   владельца подготовить версионированный wheel и доступный проверяемый Git ref,
   зависимости и checksums ближе к завершению разработки, чтобы релиз включал
   больше готовых возможностей. В ходе разработки допустимы временные тестовые
   wheels; это не финальный релиз. Глобальная установка из wheel/Git должна
   работать до публикации в registry; установка по имени пакета требует отдельного
   нового опубликованного релиза. Старый 0.1.0 не представлять как содержащий P4.8.
   Финальная приёмка именно собранного пакета идёт в отдельном этапе ниже;
   публикация — отдельное действие после её результата.

«Обновить без переустановки» означает для пользователя штатную команду/скрипт,
сохранение настроек и данных и возвращение службы в прежнее состояние. Внутреннее
замещение пакета через uv является частью updater; ручное удаление, повторный
enrollment и повторный setup не требуются.

Полная функциональная source база кандидата: **1152 passed / 28 skipped**,
183.75 s; ruff check/format — 220 files, clean.
Текущий порядок приёмки и CLI команды: [runbook](../setup/p48-acceptance.md).

Кандидат 0.2.0rc1 собран из чистого source ref
3da8dffbd5d265c0b135e3aa1bd212e8e618c779: dist/p48-0.2.0rc1.
Installed Linux wheel: **1152 passed / 28 skipped**, 150.18 s.
Native source: **287 passed / 2 skipped**, 90.77 s.
Installed native wheel: **374 passed / 2 skipped**, 251.10 s; все 23 Scheduler.
Ruff финального staging: 221 files, clean; secrets scanner — 0 findings.
Readonly probe установленного кандидата: Hermes 0.21.6 ready, без mutation.
Manifest/JUnit/checksums сохранены рядом с wheel; пакет не опубликован.

## Отдельный этап: установка и приёмка P4.8.5

Это полный перечень предполагаемых сценариев. Агент выполняет всё, что доступно
в своей среде; отсутствие второго ПК/доступа к VPS не превращает проверку в passed.
Пользователь предоставляет доступ/значения секретов приватно и выполняет физические
действия. При доступе к целевому хосту команды установки может выполнить агент.

| Сценарий | Что проверяет/делает агент | Что нужно от пользователя |
| --- | --- | --- |
| Чистая глобальная uv-установка | Отдельные Linux/Windows tool directories; установка нового wheel/Git ref, CLI из любого cwd, setup каждой роли | Выбрать целевые машины; иметь uv/права текущего пользователя |
| Git-копия и лёгкие скрипты | Linux/Windows install/run/update, пути с пробелами, повторный запуск, отсутствие обращения к чужому acpgw, сохранение dirty checkout | Выбрать checkout/ref; решить, как получать обновления |
| VPS и постоянный запуск | Установка пакета, server setup, config, systemd, SQLite/log paths, health/doctor; запуск после выхода из SSH при доступном хосте | VPS/SSH, домен/TLS либо доступный способ их настройки, права для lingering/firewall |
| Telegram | Настройка token/allowlist, диагностика polling/webhook конфликтов, проверка маршрутизации на mocks и на доступном VPS | Создать/выбрать bot, приватно внести token/user ID, отправить реальные команды боту |
| Первый и второй ПК | Hermes discovery/profile, enrollment key, saved connection, приватные ACL, регистрация/управление task; отсутствие второго процесса и изменений Hermes | Запустить Hermes под нужным Windows-пользователем; доступ к обоим ПК и приватная передача каждому только его key |
| Реальный запрос и Desktop | Подготовить безопасные сообщения, проверить stream/final и адресацию, сопоставить jobs/results | Визуально подтвердить диалоги Desktop и отсутствие изменений личных диалогов; наблюдать ответы Telegram |
| Провайдер и модель Hermes | Проверить show/list/set/default, scope двух ПК/диалогов, сохранение chat ID, busy/deferral/confirmation, reconnect/readback и пакетные CLI/Telegram tests | Настроить providers/credentials на каждом Hermes; команды Telegram, явные confirmations, визуальная проверка Desktop; реальный model call отдельно |
| Настоящие approvals | Проверить once/reject/timeout/late response и фактический результат безобидного действия; mocks отдельно | Нажимать карточки в Telegram; не отвечать одновременно из Desktop; согласовать безопасное действие |
| Изоляция home/work | Разные диалоги/контекст, правильные карточки, отключение одного connector без влияния на другой, idle WSS, rotate/revoke key | Доступ к обоим ПК и рабочему боту; при необходимости переключать подключения |
| Рестарты и неизвестный исход | Restart connector/потеря Hermes attachment: история и выбор остаются, старый handle отклонён; /new только явный. Pending drop: quarantine, replay отсутствует | Подготовить сеанс без важных текущих задач; подтверждать видимый результат в Desktop/Telegram |
| Сохранение чата при VPS reconnect | Idle disconnect/reconnect, restart dispatcher при живом connector/Hermes; тот же logical chat, новый epoch, старые RPC/approvals отвергаются. Автоматизированный CLI/WS/mocks этап пройден; native/live ещё требуется | На живом маршруте подтвердить контекст кодовым словом в том же чате; не вмешиваться из Desktop |
| Постоянный выбор пользователя | Offline/restart/error не меняют active pointer; только явные /new, /switch, выбор маршрута. Поздние ответы/approvals не переезжают в новый выбранный чат | Проверить команды выбора и отображение сообщений в Telegram |
| Обновление и ошибка обновления | Настоящий uv tool update установленного dispatcher/connector, состояние enabled/running, сохранность config/keys/DB, неизменность маршрутов; неисправный пакет/health failure и rollback на изолированных данных | Для рабочих машин выбрать окно без активных jobs; не требуются ручное удаление/повторный setup |
| Резервирование dispatcher перед обслуживанием | prepare/status/resume, отказ busy/approval/session-create, queued admission race, private token, stale token и owner recovery; реальный loopback CLI отдельно от mocks | В живой приёмке отправить Telegram сообщение во время резерва и после resume; выбрать окно обслуживания |
| Резервирование connector | Локальный CLI без owner API, private IPC, instance/expiry, busy/uncertain refusal, блокировка новых запросов и тот же chat после resume; Linux CLI/WS/fake Hermes и native профиль отдельно от live | На каждом ПК проверить отказ при безопасном активном ходе и продолжение после resume; не проверять на важных действиях |
| Удаление | В отдельной установке удалить только свои managed services/package, сохранить config/keys/DB; чужие задачи не затронуты | Удаление рабочих установок не требуется |
| Reboot/logon | Подготовить инструкции и собрать status/logs после включения; проверить автозапуск VPS и Windows task | Перезагрузить целевые машины и войти в Windows; агент не инициирует неожиданную перезагрузку |
| Sleep/wake/network | Подготовить наблюдение, проверить reconnect и отсутствие replay/дубликатов после события | Усыпить/разбудить ПК, переключить Wi-Fi/VPN или отключить сеть |

Готовность к передаче: пункты разработки выполнены и доступные агенту сценарии
изолированной установки/обновления прошли на Linux и Windows. Готовность Windows
MVP: дополнительно подтверждён реальный маршрут VPS → home/work Hermes → Telegram
и целевой lifecycle. Пока физические/живые сценарии не выполнены, они остаются
открытыми в отчёте независимо от числа прошедших unit tests.

## За границами текущего MVP

- Goose, Linux/WSL/macOS agent attach и новые адаптеры — отдельный P4.9.
- Общая запись Desktop/Telegram в пользовательский диалог, безопасная отмена
  конкретного Hermes turn и restore без auto-continue требуют upstream ownership
  контракта. Не включать их скрыто в критерии готовности выбранного MVP.
- Offline queue и durable recovery неизвестного результата — дальнейшая работа
  P4.1. Новые каналы управления — отдельные треки поверх того же dispatcher.
- Полноценные аккаунты, self-service connectors и межпользовательская изоляция
  приняты в P4.10; контракты ownership/actor/dialogue закладываются в P4.8.
