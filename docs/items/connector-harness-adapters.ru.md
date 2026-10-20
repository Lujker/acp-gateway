# P4.13 — адаптеры других harness для коннектора

**Статус:** OPEN — исследование по запросу владельца 2026-10-13.
Официальные интерфейсы проверены; адаптеры Codex/Claude Code/Gemini/OpenCode
в ACP Gateway не реализованы и локальными model calls не проверены.
P4.9 сохраняет отдельный Goose/platform трек; P4.7 — общий stdio lifecycle.
P4.13 добавляет конкретные native protocol adapters, не объявляет всё ACP.

## Что означает «подключить агента»

| Режим | Что требуется доказать отдельно |
|---|---|
| Managed | Connector запускает собственный процесс/сервер и владеет lifecycle |
| External endpoint | Пользователь запускает документированный local server, connector проверяет identity/auth |
| Resume owned | Продолжение своего сохранённого dialogue по точному ID и проверенным ограничениям |
| Live attach / shared UI | Привязка уже работающего UI/IDE session, единственный writer и approvals ownership |

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
