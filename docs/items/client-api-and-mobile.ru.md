# P4.12 — клиентский Web API; P4.14 — будущий мобильный клиент

**Статус P4.12:** OPEN — направление запрошено 2026-10-13, есть proposal,
реализации публичного API нет. Зависимость: identity/ownership P4.10.
**Статус P4.14:** CONDITIONAL — востребованный продукт и стабильный P4.12,
после отдельного решения о native mobile. Flutter/React Native/Swift/Kotlin
сейчас не выбираются; мобильное приложение не является prerequisite API.

## Текущее состояние и граница

HTTP/SSE API уже существует для локального CLI и owner. В source:
api/app.py создаёт FastAPI с openapi_url=None; api/security.py OwnerAccess
проверяет loopback Host, общий owner bearer и размер body. MCP имеет отдельный
token; connector ingress/enrollment — отдельный транспорт и credential.
Это не готовый публичный пользовательский контракт и не per-user authorization.
Наличие HTTP маршрутов не разрешает выдавать owner token каждому телефону.

Предлагается отдельная client surface/API audience с версиями и scoped identity.
Local admin API остаётся административным. Не обходить текущий loopback guard
проксированием глобального owner token наружу. Public API listener/gateway/TLS
и reverse-proxy base path должны проектироваться отдельно и проверяться.

## Общий application layer

Web UI P4.4, mobile, Telegram, CLI и MCP используют одни domain services и
проверки actor/workspace/resource scope. HTTP handler не владеет правилами
доступа; private IDs из callback/push/request body также проходят service guard.
Public contracts не раскрывают сырой agent RPC, provider auth и connector keys.

Предлагаемый base URL: настраиваемый prefix с /api/v1 (например под domain path);
это пример будущего контракта, не существующий endpoint. Клиент получает base URL
при подключении, не угадывает /connect или порт owner API.

| Ресурсы | Предлагаемый доступ |
|---|---|
| me / workspaces | Identity, memberships и разрешения текущего пользователя |
| connectors / agents | Только доступные PCs/profiles, health/capability summaries |
| dialogues / jobs | Создание, prompt, status/result, явная отмена и выбор |
| approvals | Свои открытые requests, одноразовое решение с expiry/version guard |
| scenarios / schedules / runs | P4.11 CRUD, manual trigger, history, pause и cancel |
| events | Scoped stream, cursor, reconnect и authoritative snapshot |
| devices / sessions | Отзыв client session и просмотр связанных устройств |

Формы сообщений, ошибки, pagination, timestamps и enums фиксируются в versioned
OpenAPI. UI и SDK генерируются из контракта, contract tests проверяют совместимость.
Breaking changes требуют новой major API version и понятного периода миграции.
Не публиковать внутренние записи SQLite как бессрочный API.

## Авторизация и мобильный сценарий

- Предлагается стандартный OIDC/OAuth Authorization Code + PKCE для public client,
  без встроенного client secret. Конкретный provider/владение identity и возможный
  промежуточный personal-device token требуют решения перед реализацией.
- Access token короткоживущий, audience/scopes обязательны; refresh rotation,
  session/device revoke и серверная проверка актуальной membership.
- Tokens хранятся в системном secure storage клиента; provider keys и общий owner
  token на телефон не попадают. Device ID сам по себе не является auth.
- Browser flow отдельно: точные CORS origins; cookie/BFF требует SameSite/CSRF,
  mobile bearer flow не переносит автоматически эти правила на браузер.
- TLS обязателен на внешней surface. Certificate lifecycle, rate limits,
  request/output bounds и audit проверяются как часть deployment.
- Пользователь не может объявить свой workspace, owner или connector capability
  через свободное поле запроса; write scopes перепроверяются перед dispatch.
- Разделение admin/client/connector/MCP credentials и целей tokens обязательно.
  API token для своего device может дать только права выдавшего actor.

Для auth design ориентир — [OAuth 2.0 Security Best Current Practice, RFC 9700](https://www.rfc-editor.org/rfc/rfc9700).
OIDC/OAuth здесь — proposal протокола, не уже написанный identity server.
Per-user API не считается готовым до отрицательных cross-account tests.

## Асинхронность и повтор после обрыва

POST создания Run/job возвращает persisted operation ID; polling доступен
независимо от соединения клиента. Idempotency-Key scoped actor/workspace/action,
связан с hash request и TTL: тот же ключ+тело возвращает ту же операцию,
тот же ключ+другое тело даёт conflict. Это защита повторного client request,
не гарантия exactly-once внешнего prompt.

SSE — стартовый вариант для foreground events, с opaque cursor и bounded
replay; потерянный cursor ведёт к snapshot, не воспроизведению команд. WebSocket
добавляется только при подтверждённой потребности. Scope проверяется и при
подписке, и после revoke; pagination/event cursors не раскрывают чужие объекты.
Неизвестный исход отправки остаётся unknown с сохранённой provenance.

Approval response привязан к actor, request/resource ID, revision/epoch и
deadline; одноразовый. Не копировать вечный CLI watcher lease в mobile payload.
Если приложение ушло в background или offline, действуют серверные deadline и
отказ, а не implicit allow. Job continue/cancel зависит от user action и backend
capability; отключение клиента само по себе не даёт права повторить prompt.

## Мобильное приложение (P4.14)

Первый возможный объём: подключение к dispatcher URL, вход, список PCs/agents,
диалоги/результаты, start/cancel, сценарии, approvals и run history.
Push — отдельная опциональная интеграция: минимальные IDs/тип события, без prompt,
answer, credential или approval token; клиент после входа перечитывает authoritative
state. Нажатие push не является одобрением. Background execution платформы не
используется как scheduler диспетчера. Offline можно смотреть разрешённый cache;
отложенные mutations требуют idempotency/revision/expiry и явного дизайна.

PWA/Web UI может проверить UX до native app, но не заменяет решение о native.
Выбор OS/framework, push provider, secure storage и app distribution делается
по подтверждённому спросу и требованиям, а не сейчас.

## Этапы и приёмка

1. Вместе с P4.10 определить actor/workspace scopes и domain service boundary.
2. Draft OpenAPI v1, threat/permission tests и общий error/idempotency contract.
3. Device/auth lifecycle + scoped dialogue/job/approval/event endpoints.
4. Web/CLI test client и проверка reconnect/expired session/revoke.
5. P4.11 scenarios/runs endpoints; mobile только при выполненном триггере P4.14.

Автотесты: два пользователя, чужие/удалённые IDs, forged scope, stale approvals,
duplicate POST, lost response, token expiry/rotation/revoke, reconnect/replay,
CORS/CSRF при browser flow, bounds, backward compatibility, path prefix.
Живые: внешний TLS домен, отдельные user accounts/devices, logout/revoke,
background/resume и notification без утечек. Owner API при этом не раскрывается.

Открыто: identity provider и первоначальная регистрация/link Telegram; opaque
tokens или signed access tokens; TTL/quotas; browser BFF; диапазон public v1;
нужен ли personal client API до полного multi-user. До ответа это варианты,
не утверждённые настройки или доступные команды.
