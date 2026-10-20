# Постоянные диалоги, независимые каналы и аккаунты диспетчера

Дата: 2026-10-13; enrollment/scope уточнены 2026-10-20. Требования подтверждены владельцем.
Это архитектурный план, не заявление об уже реализованной многопользовательской
изоляции. Hermes — целевой агент P4.8; Telegram — текущий основной вход.

## Принято: активный диалог — постоянный выбор пользователя

У пользователя сохраняются выбранный маршрут и активный логический диалог.
Он меняется только явной командой: создать новый, выбрать другой диалог/агента,
либо другим явно обозначенным действием пользователя. `/start`, потеря сети,
restart dispatcher, offline connector или недоступный Hermes не выбирают другой
диалог и не создают новый скрыто. Если выбора ещё нет — предложить `/new`/выбор;
не отправлять обычное сообщение в автоматически выбранный новый чат.

При явном выборе другого агента можно возвращать последний явно выбранный
диалог этого агента, показывая результат пользователю. Если такого диалога нет,
попросить создать/выбрать его. Недоступный/удалённый/отозванный маршрут не даёт
права на fallback к другой машине. Ошибка восстановления сохраняет выбор и
показывает причину, а не меняет указатель.

Нужен стабильный logical_dialogue_id диспетчера. Он отделён от Telegram chat ID,
relay stream/epoch, connection-local handle, Hermes runtime ID и stored ID.
Восстановление может менять технические IDs, но логический диалог остаётся тем же.
Связка с backend/profile/computer хранится и проверяется на каждом rebind.

Текущая основа уже есть: Telegram agent selection в channel_state и active_session
в SQLite conversations. Добавлены core.submit_selected без implicit creation и
core.switch_dialogue по постоянному ID записи с проверкой conversation ownership;
Telegram использует эти операции. Невыполненный /new ALIAS не меняет route.
Generation выбора сохраняется в SQLite и защищает от запоздавшего создания,
включая переключение A → B → A во время await. Created session при конфликте
остаётся в списке, но не заменяет более поздний выбор.
Текущий Conversation=(channel,key,agent) всё ещё одновременно задаёт владение
и адрес канала. Idle reconnect при живом connector session manager поддержан;
restart connector/потеря Hermes attachment не считаются cold resume. Полная actor/
workspace модель ещё предстоит; первый connector session manager реализован.
Legacy submit для
программных callers пока сохраняет auto-create; interactive callers должны
использовать submit_selected. Это не готовая многопользовательская изоляция.

## Принято: продолжение старого Hermes-чата входит в целевой P4.8

Минимальная гарантия: при живых connector и Hermes обычный VPS disconnect/reconnect
и restart dispatcher сохраняют тот же диалог и контекст после подтверждённого
завершения предыдущего хода. Connector session manager должен жить независимо
от remote relay stream. Новая remote epoch получает проверенное право доступа;
старые RPC и approval responses не приобретают его повторно.

Обрыв во время работы выделяется отдельно: локально наблюдать за результатом,
хранить ограниченный журнал результата/событий и согласовать состояние после
подключения. Не повторять prompt. При неизвестном исходе оставить тот же диалог
активным, заблокировать отправку и объяснить состояние. Отдельно исследуются
restart connector, потеря Hermes WS и cold restart Hermes с auto-continue.
Сохранение выбранного диалога не доказывает возможность возобновления backend.

Desktop может читать диалоги диспетчера. Общая параллельная запись, безопасная
отмена конкретного Hermes turn и гарантированное cold resume пока зависят от
upstream-контракта; выбранный указатель не устраняет эти ограничения.

Состояние реализации: idle VPS reconnect и restart dispatcher покрыты новым
HermesSessionManager и restore=connector-live binding. ACP load не вызывает
Hermes resume; допускаются только свои живые idle handles в том же manager.
Dispatcher сверяет сохранённую provenance перед prompt. In-flight drop пока
даёт quarantine этого handle, а не автоматическое recovery результата.
Bounded journal и exact known model-outcome reconciliation уже реализованы;
поздний prompt result не снимает prompt-loss fence. Restart connector/потеря Hermes WS снимают
право load. Manager scoped по локальному alias текущего single-owner connector;
workspace authorization по-прежнему обязательна до включения второго пользователя.

## Принято: Telegram не владеет предметной моделью

