# P4.11 — задачи коннектора: ручной запуск и расписание

**Статус:** DEFERRED — отдельный будущий этап; архитектура от 2026-10-15, реализации нет.
**Репозиторий:** `acp-gateway`.
Основной контракт модулей — [P4.16](connector-action-modules.ru.md).

## Решения владельца — 2026-10-15

1. Задачи находятся на коннекторе. Определения, расписания, authoritative состояние
   и история исполнения принадлежат компьютеру, не диспетчеру.
2. Запуск вручную, в заданное время или периодически.
3. Два взаимоисключающих executor: подключённый агент с prompt или действие
   пользовательского модуля без агентского запроса.
4. Управление через диспетчер (включая Telegram) и CLI коннектора.
5. Работающий коннектор продолжает разрешённые локальной политикой задачи
   при недоступном диспетчере/соединении. Это отдельно подтверждено владельцем.
6. Подтверждено: пересечения и пропущенные сроки пропускаются с записью причины,
   без очереди и автоматического догоняющего выполнения.

Последующие технические defaults — предложения, не факт реализации.
В текущей поставке административных actions нет. В будущем P4.16 по уточнению
2026-10-20 разделяет базовые встроенные действия и пользовательские модули;
точное участие built-ins в Task executor ещё проектируется. Модульный executor
не вызывает LLM сам по себе; собственный модуль пользователя может обращаться
к внешним сервисам, поэтому универсальное обещание нулевых расходов неверно.

## Изменение прежнего решения

Proposal от 2026-10-13 предполагал Scenario/Schedule и scheduler на диспетчере,
который в момент due отправляет готовый prompt коннектору. Решением 2026-10-15
эта модель заменена: dispatcher управляет задачей, а due и исполнение локальны.
Время — явная timezone задачи, не глобальная зона диспетчера.

Сохранено: каждый агентский Run создаёт новый отдельный dialogue, без reuse
пользовательского чата; неизвестный исход не разрешает replay prompt/мутации.
Прежнее offline connector → skip + notify теперь означает: выключенный ПК или
остановленный runtime не догоняет пропущенные сроки после возвращения. Потеря
связи с dispatcher сама по себе не пропускает разрешённую локальную задачу.
Локальное выполнение агента без relay ещё предстоит реализовать; существующая
поддержка подключённого агента не доказывает наличие автономного Task Runner.

## Разделение ответственности

Connector Task Service владеет CRUD, revision checks, расписаниями, admission,
Run journal, executor adapters и outbox. Это единый application service на ПК.
CLI обращается к нему через защищённый локальный IPC; remote management frames
через существующий исходящий WSS вызывают тот же сервис с remote authorization.
Прямое редактирование работающей БД через CLI и второй независимый scheduler
не допускаются. При остановленном runtime CLI показывает stopped; offline import
возможен только с exclusive lock, если будет добавлен отдельным решением.

Диспетчер хранит каталог/проекцию задач, историю доступных Run, remote request
intents и audit, но не является вторым владельцем definition и не запускает due.
События синхронизируются cursor/sequence, с snapshot после gap и дедупликацией.
При offline коннекторе UI честно показывает cache/staleness. В MVP изменения и
remote run offline отклоняются, не сохраняются в скрытую очередь на reconnect.

CLI и dispatcher используют expected_revision; при конфликте пользователь
обновляет карточку. Успешная локальная запись публикует новую revision обоим
клиентам. Передача task definition не устанавливает executable: модули заранее
установлены и разрешены на ПК. Prompt/cwd/model также проверяются локально.

## Модель данных

| Объект | Смысл |
| --- | --- |
| Task | computer_id + task_id, name, revision, owner/local principal, enabled, executor и policy |
| AgentExecutor | local agent profile, prompt, разрешённый cwd, model/capability policy; новый dialogue |
| ModuleExecutor | module/action id, descriptor revision, валидированные параметры; без agent dialogue |
| Schedule | task_id, generation, paused, once/calendar/interval, timezone, next due, misfire policy |
| Run | run_id, task/executor revision snapshot, manual/scheduled trigger, initiator, due, status/result |
| Execution intent | stable operation_id, admission evidence, исполнение/неизвестный исход |
| Audit / Outbox | изменения и авторизация; отдельная доставка события/уведомления |

