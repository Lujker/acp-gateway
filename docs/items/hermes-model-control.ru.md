# P4.8: управление провайдером и моделью Hermes

Общий checkpoint трека и порядок возвращения: [P4.8 resume](p48-resume.ru.md).

Решение владельца 2026-10-13: включить в текущий P4.8 и реализовать до сборки
финального релиза и передачи пользователю на тестирование. Статус: READY как кодовая основа, живая приёмка — P4.8.5.
Driver/transport/core/API, durable state/defaults, CLI/Telegram и точное
model-outcome reconciliation реализованы; реальные provider/compute-host
проверки остаются в матрице приёмки.

## Поведение для пользователя

- Показать фактические provider/model активного диалога и доступные варианты
  выбранного connector/Hermes profile. Два ПК могут иметь разные настройки.
- Явно сменить provider/model текущего принадлежащего диспетчеру idle-диалога,
  сохранив logical dialogue и upstream chat. Не отправлять команду как prompt.
- Отдельно задать выбор по умолчанию для новых диалогов диспетчера в данном
  owner/workspace/connector/profile. Применять его явно при создании нового чата;
  существующие чаты и общий профиль Desktop не менять этой операцией.
- Telegram и CLI вызывают общие операции ядра/API. Предлагаемые Telegram команды:
  `/model` — текущее состояние, `/models` — варианты, `/model set ...` и
  `/model default ...` — разные явные действия. Окончательный синтаксис уточнить
  при CLI audit; у provider/model отдельные поля, а не неоднозначная строка.
- Ключи, OAuth и endpoints остаются на хосте Hermes. Добавление credentials через
  Telegram не входит в этот пункт; ошибка настройки показывает понятную причину.
- Не менять модель во время jobs/approvals, maintenance или uncertain state.
  Устаревшее меню/ответ не должно менять другой выбранный диалог. Потеря связи
  во время mutation означает неизвестный исход, без автоматического повторения.
- Отличать запрос, deferred/pending, подтверждённое применение и отказ. После
  reconnect сверять runtime, не показывать желаемый выбор как подтверждённый.
  Не создавать новый чат скрытно, не выполнять cold resume и prompt replay.
- Upstream confirmation показать пользователю и привязать к точным session,
  provider/model и selection revision. Не подтверждать предупреждение автоматически.

## Наблюдения установленного Hermes 0.21.6

Исследованы локальные исходники установленного Windows Hermes:
`C:\Users\vladz\AppData\Local\hermes\hermes-agent`.

| Контракт | Наблюдение и следствие |
| --- | --- |
| `model.options` | Каталог с session/profile context; `explicit_only=true` позволяет запрашивать настроенные варианты. Список — подсказка, успешное применение всё равно проверяется upstream. Каталог может обращаться к провайдерам; живая проверка пока не выполнялась. |
| `config.set`, `key=model`, `session_id` | Вызывает switch для конкретной live session. Строка передаётся парсеру `/model`; connector должен сам формировать её из проверенных отдельных полей и явно добавлять `--session`. Пользовательские flags не пропускаются. |
| Session scope | `_switch_request` с `--session` запрещает persistence даже при наличии `--global`. Нельзя полагаться на отсутствие флага: есть конфигурация persist-by-default. |
| Busy | `_set_model` сохраняет pending selection, возвращает `deferred=true`. Наша политика должна отказывать раньше; local idle check не исключает внешнюю Desktop race, поэтому deferred всё равно нужно обрабатывать. |
| Compute host | Даже idle session с `_compute_host_active` откладывает смену до следующего хода. Нужна отдельная проверка acknowledgement/event и фактической модели; немедленный успех обещать нельзя. |
| Confirmation | Guard может вернуть `confirm_required=true`, не применяя и не ставя выбор в pending. Нужен явный ответ пользователя либо отмена. |
| Нет live session | Sessionless `config.set model` отвергается с 4001; изменение общего profile default — другой интерфейс. Dispatcher default поэтому хранится отдельно, а не записывает общий профиль Hermes. |

Источники внутри установленного дерева: `tui_gateway/methods_complete.py`,
`methods_config_set.py`, `model_switch.py`, `hermes_cli/model_switch.py`,
Desktop `src/app/session/hooks/use-model-controls.ts` и `src/lib/model-options.ts`.

`scripts/spike_hermes_models.py` извлекает выбранные upstream определения через
AST, исполняет реальные parser/setter branches на синтетическом состоянии.
Импорт provider/config modules запрещён; resolver/apply/persistence hooks
подменены. Fixture [models.json](../../tests/fixtures/hermes/0.21.6/models.json)
содержит SHA-256 источников. Проверены idle direct dispatch, busy/compute-host
deferral, confirmation без pending, sessionless refusal и session scope.
Это не проверка реального switch, credentials, live catalogue или Desktop GUI.

