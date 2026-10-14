# P2.3 — Telegram: inline управление и форматирование

**Статус:** PARTIAL — реализация rc6 проверена автоматически;
живая приёмка нового интерфейса владельцем ещё впереди.

## Решения владельца и подтверждённая приёмка

2026-10-14 владелец подтвердил обновление VPS/Windows до rc5 и корректную работу
обоих ПК home/home2. Основные команды sessions/results и первый реальный prompt
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
  dist/telegram-ux-preview и dist/telegram-ux-verification. PyPI/tag rc5 не менялись.

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