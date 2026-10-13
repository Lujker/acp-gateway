# P4.11 — сценарии диспетчера: ручной запуск и расписание

**Статус:** OPEN — направление запрошено владельцем 2026-10-13; исследование и
предлагаемый MVP записаны, реализации нет. Зависимости: P4.10 (identity/scope),
контракты P4.12 (клиенты/API), существующие core/jobs/approvals/outbox.
Режим диалога и offline policy утверждены владельцем ниже. Остальные детали —
предложения для обсуждения, не существующие команды/настройки.

## Задача и пример

Пользователь создаёт «Утренний отчёт»: свой workspace → неизменный connector ID
→ выбранный agent profile → разрешённый cwd → сохранённый prompt.
Нажатие «Запустить» или наступление 09:00 по явно показанному часовому поясу
диспетчера создаёт отдельный Run. В нём видны инициатор, причина запуска,
версия сценария, выбранный диалог, progress/result/error/approval и доставка.
Ручной запуск не меняет время следующего планового запуска.

Сценарий — ресурс приложения, а не Telegram-команда, cron job ОС или скрытый
prompt внутри канала. Telegram, CLI, Web API и будущий мобильный клиент обращаются
к одному application service. Реальный cron/systemd/Windows scheduler не получает
provider keys и не запускает agent CLI в обход connector.

## Решения владельца — 2026-10-13

- Каждый ручной или плановый Run создаёт новый отдельный dialogue. Закреплённый
  диалог и повторное использование активного пользовательского чата не входят
  в первый вариант.
- Если в назначенное время connector недоступен, Run пропускается с причиной
  и уведомлением владельца. Ожидания подключения и автоматического догоняющего
  запуска после reconnect в первом варианте нет.
- Пропуск сохраняется в истории Run; уведомление доставляется отдельно от
  agent execution. Unknown после возможного dispatch по-прежнему не становится
  skipped и не разрешает повторить prompt.
- Это решения MVP, не факт реализации. Busy target, misfire после простоя самого
  dispatcher и approval policy остаются отдельными вопросами.

## Предлагаемый первый этап

- Один текстовый prompt, один явно выбранный connector/agent/cwd, один Run.
- Create/edit/disable/delete, «проверить доступность без prompt», «запустить»,
  список запусков, просмотр результата и явная отмена.
- Сначала ручной запуск; затем разовое время и ежедневный запуск. Произвольный
  cron, цепочки шагов, условия, webhooks, fan-out и загрузка файлов — расширения.
- Принято: новый отдельный dialogue на каждый Run; закреплённый dialogue —
  возможное расширение после отдельного решения.
- Принято: offline connector → skipped + уведомление, без ожидания/reconnect retry.
  Для busy agent предлагается skip; это ещё не выбранная владельцем policy.
- Approval идёт человеку с действующими правами; без ответа действует deadline
  и отказ. Сам факт расписания не выдаёт allow, always или bypass permissions.
- По умолчанию concurrency=1 на сценарий, плюс общий lock диалога и квоты
  workspace/user/connector. Ручной и плановый запуск конкурируют одинаково.

## Объекты и права

| Объект | Основные поля/смысл |
|---|---|
| Scenario | id, workspace_id, owner_id, name, revision, enabled, prompt/parameters |
| Target | immutable connector_id, agent_id/profile, cwd, model policy, required capabilities |
| Schedule | scenario_id, generation, timezone, recurrence/one-shot, next_due_utc |
| Run | id, scenario_revision, trigger, initiator, scheduled_for, job_id, immutable target snapshot |
| Execution intent | run_id, operation_id, target runtime/epoch, dispatch state и evidence |
| Audit | actor, service identity, action, resource, reason, authority revision, result |

Connector alias служит отображению, не является authority. Имена home у разных
пользователей не должны пересекать routing. Workspace/owner вычисляются сервером
по authenticated actor, не принимаются на веру из request body.

Раздельные права: читать/изменять сценарий, управлять расписанием, запустить,
использовать connector/agent, писать в dialogue, отменить, ответить на approval.
Право редактировать prompt не даёт доступ к чужому target. Запуск от имени владельца
не выполняется с глобальным owner API token. Scheduler имеет отдельную service
identity, действует on behalf of владельца и не расширяет его полномочия.
Права проверяются при сохранении, создании Run, перед dispatch и при approval.
Удаление membership, revoke connector, disable agent или смена policy до dispatch
даёт blocked/revoked, а не исполнение по старому снимку прав.
Connector повторно применяет свою локальную политику cwd/tools/model.

Prompt и параметры приватны по workspace. Секреты — только разрешённые scoped
references; provider credentials остаются на host агента. Audit/уведомления не
содержат сырые secrets. Политики retention отдельно охватывают definition,
историю Run и результаты; удаление definition не стирает audit самовольно.

