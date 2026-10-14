# P4.15/P4.16 — удалённое обновление коннекторов и управление ПК

**Статус:** OPEN — будущие задачи по запросу владельца 2026-10-14.
Реализации remote update/host control сейчас нет; локальный acpgw update и
существующий maintenance-контракт — возможная основа, а не готовая remote функция.

## Запрос владельца

Из dispatcher управлять подключёнными коннекторами, обновлять их удалённо и
выполнять базовые действия на компьютере, где они работают. Всё должно быть
доступно из Telegram-бота через понятные кнопки. Это задачи на будущее;
в текущем проходе выполняется независимое ревью и устранение дефектов.

## P4.15 — удалённое обновление

- Показать computer/connector version, manager/source, capabilities и доступную
  версию. Выбрать конкретный компьютер и точную версию/канал обновления.
- Общий application service dispatcher, не логика только внутри Telegram handler.
  Typed control request с operation ID, authenticated actor, точным connector
  identity/generation и deadline; connector проверяет локальное разрешение.
- До обновления проверить совместимость, проверяемый источник пакета, права,
  свободное место и idle/drain. Не обрывать активные задания/ожидающие approvals.
- Updater живёт отдельно от обновляемого процесса: сначала durable intent/ACK,
  затем штатный package upgrade, restart, health check и reconnect с результатом.
  Сохранить YAML/env, enrollment key, data и service настройки.
- Состояния queued/preparing/updating/reconnecting/succeeded/failed/unknown;
  отсутствие соединения не равно успеху/ошибке. Повтор кнопки или reconnect не
  повторяет неизвестную установку; status/reconcile читают существующую операцию.
- Rollback к известной версии с явными границами совместимости схемы/данных;
  не обещать recovery, если потеряны права, сеть или updater.
- Один ПК, затем выбор нескольких с ограниченной параллельностью. Offline ПК
  не получать скрытое задание на будущее без отдельного решения владельца.

## P4.16 — базовое управление компьютером

Кандидаты первого этапа: состояние ОС/uptime/connector/managed agent,
диагностика соединения, bounded/redacted logs, restart connector и принадлежащего
ему agent. Hermes Desktop — внешний процесс пользователя: владение и способ
его перезапуска требуют отдельного контракта; чужие процессы не останавливать.

Sleep/reboot/shutdown — возможный следующий набор, с явным target в подтверждении,
локальным opt-in, проверкой активных работ и различением accepted/completed/unknown.
Уснувший/выключенный ПК сам не сможет принять wake request: Wake-on-LAN возможен
только после отдельного выбора подходящего всегда включённого узла/сетевой схемы.
Произвольный remote shell, произвольные файлы и скрытое elevation не входят в
базовый scope. Исполнение доступных операций ограничено локальными capabilities.

## Telegram и авторизация

Панель компьютеров: имя, версия, online/offline, операции, текущий результат.
Кнопки привязаны к owner/chat/message, computer identity/generation и operation ID;
устаревшие/чужие кнопки отказывают. Подтверждение reboot/update показывает конкретный
ПК и действие. Отдельный аудит actor/target/version/result без секретов.

Сначала существующий single-owner deployment. До общего multi-user выпуска
нужны P4.10 workspace/connector scopes и сквозная application authorization.
Человеческие management-команды отделены от agent tool approvals: режим /mode auto
не даёт модели права обновлять пакет, перезагружать хост или выдавать новые scopes.

## Критерии приёмки

В изолированных Linux/Windows installations проверить install→remote update,
сохранность config/key/data/service, busy refusal, health failure/rollback,
повторные клики, foreign/stale targets, потерю ACK/соединения на каждом этапе,
read-only reconcile без replay, revoke и неподдерживаемые capabilities.
Затем владелец проверяет два физических ПК, Telegram UX и reboot/logon/network.

## Открытые решения перед реализацией

Точный MVP host actions, stable/RC update policy и unattended opt-in, способ
обновления file/Git installations, privilege model/способ запуска независимого
updater и ownership внешнего Desktop. Эти вопросы не блокируют запись задачи.