Telegram, будущий Web UI, CLI/API и другие входы — adapters к одному application
layer. Этот слой выполняет выбор/создание диалога, отправку, список jobs/results,
approval decisions и проверки прав. Канал аутентифицирует пользователя,
преобразует ввод в команды и доставляет события; не определяет владение ресурсом.

В ядре нужны независимые user identity, resource ownership, logical dialogue,
agent binding и delivery subscription. Будущий Web UI должен иметь возможность
явно открыть тот же logical dialogue без копирования истории Telegram.
Последний выбранный диалог можно хранить отдельно для каждого interaction context;
глобальную синхронизацию переключений между всеми входами не включать скрыто.
Политика синхронизации каналов остаётся отдельным выбором UX.

При приёме сообщения фиксируется конкретный logical dialogue. Более поздний
`/switch` не перенаправляет уже идущий job, его ответ или approval в другой чат.
Для параллельных переключений нужны атомарное сохранение выбора/version и
понятная обработка конфликта. Доставка результатов использует subscription,
проверенный user scope и исходный job/dialogue, а не текущую активную вкладку.

## Обсуждение: несколько независимых пользователей

Рекомендуемый старт — один dispatcher process, один общий Telegram bot, доступ
по приглашениям и личное пространство каждого пользователя. Владелец сервера
администрирует службу; пользователи управляют только своими ресурсами.
Владелец подтвердил личные аккаунты по приглашениям как первый режим.
Командный доступ остаётся последующим расширением.

| Вариант | Плюсы | Стоимость и ограничения |
| --- | --- | --- |
| Отдельный процесс/data directory на пользователя | Простая граница данных, малые изменения текущего ядра | Несколько deployments; один bot нельзя независимо poll каждым процессом; общий вход потребует router |
| Общий dispatcher, личные пространства | Одна установка и bot; свои connectors/dialogues/approvals каждого; соответствует основной цели | Нужна сквозная авторизация, scoped storage и миграция текущего single-owner состояния |
| Общие командные пространства сразу | Совместные машины/диалоги, роли участников | Дополнительно invitations/membership, роли, конкурирующие approvals и отзыв доступа; больше объём до первого релиза |

Рекомендуемая модель заранее допускает все эти случаи без включения командного
доступа в первую версию:

- `User`: внутренний стабильный user_id, статус; не равен Telegram ID.
- `Identity`: provider + issuer/bot context + external subject → user_id.
  Другой канал привязывается только после подтверждения обеих сторон, не по имени.
- `Workspace`: граница ресурсов; сначала личный workspace одного владельца.
- `Membership`: связь user/workspace, сначала только owner; роли расширяются позже.
- `Connector`: внутренний ID, workspace_id, display alias, отдельный credential.
  У двух пользователей могут быть свои `home`; alias не является глобальным ключом.
- `Agent`, `Dialogue`, `Job`, `Approval`, `Audit`: проверяемый workspace/owner scope.
- `ActiveContext`: user/workspace/interaction context → selected route/dialogue;
  при необходимости последние dialogue pointers по каждому маршруту.
- `DeliverySubscription`: кому и через какой вход доставлять события диалога/job.

Для первого личного workspace разрешения фактически owner-only. Командные роли,
sharing отдельных connectors и совместные чаты можно добавить после решения UX.
Наличие workspace не делает данные невидимыми администратору самой VPS/базы;
это логическая изоляция приложения, не end-to-end encryption.

## Актуальное направление: личная привязка, затем multi-user

Уточнение владельца 2026-10-20: ближайший [P4.17](connector-pairing.ru.md) начинается
на целевом ПК: адрес диспетчера + стабильный не секретный ID владельца → запрос →
сверка/подтверждение в уже авторизованном Telegram или CLI диспетчера. Постоянный
credential создаёт/сохраняет приложение; пароль и ручной перенос key/code не нужны.
Прежний bot-first перенос pairing code заменён; данные сверки не являются credential.

Сейчас один профиль владельца, без регистрации других пользователей. Старые ПК
однократно перепривязываются, сохраняя записи/настройки/историю, без автоматической
замены по alias. Обычные updates после перехода сохраняют новую привязку.
WSS рекомендуется, WS разрешён с явным предупреждением/согласием, без downgrade.

P4.10 остаётся будущим invite-only личным workspace для каждого независимого
пользователя; минимальная owner identity P4.17 не доказывает multi-user изоляцию.
При включении P4.10 принадлежность назначает сервер, не клиент. Account invitation
и создание пользователя отделены от запроса подключения устройства. Owner API token
не выдаётся коннекторам или пользовательским клиентам. Общая авторизация/динамический
scoped routing/expiry/revoke/limits требуют отдельной реализации и приёмки.

