# P2.3 — Telegram: inline управление и форматирование

> Актуальное решение 2026-10-16: разработка Telegram закрыта. Оставшаяся
> живая приёмка предварительно закрыта владельцем; возвращаться только по
> сообщениям о проблемах. Исторические PARTIAL и pending проверки ниже
> сохраняют evidence, но больше не являются активной очередью.


**Статус:** PARTIAL — development dashboard реализован: компьютеры, задачи, навигация;
rc6/rc7 и дополнительные исправления проверены автоматически;
живая приёмка нового интерфейса владельцем ещё впереди.

## Решения владельца и подтверждённая приёмка

2026-10-14 владелец подтвердил обновление VPS/Windows до rc5 и корректную работу
обоих ПК (на позднем скриншоте второй маршрут — work/hermes). Основные команды sessions/results и первый реальный prompt
с ответом PONG проверены ранее. Это подтверждение функциональности двух узлов,
а не доказательство всех сценариев approvals/parallel isolation/lifecycle.

Следующий приоритет — удобство Telegram и правильное отображение сообщений.
Владелец выбрал кнопки под сообщениями; постоянная reply keyboard не используется.

## Реализация 2026-10-14

- `/start` и новый `/menu` открывают inline панель: агенты, диалоги, новый диалог,
  статус, модель, подтверждения, помощь. Команды продолжают работать.
- Агент выбирается кнопкой по полному маршруту; диалоги имеют по 10 кнопок и
  страницы назад/далее. Состояния выбора и cwd сохраняют прежний private scope.
- Модель отображается читаемой сводкой; каталог по 10 вариантов на страницу.
  Выбор вызывает прежний guarded model set с dialogue ID/revision; если upstream
  требует подтверждение, показывается отдельная одноразовая кнопка. Token остаётся
  на диспетчере, не попадает в callback data/текст. Unknown/reconcile/default
  команды сохраняют существующие правила, не повторяют неизвестную мутацию.
- У задачи/ответа видны исходный агент и номер диалога. Result/details buttons
  проверяют принадлежность самого job и работают после выбора другого агента.
  Stop отменяет конкретный owned job, никогда не другую/новую задачу; при явно
  неподдерживаемом безопасном cancel кнопка скрыта. Старый finished job не отменяется.
- Controls привязаны к user/chat/message, route revision и при необходимости
  active selection revision. Непрозрачный callback reference — 26 ASCII bytes,
  server-side cache не более 1024 actions, TTL 1h. Restart/eviction → /menu.
  Mutating controls потребляются до await, двойное нажатие не повторяет host call.
  UI clicks используют message throttle; существующие tool approval buttons
  остаются отдельными и не расходуют этот лимит.
- Закрыта гонка /new: смена route во время RPC больше не перезаписывается ответом
  создания старого диалога. Model UI дополнительно проверяет route после RPC.

## Формат ответов и доставка

markdown-it-py commonmark с table/strikethrough, html=False/maxNesting=20.
Поддержаны заголовки, emphasis/strike, inline/fenced code с language, списки,
quotes и HTTP(S) links. В выбранном совместимом sendMessage/HTML пути таблицы
показаны моноширинными строками; images — описательные ссылки без загрузки.
Raw HTML всегда текст, link previews отключены. Media uploads/полноценные
математические блоки этой итерацией не реализованы.

Redaction выполняется до разбора, служебные masks защищены от Markdown-разметки.
Каждый фрагмент самостоятельно сбалансирован: закрытие/повторное открытие тегов,
до 2000 codepoints/4000 UTF-16 units после entity parsing. Code/pre не перекрывают
bold/link/quote; nested blockquotes уплощаются. Только явный 400 entity parse
rejection включает plain fallback. Network timeout не запускает повторную отправку.
Частичный status preview отформатирован, итог доставляется через durable outbox.

Новый hash включает FORMAT_VERSION и rendered chunks. Уже связанные rc5 deliveries
продолжаются в исходном plain формате и с прежними границами/ACK cursor. При
несовпадающем содержимом остаётся unknown fence; неизвестные remote ACK не повторяются.
Stored answer не переписывается. Explicit result button использует bounded queue,
не пересылает prompt и не сбрасывает автоматическую доставку.

## Проверки и evidence

