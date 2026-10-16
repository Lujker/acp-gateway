# Приёмка P4.8.5: VPS → два Windows Hermes → Telegram

> Решение владельца 2026-10-16: оставшиеся приёмочные проверки предварительно
> закрыты. Использовать этот список при сообщениях о проблемах, не как блокер
> текущей разработки. macOS коннектор владелец проверит отдельно позже.


Уточнение 2026-10-13: основной способ поставки P4.8 — uv tool либо Git-копия
со скриптами. Этот Docker runbook сохраняется как дополнительный вариант.
Основная инструкция: [Linux VPS без Docker](vps-native-hermes.ru.md).
Актуальные доработки и отдельная матрица установки/проверок агентом и пользователем:
[план поставки P4.8](../items/p48-delivery-plan.ru.md).

Это следующий живой этап после локальной Windows-приёмки. На VPS работает
диспетчер, на home/work — нативные Windows connectors под пользователем уже
запущенного Hermes Desktop. Telegram создаёт отдельные диалоги диспетчера
в этих backend. Пользовательские диалоги Desktop не импортируются.

Использовать актуальную development-сборку P4.8, а не опубликованный пакет
старой версии. Прежний `dist/p4.8-preview` — исторический результат, не включающий
все последующие изменения; перед живой приёмкой нужна новая проверенная сборка
с locked requirements и SHA256SUMS. Развёртывание ещё не выполнено.

## Подготовка VPS

Из проверенного checkout выполнить `python3 deploy/docker/prepare.py`.
Подготовка сохраняет существующие runtime-файлы. В
`deploy/docker/runtime/gateway.yaml` оставить data_dir, gateway, connector,
policy и logging из шаблона, заменить только список agents:

```yaml
agents:
  - alias: hermes
    kind: hermes
    backend: connector
    computer_id: home
    title: Home Hermes
    default_cwd: 'C:\Users\HOME_USER\work'
  - alias: hermes
    kind: hermes
    backend: connector
    computer_id: work
    title: Work Hermes
    default_cwd: 'C:\Users\WORK_USER\work'
```

Указать существующие рабочие каталоги каждого хоста. Включить Telegram,
внести свой numeric user ID в allowed_user_ids, а bot token — только в
`runtime/gateway.env`. Другой процесс не должен опрашивать тот же bot.
Hermes credentials на VPS не копируются.

Сразу подготовить WSS: DNS, доверенный сертификат в `runtime/tls/`, свой домен
в `runtime/nginx.conf`. Использовать `compose.yaml` + `compose.nginx.yaml`.
Инструкции для сертификата, занятого порта 443 и nginx находятся в
[основном runbook, раздел 5](vps-acceptance.ru.md#5-второй-этап-домен-и-собственный-nginx).
Предварительный plaintext/IP этап для этой приёмки не требуется.

Из `deploy/docker`:

```bash
docker compose -f compose.yaml -f compose.nginx.yaml build gateway
docker compose -f compose.yaml -f compose.nginx.yaml run --rm gateway config check
docker compose -f compose.yaml -f compose.nginx.yaml run --rm --no-deps nginx nginx -t
docker compose -f compose.yaml -f compose.nginx.yaml up -d
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers enroll home --name 'Home computer' --token-file /data/enrollment/home.key
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers enroll work --name 'Work computer' --token-file /data/enrollment/work.key
```

Передать каждому ПК только его key защищённым каналом. Проверить приватные
ACL локальных config/env/key/data/log по [Windows runbook](windows-connector.md).

## Подключение каждого Windows-хоста

Установить проверенный wheel в отдельную нативную Windows установку.
Использовать [setup Hermes](hermes-attach.md): observer должен увидеть
desktop-serve и нужный профиль без запроса к модели. Локальный config содержит
`alias: hermes`, `kind: hermes`, `backend: hermes`, свой default_cwd и
`hermes.role: desktop-serve`, `hermes.profile: default` либо выбранный профиль.
Профиль и cwd должны совпадать с выбранным маршрутом VPS. Указать постоянные
data_dir/logging.file; рядом сохранить приватный computer.env (может быть пустым).

Сначала запустить в foreground (на work заменить computer-id и key):

```powershell
acpgw --config C:\private\computer.yaml --env-file C:\private\computer.env config check
acpgw --config C:\private\computer.yaml --env-file C:\private\computer.env doctor --live
acpgw --config C:\private\computer.yaml --env-file C:\private\computer.env connector --dispatcher-url wss://gateway.example.com/acpgw/connect --computer-id home --token-file C:\private\home.key
```

На VPS `computers status` должен показать два разных online computer ID;
инициализация агента происходит при первом запросе. После успешной foreground
приёмки остановить его и установить/включить Windows logon task по runbook.

## Проверки и ожидаемое поведение

1. `/agent` показывает home/hermes и work/hermes. На каждом маршруте выполнить
   `/new`, задать разные безопасные кодовые слова, затем проверить их раздельно.
   Desktop продолжает работать; его исходные диалоги не изменены.
2. Проверить `/sessions`: при запросе busy, после ответа attached. Выбор другой
   вкладки Desktop не меняет адресат запросов Telegram.
3. Попросить безопасное tool-действие: сначала reject, затем новое действие
   allow once. Карточка указывает нужный маршрут; отказанное действие не исполняется.
   При истечении срока approval повторного решения/автоматического allow нет.
4. `/stop` не должен вызывать unsafe Hermes interrupt: остановка текущего
   Hermes turn ограничена контрактом upstream и не считается подтверждённой.
   Не использовать долгую/опасную команду для этого теста.
5. Разорвать только сеть work → VPS после завершения ответа: home отвечает
   независимо; живой work connector переподключается и продолжает тот же новый
   managed dialogue с прежним кодовым словом. Отдельно перезапустить процесс
   work connector: его старый handle уже недоступен, выбор остаётся; новый чат
   создаётся только явным `/new`. Обрыв во время запроса не даёт replay.
6. Перезапустить только Gateway при живых connector/Hermes и завершённых ходах:
   история и активный выбор сохраняются, тот же managed chat продолжает прежнее
   кодовое слово. Не должно быть нового session.create или Hermes resume.
   Для старых restore=disabled bindings остаётся прежний отказ и явный `/new`.
7. Проверить отзыв/ротацию computer key, независимость второго ПК, idle WSS,
   sleep/wake и смену сети. Затем проверить logon после настоящего reboot.
   Физические действия выполнять отдельным согласованным сеансом приёмки.

Записать версии Windows/Python/Hermes, hash wheel, маршруты, результаты,
job IDs и отсутствие оставшихся тестовых задач. `/result JOB_ID` позволяет
прочитать сохранённый ответ; `/approvals` — увидеть ожидающие карточки.
Отсутствие ответа при сетевом обрыве не означает, что действие не выполнилось.
Live Goose и Linux/WSL/macOS относятся к отдельному треку расширения P4.9.