## Реализованный driver layer

`HermesDedicatedClient.model_options(handle)` и `set_model(handle, provider=...,
model=..., confirmation_token=...)` принимают только локально созданные handles.
Driver формирует точный session-scoped RPC; observer allowlist не расширяется,
произвольные config keys/flags/параметры не передаются. Identifier fields ограничены
одним ASCII token, до 256 символов, без начального дефиса. Projection каталога
содержит только provider IDs и model IDs, без endpoints/credentials и иных полей.

Параллельная операция даёт configuring и блокирует prompt/другую model operation.
Busy turn, uncertain и foreign handle отвергаются. Confirmation одноразовое,
действует пять минут для конкретных handle/provider/model и сбрасывается после
prompt, изменения наблюдаемого выбора или session.info. Таймаут/cancel/невалидный
acknowledgement/RPC error делают чат uncertain без retry. Method-not-found
(-32601) оставляет обычный чат доступным: обработчик не был выполнен.

Ответ `accepted` означает acknowledgement, не подтверждённое выполнение модели.
Смена остаётся pending до свежего `model.options` с совпадающей парой. При
`deferred` дополнительно нужен завершённый следующий собственный ход. Readback
публикует observation_source=model.options; это не доказательство backend,
реально обслужившего модельный запрос. Отложенная смена не блокирует следующий
prompt, но запрещает повторный set и package maintenance. Identity чата остаётся
прежней. При несовпадении aliases/canonical IDs pending не снимается автоматически.

Из source `_session_info` видно, что session.info может показывать pending pick
вместо текущего runtime. Driver не считает такой event подтверждением смены.
`model.options` берёт agent fields через `_model_picker_context`; при отсутствии
agent может использовать profile default. Live/compute-host readback требует
дальнейшей проверки, поэтому интерфейс пока не заявляет applied или фактический
provider ответа. Точное outcome reconciliation по живому manager реализовано ниже;
без авторитетного исхода блокировка сохраняется.

Model operations теперь проходят через managed stream и настоящий WS relay.
ACP metadata объявляет `modelControl: 1`; узкие extensions `_acpgw/model_options`
и `_acpgw/model_set` требуют attached session и точных параметров. Это собственное
расширение Gateway, не стандартная административная функция ACP. Никакой raw
Hermes config RPC через relay не доступен. Клиент сверяет capability и известный
binding, валидирует/проецирует ответ ещё раз и сериализует model operations с prompt.

Manager проверяет stream ownership, maintenance и pending requests. Drop во время
model set блокирует handle даже после позднего ответа; read-only drop не делает
чат навсегда uncertain. Подтверждение не переносится новому stream: detach
инвалидирует его, поздний ответ закрытому stream также отбрасывается. Клиент
сохраняет unknown mutation отдельно от prompt cancellation и не снимает её
через reconnect/load. Refusal до execution и method-not-found оставляют чат
доступным. Нет mutation replay и неявного создания/выбора нового чата.

Core show/list/set работают с активным owned dialogue под conversation lock,
учитывают busy job/maintenance и повторно проверяют selection revision прямо
перед отправкой, после connection/load preflight. Maintenance status включает
model_operations. Confirmation дополнительно связана с conversation, row ID,
provider/model и revision; switch away/back и prompt делают старое подтверждение
неприменимым. После уже отправленной mutation смена выбора не отменяет её;
ответ явно содержит исходные dialogue_id, selection_revision и selection_changed.

Owner API (требует owner authentication; actor/workspace поля не принимаются):

- `GET /sessions` дополнительно возвращает `active_revision`.
- `GET /sessions/{id}/models?agent=...&thread=...` показывает каталог/наблюдение.
- `POST /sessions/{id}/model` принимает `provider`, `model`, обязательную
  `expected_revision`, optional `confirmation_token` и обычные agent/thread.
  Row ID должен быть owned и активным; ключи, endpoints и raw flags запрещены.
  Устаревший selection/confirmation на уровне core возвращает HTTP 409.

### Постоянное состояние model operation — 2026-10-13

Миграция 0006 хранит последнее состояние каждого owned dialogue в SQLite:
requested provider/model и selection revision, observed provider/model с
источником model.options, intent/pending/unknown/refused/confirmation_required/
observed и признак завершённого собственного хода. Это ограниченный по числу
диалогов checkpoint, не история всех попыток и не bounded session event journal.

Core записывает intent в admission callback после connection/load/revision
preflight, до отправки mutation. Ошибка до admission не оставляет intent.
После acknowledgement хранится pending; accepted не переименовывается в applied.
Deferred требует завершённого собственного хода и свежей совпадающей observation
без upstream pending. Alias mismatch не снимает pending.