Task.enabled=false блокирует новые ручные и плановые запуски. Pause schedule
останавливает только расписание; ручной Run остаётся возможным. Run now не
сдвигает next_due. Edit/disable/delete не останавливают уже допущенный Run;
остановка — отдельный capability-gated cancel. Удаление task не стирает audit.
В MVP один executor на Task, без цепочек agent→module, fan-out и fallback.

## Расписание и работа после простоя

Первый объём: manual, одноразовое время, calendar daily и fixed interval.
Примеры: сегодня в 18:30 Europe/Moscow; ежедневно в 09:00; каждые 30 минут.
Weekdays/cron/exclusions — возможные расширения после проверки простого UI.
Каждая calendar schedule имеет явно выбранную IANA zone, UI показывает следующие
запуски. Хранятся UTC instants + zone/правило. Interval имеет фиксированный anchor,
не отсчитывается заново после manual run или окончания предыдущего Run.

Подтверждённые правила skip и предлагаемые технические defaults:
- concurrency=1 на Task, общий resource admission между задачами, модулями и ACP;
- busy/overlap → skipped + причина, без скрытого накопления;
- после sleep/reboot/остановки runtime — skip пропущенных occurrences без catch-up;
- runtime не включает и не будит выключенный ПК; OS autostart — отдельная настройка;
- DST gap → skip, fold → один запуск; clock rollback/restart не дублируют occurrence;
- небольшой явный grace допускает обычный scheduler jitter, но не маскирует простой.

Occurrence key включает task_id, schedule generation и planned UTC instant.
Claim и durable intent атомарны; один active scheduler на локальное хранилище.
Manual request имеет idempotency key. Edit увеличивает revision/generation;
старый claim проходит повторную проверку до start. Нужны locks/fencing для
гонок CLI/dispatcher/scheduler, а не только защита кнопки Telegram.

## Два исполнителя

### Module

Task Runner вызывает Action Runtime P4.16 с зафиксированным action/params и тем
же operation_id. Общие validation, local policy, journal, лимиты stdout/runtime,
concurrency group и неизвестные исходы. Direct action и Task run разделяют
admission; ручной action не создаёт вторую scheduler запись.

### Agent

Task Runner использует local agent transport/adapters с теми же проверками
cwd/tools/model, ownership, approvals и capability limits. Не отправляет prompt
обратно через VPS и не вызывает agent CLI в обход gateway policy. Runtime
получает новую выделенную managed session на Run; её identity можно показывать
через dispatcher после синхронизации, без превращения в активный чат пользователя.
Agent runner потребует отдельных session/job/result и approval lifecycle на ПК;
это существенная часть реализации, не тонкая обёртка над module subprocess.

Отказ/неизвестный исход агента не переключают Task на другой агент/модуль.
Timeout/cancel не доказывают остановку или отсутствие внешних эффектов.
Лимит токенов/стоимости возможен только при достоверной поддержке backend.

## Автономность, права и подтверждения

Разрешение расписания — явная локально сохранённая authority для конкретной
Task revision/executor/параметров. Расписание не выдаёт blanket allow для
agent tools или модулей. Изменение prompt/action/параметров/политики инвалидирует
предварительное согласие в затронутой части; новый module digest требует revalidation.

Для операции с обязательным человеческим подтверждением: либо оно получено
для конкретного Run, либо существует явно допустимое локальное разрешение
на эту точную периодическую операцию. Если подходящего разрешения нет, Run
блокируется/ждёт до deadline и затем завершается отказом; offline не означает auto allow.
Новый диалог и безопасный approval режим остаются обязательными для AgentExecutor.
Local CLI approval возможен только через отдельно авторизованный human канал,
а не через ответ script/агента; детальный UX ещё нужно согласовать.