- Полный source suite на основной реализации: **1287 passed / 31 expected
  platform/opt-in skips**, 196.48s; source.xml/source.log.
- Финальный focused Linux suite: **79 passed**, включая последние capability/
  concurrent click/preview-mask проверки; focused.xml/focused.log.
- Установленный preview wheel в отдельном native Windows environment: **79 passed**,
  39.36s; windows-final.xml/windows-final.log. Production uv tool не заменялся.
- Covered: две agent routes, чужой user/message, route away/back и selection
  revisions, двойные/параллельные clicks, TTL/cache bounds, confirmation token,
  остановка точного job, legacy chunk continuation, unknown delivery/ACK fences,
  parse fallback, Unicode/HTML/code/table и отсутствие запросов модели при навигации.
- Ruff/format/diff checks пройдены. Preview wheel/evidence находятся в ignored
  dist/telegram-ux-preview и dist/telegram-ux-verification. PyPI/tag rc5 не менялись. rc6 опубликован; release/registry evidence — в журнале поставки.

Источники: [markdown-it-py](https://github.com/executablebooks/markdown-it-py/blob/master/docs/using.md),
[aiogram callbacks](https://github.com/aiogram/aiogram/blob/dev-3.x/docs/dispatcher/filters/callback_data.rst),
[Telegram HTML](https://core.telegram.org/bots/api#html-style),
[inline keyboard](https://core.telegram.org/bots/api#inlinekeyboardbutton).
Context7 использован перед official Bot API сверкой. Актуальный Bot API также
имеет отдельный Rich Messages путь с tables/math/media; он не смешивается с
проверенным sendMessage outbox в этой итерации и может быть самостоятельным
расширением с явным persisted format contract и клиентской приёмкой.

## Следующая живая приёмка

1. Выпустить код, обновить dispatcher; проверить /menu и выбор home/home2 кнопками.
2. Создать/выбрать dialogue, проверить paging и model selection/confirmation,
   повторное нажатие и старые controls после смены route/dialogue/restart.
3. Попросить длинный ответ с Markdown/code/Unicode/table; проверить автоматическую
   доставку, result buttons и связь ответа с конкретным ПК/диалогом.
4. Отдельно разрешить/отклонить безопасное действие, проверить private approvals
   между ПК/чатами и отсутствие повторов при network/transport failure.
5. Проверить Windows logon/reboot, порядок Hermes/connector, sleep/wake, idle
   reconnect и обрыв во время работы; затем managed update/rollback на установках.
   Cold resume и unsafe cancel не считать поддержанными без upstream contract.

Релиз: [rc6](https://github.com/Lujker/acp-gateway/releases/tag/v0.2.0rc6),
[PyPI](https://pypi.org/project/acp-gateway/0.2.0rc6/). Реальный registry rc5→rc6
проверен в обеих ОС; живая UI приёмка владельцем остаётся впереди.


## Дополнительные замечания владельца 2026-10-14: названия и выполнение

Владелец уточнил, что ещё не устанавливал rc6: скриншот с сырыми `**bold**`,
backticks и Markdown-таблицей относится к rc5. Рендерер rc6 уже исправляет этот
класс вывода; новый screenshot не считается провалом приёмки rc6. Владелец
подтвердил, что реальные approvals работают. Частота запросов остаётся неудобством:
на скриншоте Hermes запрашивает once/deny для MCP atom-services с trust: untrusted
и без readOnlyHint=true; это не доказательство дублирования одной permission RPC.

Причина постоянного названия найдена в нашем session.create: явно заданный title
Dispatcher становится пользовательским заголовком Hermes и блокирует его штатный
auto-title. В новых dedicated chats этот параметр убран. Dispatcher сохраняет
собственную краткую сводку первого принятого prompt (80 символов, redaction),
передаёт session_info_update и использует её в списках вместо Dispatcher.
Hermes свободен формировать собственное название. Уже созданные пользовательские
заголовки не мигрируются: их можно переименовать в Desktop, новые диалоги создаются
без pin. Production Hermes и его база не изменялись в ходе проверки.

У установленного Hermes Desktop 0.21.6 config.set approvals.mode записывает
настройку профиля (_word_setters в tui_gateway/methods_config_set.py), даже если
передать session_id. Использовать этот RPC для переключателя одного диалога нельзя.
Вместо глобального изменения реализована явная политика диспетчера `/mode`:

- Как Hermes (ask): upstream сам решает, когда запросить разрешение, dispatcher
  показывает такие запросы человеку. Это не обещание ручного запроса на каждый tool.
- Без подтверждений (auto): владелец разрешает все последующие permission requests
  этого диалога; dispatcher выбирает только allow_once и сохраняет каждое решение
  до отправки в ACP. Global trust/allow_always и профиль Hermes не меняются.
- Право задаётся из authenticated private Telegram chat для собственного активного
  dialogue; связаны chat, channel, dialogue ID, ACP identity и selection revision.
  Old/foreign buttons и busy операции отклоняются. Переключать нужно между задачами.
- Настройка сохраняется в SQLite, видна в /menu и /mode, действует после возврата
  к этому диалогу. Каждый новый диалог начинает с ask. Другие чаты, agents, MCP jobs
  и переиспользованный storage ID не наследуют разрешение.
- allow_approvals=false, исключённый approver channel, shutdown, отсутствие
  allow_once и ошибка аудита не разрешают действие. Auto работает и при временной
  недоступности Telegram после явного выбора, без карточек на каждый вызов.
- Политика распространяется на запросы разрешений, которые агент передал через
  ACP; она не подменяет неподдерживаемые host RPC или другие ограничения агента.

Полный source suite: **1305 passed / 31 expected skips**, 204.59s;
`dist/rc7-verification/source.xml` и `source.log`. Покрыты реальный ACP SDK с fake
Hermes, first-title lifecycle, Telegram auto→ask, чужие/stale controls, busy,
policy, storage identity, once-only audit и disk-full отказ без allow.
Живая проверка новых названий и режимов остаётся за владельцем после обновления.

Native Windows: установлен preview wheel rc7 в отдельное validation environment,
**161 passed**, 33.65s; `dist/rc7-verification/windows-focused.xml/.log`.
Production uv tool, scheduled task и живые Hermes conversations не менялись.
Ruff/format/diff checks пройдены. Новое поведение названий находится на стороне
коннектора: для него нужно обновить оба ПК; `/mode` находится в dispatcher и
совместим с rc5/rc6 connectors. Старые заголовки не мигрируются автоматически.


Финальная проверка rc7 после усиления route/actor binding: **1309 passed / 31
expected skips**, 201.22s (`source-final.xml/.log`); установленный финальный wheel
на Windows: **165 passed**, 36.27s (`windows-final.xml/.log`). Изолированный
registry rc5 → wheel rc7 upgrade выполнен в обеих ОС: конфиг/env/setup/key,
enrollment registry и SQLite sentinel сохранены; Linux PTY setup и запуск пустого
dispatcher прошли. Проверка `--stable` на file install ожидаемо отказала без
изменений; registry no-op проверяется отдельно после публикации.


Поставка: [rc7](https://github.com/Lujker/acp-gateway/releases/tag/v0.2.0rc7),
[PyPI](https://pypi.org/project/acp-gateway/0.2.0rc7/). Registry rc5→rc7 проверен
в обеих ОС с сохранением данных; hashes и workflow success записаны в
[журнале поставки](p48-delivery-plan.ru.md). Живая приёмка владельцем впереди.


## Следующая итерация dashboard — 2026-10-14

Владелец запретил новый release и поручил улучшать Telegram UI и функциональность
диспетчера. Выбран следующий завершённый набор: отдельная панель компьютеров,
история задач по всем агентам своего чата и переход к точной задаче/результату,
обновление навигационных сообщений на месте. Production не обновляется.

Общий core/storage слой даёт scoped metadata history с bounded keyset pagination;
Telegram не читает чужие chat jobs и не требует выбранного агента для истории.
История/diagnostics не создают ACP prompts и не пробуют агента скрытно.
Статусы различают offline PC и ещё не проверенную ACP readiness.
Навигационные панели можно редактировать; ответы, job status и approvals сохраняют
собственное назначение. Устаревшие кнопки после редактирования перестают работать,
а поздний ответ операции не перерисовывает более новую навигацию.


### Реализованный интерфейс dashboard (rc8.dev0, без публикации)

- `/menu` → **Компьютеры**: зарегистрированные ПК, карточка подключения,
  известные агенты и кнопка выбора агента. Онлайн-коннектор с ещё не проверенным
  ACP не объявляется недоступным. Выключенный доступ показывается отдельно.
  Команды: `/computers [PAGE]`, `/computers id COMPUTER_ID` (включая числовые ID).
- `/menu` → **Задачи**: последние задачи текущего Telegram-чата по всем его
  агентам, фильтр активных, «Еще» и возврат к новым. `/jobs [active] [before ID]`.
  История сохраняется при смене выбранного агента; удалённый из конфигурации
  агент не скрывает старые результаты. Список читает только метаданные, включая
  cursor lookup, без загрузки ответов и tool payloads.
- `/job ID`: состояние, агент, номер диалога, UTC-время начала/завершения,
  короткая ошибка; обновление, результат, доставка, история. Остановка предлагается
  только для незавершённой задачи агента с поддержкой отмены; после запроса UI
  показывает фактическое состояние, не обещает подтверждённую остановку.
  `/result ID` и `/delivery ID` проверяют channel/chat ownership независимо от
  выбранного агента, как уже делали адресные result buttons.
- Кнопки меню редактируют существующую карточку. После смены карточки старые
  callback tokens недействительны. Вызовы Bot API edit сериализованы для сообщения;
  поздний результат предыдущей навигации не перерисовывает новую карточку.
  Ответы агента, approvals и рабочие status messages остаются отдельными.
  При явном отказе Telegram редактировать отсутствующее сообщение создаётся
  новая карточка; неоднозначная network error не вызывает повторную отправку.
- Общая история доступна через owner-authenticated `GET /jobs` для CLI thread:
  `thread=default`, `agent`, `active`, `before`, `limit=1..50`. Пагинация по
  `(created_at,id)` исключает повтор предыдущей страницы при новых вставках.
  Cursor другого thread/channel/agent отклоняется; удалённый retention cursor
  требует обновить список. SQLite schema остаётся 8.

Сокращение UI labels выполняется после redaction: длинный известный секрет не
может просочиться в кнопке/карточке обрезанным префиксом. Dashboard не выполняет
скрытые ACP prompts/probes. Будущие update/restart/host commands из P4.15/P4.16
не добавлялись в runtime этой итерации.

### Живая приёмка после будущего решения о выпуске

1. Открыть `/menu`, перейти Компьютеры → оба ПК → выбрать агента → Диалоги.
   Проверить, что кнопки меню обновляют карточку, ответы агента сохраняются.
2. Открыть Задачи, фильтр Активные, карточку задачи и её результат/доставку.
   Сменить выбранный ПК и повторно открыть результат старой задачи.
3. Листать историю и вернуться к новым; нажать устаревшую кнопку из прежней
   карточки — действие должно отказать с предложением открыть меню.
4. Отключить один коннектор вручную и обновить его карточку: другой ПК остаётся
   на связи. После восстановления видны актуальное соединение и готовность ACP.

Эти действия на production не выполнялись. Нового GitHub/PyPI release нет;
проверяется development source и отдельный установленный Windows preview wheel.


Автоматическая проверка dashboard: focused core/API/UI **47 passed**;
regression set controls/delivery/dashboard/secret guard **84 passed**.
Установленный private preview wheel на native Windows Python 3.12.13:
**79 passed**, 36.33s (`dist/telegram-dashboard-verification/windows-delivery.xml`).
Windows окружение отдельное, production uv tool/Task Scheduler/Hermes не менялись.
Тестовый Bot API теперь применяет EditMessageText к существующему Message, что
проверяет фактический вид карточки и отсутствие новых сообщений. Старые delivery
тесты адаптированы к реальному редактированию, сохранив проверки message id,
финального текста и отсутствия повторного SendMessage после status 429.


Финальный полный source suite: **1337 passed / 35 expected skips**, 201.26s;
`dist/telegram-dashboard-verification/source-final.xml/.log`. Ruff check,
format и diff whitespace checks пройдены. Native установленный wheel: **79 passed**
(controls/dashboard/delivery/core API/panel guards). Первый полный прогон выявил
cancel-capability regression и два старых ожидания fake status edits; исправлены
и повторно проверены. Secret guard не принял статический test fixture token:
он заменён случайно генерируемым; отключения guard/исключения не вводились.
Статус P2.3 остаётся PARTIAL из-за ещё не выполненной живой приёмки нового UI.