Cancel/timeout/неизвестная ошибка после отправки сохраняют unknown. При старте
оставшийся intent становится unknown. Prompts и повторный set блокируются для
таких диалогов; unresolved pending/unknown/intent блокируют maintenance.
Известный refusal до исполнения и method-not-found сохраняются как refused,
не отнимая обычный чат. Unknown не снимается совпавшим model.options; явное reconciliation требует
точный operation ID, binding и terminal ACK живого connector manager. Active dialogue не меняется.

GET /sessions/{id}/model-state читает сохранённые evidence без сети и без
reconnect, с обычными owner/conversation guards. Show/list/set возвращают
model_state дополнительно к прежним полям. Observation может быть устаревшей:
updated_at относится к записи состояния в целом, это не время проверки модели.
Confirmation authority/token/message, credentials, endpoints и raw host errors
не сохраняются. После рестарта старое confirmation нельзя использовать.

### Завершённые функции кандидата 0.2.0rc1

- Scoped default сохраняется отдельно для application-assigned single-owner/
  workspace, route/computer/alias и фактического Hermes profile; CLI/Telegram
  используют один default. Для connector profile сначала нужен созданный
  диалог; смена фактического profile не переносит прежний default.
- GET/PUT/DELETE /model-default не вызывают host RPC. Новому чату default
  применяется явно через session-scoped setter. Dialogue row и required-default
  gate записываются атомарно; ошибка/confirmation не разрешает prompt на
  непреднамеренной модели. Explicit accepted/deferred set снимает этот gate.
- CLI model show/list/state/set/default/reconcile и Telegram /model, /models,
  /model set/default/state/reconcile вызывают общий core. Mutation требует
  точный dialogue ID/revision, token одноразовый; route guard повторно проверяется
  после транспортного preflight. Model command не превращается в prompt.
- Intent и operation ID записываются атомарно. _acpgw/model_recover доступен
  только через managed connector, с точным live binding и operation ID.
  Model-loss fence снимается только для известного accepted/deferred/not_applied
  при idle driver, без чужого owner, pending requests и prompt-loss quarantine.
  После ACK нужна свежая observation. Нет повторного config.set/cold resume.
- Lost confirmation возвращает not_applied, token/authority не переносятся.
  Неизвестная ошибка upstream или отсутствующий/чужой outcome остаются fenced.
  Known pre-execution refusal отображается owner API как HTTP 409.
- Canonical model берётся только из проверенного ACK resolved_request;
  первоначальный requested сохраняется отдельно. Каталог сам не придумывает
  alias mapping. Deferred всё равно требует собственного завершённого хода.
- POST /sessions/{id}/model-reconcile требует expected_revision, owned active
  dialogue и owner credential. Его результат адресует исходный dialogue и
  отражает selection_changed. Read-only вызов не создаёт нового чата.

Команды и ограничения: [приёмка P4.8](../setup/p48-acceptance.md).

Проверки после transport/core/API: полная source regression **1113 passed /
28 skipped**, 161.97 s; focused Linux профиль **195 passed**, native Windows
source driver/policy/managed-stream/relay/core/API профиль **195 passed**, 42.60 s. Wire backend fake;
установленный реальный Hermes и его provider/model настройки не менялись.
Ruff check/format и diff check прошли.

После durable model state: **1125 passed / 28 skipped**, 159.85 s.
Focused storage/core/live-restart/API: **62 passed**, 5.32 s; 12 новых cases.
Ruff check/format: 219 files, clean; diff check и ссылки документов проверены.
Эта итерация выполнена в WSL; предыдущие native результаты не являются
проверкой новой миграции/state на native Windows. Реальный Hermes/provider switch
не выполнялся; wire backend synthetic, loopback и SQLite настоящие.

## Состояние функциональной очереди

Кодовая часть model control готова для кандидата. Полная WSL source regression:
**1152 passed / 28 skipped**, 183.75 s; ruff check/format — 220 files, clean.
Живые Hermes/provider/compute-host, Desktop interference и installed-package
обновление/rollback проверяются отдельно в P4.8.5; READY выше относится к
кодовой основе и synthetic/loopback evidence, а не к этим живым сценариям.

## Установка и проверки — отдельный P4.8.5

Агент: synthetic compatibility, adapter/relay/core/CLI/Telegram tests, два
connector с разными каталогами, scope/isolation/stale action/deferral/failure,
reconnect и defaults persistence; затем проверка собранного пакета.

Живая приёмка: пользователь обеспечивает настроенные providers/credentials на
обоих ПК и визуально подтверждает сохранение чата в Desktop. Агент при доступе
проверяет каталог, переключение и readback только в тестовом диалоге диспетчера.
Пользователь выполняет команды Telegram и нужные upstream confirmations.
Отдельный реальный запрос после смены модели согласуется как provider/model call;
synthetic lab такого запроса не делает и не доказывает фактический backend ответа.
