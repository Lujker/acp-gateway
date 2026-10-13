# P4.8 — переход от разработки к установке и приёмке

Срез: 2026-10-13, frozen candidate 0.2.0rc1; development отдельно ниже. Статус всего P4.8: IN PROGRESS;
кодовая основа готова, P4.8.5 требует реальной установки и приёмки.
Владелец подтвердил доступность VPS адресов IP:port и domain/path, Telegram bot
и установленного native Hermes на текущем Windows-хосте. Первым проверяется
маршрут VPS → этот Windows; второй connector остаётся в матрице изоляции.

## Что завершено в исходниках

- Native Hermes discovery/identity/auth, dedicated driver, ACP bridge,
  relay и once/deny approvals; credentials остаются на хосте.
- Persistent logical dialogue/selection/binding; warm idle reconnect на том же
  live connector/Hermes. Offline/error не создаёт и не выбирает другой чат.
- Saved connector launch; CLI обеих ролей, Windows private paths/ACL,
  Scheduler setup/rollback, Linux systemd user и uv/Git install/run/update.
- Owned model show/list/set/confirmation, configuring/pending/unknown,
  projected catalogue, transport/policy/core/owner API.
- SQLite requested/observed/pending/unknown; intent и operation ID атомарны.
  Scoped defaults общие для CLI/Telegram single-owner/workspace и разделены
  по route/computer/alias/фактическому Hermes profile.
- CLI model show/list/state/set/default/reconcile и Telegram /model, /models,
  /model set/default/state/reconcile. Target/revision/route/confirmation guards.
- Exact model-outcome reconciliation по живому manager: terminal ACK одного
  operation ID + binding, без повторения mutation; затем свежий readback.
- Connector journal до 128 events/handle и 512 chars/answer chunk. Поздний prompt
  result записывается как evidence, не replay и не writer authority.
- Explicit connector-sessions list/journal/retire через private instance-fenced
  IPC. Только detached idle handle без model activity/permissions; upstream chat
  не удаляется, автоматического eviction нет.

Полный контракт: [model control](hermes-model-control.ru.md).
Архитектурные ограничения: [shared control](hermes-shared-control.ru.md).
План/матрица: [delivery](p48-delivery-plan.ru.md).
Практический порядок: [acceptance runbook](../setup/p48-acceptance.md).

## Ограничения при проверках

1. Dispatcher chats остаются для диспетчера; Desktop — reader. Эксклюзивная
   writer authority реального Hermes не доказана. Параллельные prompt/steer/
   cancel/approval из Desktop нарушают предположение единственного писателя.
2. Cold connector/Hermes restart, upstream session.resume и safe cancel
   не включены. Pending prompt loss остаётся quarantine, даже после позднего
   completion. Journal помогает проверить факт, но не выдаёт authority.
3. Accepted — ACK, не proof provider execution. Model.options — observation
   и может fallback к profile default без live agent. Deferred требует своего
   завершённого хода. Canonical mapping берётся только из ACK, не угадывается.
4. Model recovery снимает только точный known model-loss outcome того же
   live manager. Unknown upstream failure, missing/foreign outcome, cold restart
   и prompt-loss fence не снимаются. Новый чат — только явный выбор владельца.
5. Default создаётся без host RPC и не меняет Desktop profile/старые чаты.
   Ошибка/confirmation нового default сохраняет dialogue и gate перед prompt.
6. Model operation после dispatch не перенаправляется при смене selection.
   Использовать dialogue_id/revision/selection_changed из ответа.
7. Connector/Hermes автозапуск на Windows, реальные providers/Telegram,
   IP и domain/path, reboot/sleep/network проверяются в P4.8.5.
   Credentials и пользовательские службы в функциональной итерации не менялись.

## Проверенная база кандидата 0.2.0rc1

Полная WSL regression: **1152 passed / 28 skipped**, 183.75 s.
Skips: 23 native Scheduler opt-in и 5 installed-Hermes compatibility.
Ruff check/format: 220 files, clean. Source/tests во время полного прогона
не редактировались. JUnit: /tmp/acpgw-p48-final-source.xml.

Native source: **287 passed / 2 skipped**, 90.77 s.
Installed Linux wheel: **1152 passed / 28 skipped**, 150.18 s.
Installed native Windows wheel: **374 passed / 2 skipped**, 251.10 s;
все 23 native Scheduler cases выполнены. Native skips — POSIX permissions.
Кандидат: dist/p48-0.2.0rc1, source ref
3da8dffbd5d265c0b135e3aa1bd212e8e618c779; wheel/sdist/bundle, locked
requirements, checksums и JUnit/manifest evidence сохранены. Все 81 runtime/SQL
файлов wheel побайтно совпали с source ref. Candidate установленный probe
подтвердил существующий Hermes 0.21.6 readonly; prompts/config mutation не было.
Ничего не опубликовано. Старые 0.1.0/preview не содержат новые функции.
При checkout installation нужен штатный --refresh-package acp-gateway.
Goose/platform expansion остаётся P4.9, multi-user accounts — P4.10.

Самостоятельный этап владельца: [пошаговая установка](../setup/self-install-0.2.0rc1.ru.md).
Кандидат остаётся замороженным; последующая разработка не подменяет его wheel.

## Отдельная development-ветка после freeze rc1

HEAD реализации: f0961ca; версия 0.2.0rc2.dev0, schema 8. Это не пересобранный
кандидат и не новая опубликованная поставка. Последний сохранённый WSL JUnit:
1198 passed / 28 skipped, failures/errors=0; native source профиль 62 passed.
Подробный журнал: [P3.3](p3.3-reliability.ru.md); результаты при этой сверке
прочитаны из сохранённого отчёта, не выдаются за повторный full suite.

Готовы bounded periodic retention (audit opt-in), общий work admission limit,
persistent Telegram outbox с chunk ACK/digest/unknown fence, partial/final
status edits и дополнительные update/rollback recovery checks. Изолированные
wheel lifecycle прошли, включая настоящий переход rc1 schema 6→8.
Незавершённые delivery records после rollback требуют явного /result; runtime
unknown prompt не повторяется. Все 85 runtime/SQL файлов private development
wheel были сверены с source; artifact оставался в /tmp, не включён в rc1 bundle.

Будущие OPEN направления по запросу владельца:
[сценарии/manual+schedule](scenarios-and-schedules.ru.md),
[client Web API/mobile](client-api-and-mobile.ru.md),
[Codex/Claude и другие adapters](connector-harness-adapters.ru.md).
Это proposals, не новые команды/поддерживаемые backend. P4.10 ownership нужен
до shared user release; mobile P4.14 CONDITIONAL. P4.9 остаётся DEFERRED.

## Следующие действия

1. P4.8.5: fresh VPS uv/systemd dispatcher, bot allowlist/token на VPS; проверить
   IP:port и domain/path ingress, owner API на loopback и CLI через SSH.
2. Native Windows connector рядом с установленным Hermes; enrollment/doctor/
   CLI/service. Настроить per-user logon Hermes и connector после проверки
   фактического installed entry point, не запускать Desktop как SYSTEM.
3. Диалог/память/once approvals/model/default/reconcile в Telegram и CLI;
   controlled reconnect/network/sleep/reboot, package/service update/rollback.
4. Второй Windows connector: отдельный key/route/catalogue и изоляция.

Функциональные пункты закрыты как кодовая основа; кандидат собран и проверен.
Живая установка/приёмка ещё не проведена; исходный ref кандидата указан выше.