## Что заложить сейчас и что вынести дальше

**В P4.8 до session manager и финальной поставки:** channel-neutral logical
dialogue/active-context contract; explicit authenticated actor context и единая
точка authorization в application layer; owner/workspace references в новых
контрактах session manager, enrollment и events; миграционный план для default
личного workspace. Не принимать scope, переданный клиентом, без server-side
проверки. Сквозное применение и миграции должны быть завершены до включения
второго независимого пользователя; сами поля в базе изоляции не обеспечивают.

**Принятый отдельный P4.10:** полноценные аккаунты/личные пространства,
invitations, развитие pairing P4.17 до user scopes, dynamic routing, credentials, доступ через
Telegram и будущую привязку каналов. Командные пространства и billing — дальнейшие
расширения после решения. Учёт здесь означает identities/resources/audit/basic
limits; платёжная система не предполагается автоматически.

Текущая allowlist не является полноценной моделью аккаунтов. Список agents и
computer registry общий, owner API token обладает общими полномочиями. Telegram
jobs приватны по conversation, но approvals из других каналов сейчас могут
рассылаться всем разрешённым Telegram chats. Для общего dispatcher это нужно
заменить scoped approver subscriptions, в том числе для callbacks и API.
Обычным пользователям нельзя выдавать общий административный token.

## Отдельная приёмка аккаунтов и постоянных диалогов

| Сценарий | Агент | Пользователь |
| --- | --- | --- |
| Постоянный выбор | Restart/reconnect/offline не меняют route/dialogue; missing selection требует явной команды; /new и /switch меняют pointer атомарно | В Telegram проверить активный диалог и сохранность контекста после живого reconnect |
| Поздний ответ после переключения | Job/events/approval остаются привязаны к исходному диалогу; /switch не перенаправляет работу | Проверить понятное отображение двух диалогов |
| Два личных аккаунта | Один bot, два user_id, одинаковые aliases home; нет пересечения списка, history, jobs, approvals, routes, audit и event subscriptions | Два Telegram-пользователя для живого smoke; свои тестовые connectors |
| Доступ по чужому ID | Отказ на direct API/callback/session/job/connector IDs во всех application entry points; один фильтр списка не считается защитой | Дополнительных действий для автоматизированных отрицательных тестов не требуется |
| Pairing и отзыв | Одноразовость/expiry/неверный scope, подмена owner, revoke/rotate; старая epoch не получает новую authority | Создать запрос в CLI ПК, сверить/подтвердить его в профиле; проверить отключение отозванного ПК |
| Несколько входов | Fake Telegram/Web adapter используют тот же dialogue/application service и actor; identity linking не основан на display name | Настоящий Web UI — отдельный будущий этап |
| Миграция | Старые owner data/bindings/active pointers переходят в default workspace без потери и без назначения всем allowlisted users административных прав | Определить владельца прежних ресурсов при неоднозначной конфигурации |
| Ресурсы и администрирование | Лимиты jobs/connectors/event buffers по workspace, отсутствие starvation; audit actor/action/resource/result, отдельные admin операции | Выбрать начальные лимиты и администраторов при запуске общего сервиса |

Публичная регистрация не включается автоматически. Пока изоляция и её приёмка
не завершены, текущую single-owner сборку не представлять как готовую услугу
для независимых пользователей.


## Связанные будущие контракты (2026-10-13)

P4.11 по последующему решению 2026-10-15 использует connector-owned Task/Schedule/Run:
[актуальный контракт](scenarios-and-schedules.ru.md). Scheduler и due локальны;
dispatcher управляет проекцией, не является вторым scheduler. Local authority
проверяет права на task/executor; multi-user scope/revoke добавляются в P4.10.
Manual и scheduled triggers не создают отдельной модели доступа.

P4.12 выделяет versioned client API и user/device auth:
[API/mobile proposal](client-api-and-mobile.ru.md). Telegram, Web/mobile и CLI
используют один application layer, а admin/connector/provider credentials
не становятся client tokens. P4.14 mobile CONDITIONAL, без выбора framework.

P4.13 [native adapters](connector-harness-adapters.ru.md) наследуют scope и
capability проверки; наличие resume/SDK не выдаёт доступ к чужому live chat.
Эти направления запрошены владельцем, а детали MVP пока предложены для обсуждения.