## Время диспетчера

Один явно настраиваемый IANA dispatcher_timezone в первом варианте. UI/CLI
показывают zone и ближайшие запуски, а не только «09:00». Например Europe/Moscow
можно выбрать при настройке; текущая timezone Windows/телефона не меняет запуск.
В базе — UTC instants плюс исходная zone/правило для календарных повторов.
Одноразовое время нормализуется при создании; ежедневное считается в этой zone.
Версия правила/zone фиксируется для Run; изменение настроек явно пересчитывает
только будущие occurrences с показом новой next_due.

Для DST предлагается: отсутствующее локальное время пропустить с записью причины,
повторившееся выполнить один раз (первое UTC соответствие). Это требует тестов;
политику можно изменить до реализации. Wall-clock нужен для calendar due,
monotonic — для таймаутов. NTP jumps, restart, sleep VPS и update timezone data
не должны порождать повтор уже зарегистрированного occurrence.

## Доставка исполнения и неопределённость

Единая цепочка: trigger → persisted occurrence/Run → проверка актуальных прав
→ проверка target/lease/budgets → persisted dispatch intent → core job →
stream/terminal evidence → результат → отдельная доставка уведомления.

Создание occurrence и обновление scheduler watermark атомарны. Dedupe по
scenario_id/schedule_generation/planned UTC instant; ручной trigger имеет
actor-scoped idempotency key. Edit/disable меняет generation, запрещает старым
неотправленным claims проходить final check; применимость проверяется под тем же
lock/transaction перед созданием execution intent. После атомарного допуска dispatch intent Run считается начавшим исполнение
для гонки edit/revoke: изменение не является доказанной отменой внешней команды.
Уже начавшийся Run живёт по immutable snapshot; остановка — отдельная операция. Редактирование не
порождает второй запуск старого tick.

| Ситуация | Предлагаемое поведение |
|---|---|
| Диспетчер перезапустился до due | Восстановить сохранённое расписание |
| Пропущено несколько сроков | Не разгребать все подряд; MVP skip + audit, позднее latest-within-TTL |
| Connector offline | Принято: skipped + уведомление, без запуска после reconnect |
| Agent busy | Предлагается skip; отдельное решение до реализации |
| Два scheduler workers увидели due | Unique occurrence + lease/fencing; один dispatch intent |
| Prompt мог быть принят, ACK потерян | Run unknown, никакого автоматического повторения prompt |
| Dispatcher/connector упал в работе | Сохранить target/session/run; recovery только по доказанным capabilities |
| Нет человека на approval | Timeout/reject, уведомление; расписание не подменяет решение |
| Результат готов, notification потерян | Доставлять сохранённый результат независимо от agent execution |
| Восстановлена старая БД | Pending dispatch/delivery требует reconciliation/unknown fence |
| Диалог занят Desktop/другим Run | Не steal writer; skipped/blocked согласно явной политике |

Exactly-once выполнение внешнего prompt не обещается: локальный dedupe не доказывает
удалённую идемпотентность. Retry безопасен только до возможного dispatch либо после
явного подтверждения непринятия; новый ручной запуск при unknown требует отдельного
решения пользователя с предупреждением о возможном повторном действии.
Смена connector/agent/model/cwd не служит неявным fallback.

Timeout, max turns/tokens/cost — capability-dependent: нельзя обещать жёсткий лимит
стоимости, если backend его не предоставляет. Локально ограничиваются runtime,
очередь, concurrent jobs и output; cancel request не равен доказанному kill.
Многошаговый workflow требует отдельного решения, компенсаций и новых границ.

## Этапы и критерии

1. Согласовать scopes и busy/misfire/approval правила; dialogue/offline policy уже выбраны.
2. Schema/application service для Scenario/Run и ручной запуск через CLI/test API.
3. Fake clock scheduler, durable claims/lease/fencing и одна node deployment.
4. Telegram/API UI, approvals/notifications и отдельная живая приёмка.
5. Только потом несколько scheduler instances и расширенные triggers/workflows.

Автотесты: два пользователя с одинаковыми aliases; чужие IDs/approval/events;
отзыв прав между claim и dispatch; double tick/restart/DST/clock rollback;
manual+scheduled overlap; target offline/removed/busy; duplicate request;
ACK lost после side effect; no human; restore snapshot; bounded quotas.
Живые: один известный connector/agent, разовое время по zone диспетчера,
ручной запуск, отдельный dialogue, сохранённый результат и ответ на approval.

Открытые решения: busy target и misfire после простоя dispatcher; нужны ли
cron/workdays в первом выпуске; зона диспетчера и экран её изменения;
retention/ограничения Run; сценарии без доступного approver.
Новый dialogue и пропуск offline connector с уведомлением уже приняты.
Остальные технические defaults выше остаются предложениями.