Owner/local policy определяет, каким Task разрешена автономная работа.
Расписания действуют от локальной service identity с ограниченной сохранённой
authority; remote edits проверяют actor и connector grant, CLI — права OS пользователя.
/mode auto не предоставляет полномочий management API.

Без связи невозможно узнать о свежем удалённом отзыве прав немедленно. Для MVP
явная local offline authority действует до локального запрета или получения
revoke при reconnect; локальная policy всегда может запретить start. Для shared
P4.10 можно отдельно ввести authority lease/expiry: это ограничит автономность.
Не обещать одновременно бессрочный offline execution и мгновенный remote revoke.

## История, доставка и recovery

Run journal и результаты сначала локальны. Во время outage task продолжает
исполнение, outbox сохраняет события; после reconnect dispatcher получает их
и доставляет уведомления, не повторяя исполнение. Уведомления настраиваются
отдельно (ошибки/пропуски/все итоги), чтобы частая задача не засоряла Telegram.
Нужны лимиты диска, retention, redaction и политика outbox overflow; заполнение
хранилища не должно вести к start без durable intent.

Если процесс/агент мог принять запрос, но нет доказанного terminal outcome,
Run=unknown; восстановление/reconcile только по journal/capabilities. Локальный
dedupe не обеспечивает exactly-once внешних действий. Пользовательский повтор
создаёт новый Run с предупреждением, не тайно переисполняет неизвестный intent.
После restore старой БД pending operations требуют reconciliation/fencing.

## Этапы и приёмка

1. Утвердить Task/Schedule/Run, local authority и approval policies; skip без
   очереди/catch-up уже подтверждён.
2. P4.16 registry/runner и локальный Task Service с ручным module executor/CLI.
3. Локальный scheduler: once/daily/interval, fake-clock tests, locks и restart.
4. AgentExecutor: отдельные sessions, local approval/result, без VPS dependency.
5. WSS management и dispatcher/Telegram UI для обоих исполнителей.
6. Linux/Windows приёмка, автономный outage/reconnect, disk/clock/crash tests.

Проверки: CLI и remote concurrent edits; duplicate manual/tick; old revision;
busy/overlap; sleep/missed due; DST/clock rollback; agent unknown; module crash;
outbox recovery; insufficient approval offline; module removal/change; права
actor и OS principal; bounded quotas; no token/secret leaks. Shared multi-user
выпуск требует P4.10; публичный client API — P4.12, не блокирует single-owner CLI.

## Открытые решения

- Выбрать первый собственный модуль для испытания.
- Согласовать allowed unattended policy для module/agent и локальный approval UX.
- Определить retention/output/disk quotas и уведомления по умолчанию.

### Журнал — 2026-10-15

Владелец заменил dispatcher scheduling локальными задачами коннектора с двумя
исполнителями и управлением через dispatcher/CLI. Отдельно подтвердил продолжение
локально разрешённых задач при outage диспетчера. Предыдущая модель времени
диспетчера и готового remote prompt заменена; новый dialogue на агентский Run
и отсутствие replay неизвестного исхода сохранены. Runtime не изменялся.

Владелец также подтвердил skip для пересечений/пропущенных сроков без очереди
и catch-up. Пример первого возможного модуля — управление Happ; несколько
публичных действий и внутренние шаги описаны в P4.16. Реализации нет.

### 2026-10-15 — сохранено для будущего возвращения

По просьбе владельца требования и пример Happ сохранены для отдельного будущего
обсуждения и возможной реализации. Сейчас разработку P4.11/P4.16 и Happ-модуля
не начинать автоматически при продолжении других треков. Сначала вернуться к
этим документам, сверить архитектуру с актуальным кодом, выбрать объём и проверить
способ управления установленной версией Happ. Реализация пока не обещана.
