# Приёмка: VPS, два компьютера и Telegram

[English version](vps-acceptance.md)

Инструкция для первого рабочего развёртывания: **Gateway** работает на VPS
в Docker; на каждом компьютере с Linux/WSL работает исходящий **коннектор**,
а Goose запускается локально на обоих компьютерах. Сначала проверяем соединение
напрямую по IP и порту, затем переходим на
`wss://gateway.example.com/acpgw/connect` через собственный nginx.
Проверка совместимости с Hermes как ACP-агентом будет отдельным этапом.

```text
Telegram ──> Gateway на VPS ──> home/goose
                         └──> work/goose
                    ▲
    исходящие коннекторы с каждого компьютера
```

## 1. Подготовка репозитория на VPS

Склонируйте репозиторий в отдельный каталог, например `~/acp-gateway`.
Потребуются Docker Engine с Compose v2+, git и Python 3 для небольшого скрипта
подготовки. Python и зависимости приложения уже входят в образ.

Из каталога репозитория ACP Gateway:

```bash
python3 deploy/docker/prepare.py
cd deploy/docker
```

Скрипт создаёт приватные `runtime/` (настройки и токены), `state/` (SQLite,
отпечатки сертификатов и экспортированные ключи регистрации) и `.env` (UID/GID
для Compose). Существующие файлы сохраняются. Запускайте скрипт от пользователя,
который будет владельцем развёртывания: контейнер использует его UID/GID.
Эти файлы исключены из Git и контекста сборки образа. Храните их на файловой
системе Linux, а не в каталоге, смонтированном из Windows.

Отредактируйте `runtime/gateway.yaml`:

- Укажите **реальный абсолютный рабочий каталог Goose** отдельно для `home`
  и `work`. Он должен существовать на соответствующем компьютере.
- Для первой приёмки сохраните маршруты `home/goose` и `work/goose`,
  `session_mode: approve` и `connector.connect_path: /acpgw/connect`.
- Установите `telegram.enabled: true` и
  `allowed_user_ids: [ВАШ_ЧИСЛОВОЙ_ID_ПОЛЬЗОВАТЕЛЯ]`.

В `runtime/gateway.env` задайте `TELEGRAM_BOT_TOKEN` отдельного бота.
Токены owner API и MCP сгенерированы независимо; храните их в секрете.
Секреты Goose на VPS не нужны. Опрос одного бота должен выполнять только один
процесс: перед запуском остановите тестовый Gateway, использующий тот же токен.
Существующий webhook нужно удалить явно; см. [настройку Telegram](telegram.md).

Gateway использует отдельный Compose-проект `acpgw`, собственную Docker-сеть,
SQLite и ограничения ресурсов. Базовая конфигурация не публикует порты хоста.
Существующий reverse proxy или сеть другого приложения ей не нужны.
Скрипт также создаёт `runtime/nginx.conf` и `runtime/tls/` для второго этапа.

```bash
docker compose build
docker compose run --rm gateway config check
```

## 2. Первый этап: IP и отдельный порт

Добавьте в `deploy/docker/.env`:

```dotenv
ACPGW_INGRESS_BIND=0.0.0.0
ACPGW_CONNECTOR_PORT=18766
```

Убедитесь, что порт свободен: `ss -ltn sport = :18766`.
Дополнительный Compose-файл публикует **только вход коннекторов**;
owner API и MCP остаются внутри контейнера:

```bash
docker compose -f compose.yaml -f compose.direct.yaml config --quiet
docker compose -f compose.yaml -f compose.direct.yaml up -d
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env status
docker compose logs --tail 50 gateway
```

Разрешите TCP 18766 с двух тестовых компьютеров в действующей политике firewall,
включая правила пересылки Docker, если они используются. Не сбрасывайте firewall
и не меняйте посторонние службы ради открытия порта. Первый адрес подключения:

```text
ws://VPS_IP:18766/acpgw/connect
```

На первом этапе используется незашифрованный тестовый транспорт: ключи
компьютеров, запросы и ответы передаются по этому соединению открыто.
Используйте безобидные тестовые запросы; на этапе с доменом соединение заменяется
на WSS.

Зарегистрируйте на VPS два постоянных идентификатора. Файлы ключей должны быть новыми:

```bash
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers enroll home --name "Home computer" --token-file /data/enrollment/home.key
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers enroll work --name "Work computer" --token-file /data/enrollment/work.key
```

Безопасно перенесите `state/enrollment/home.key` на домашний компьютер,
а `work.key` — на рабочий, например по существующему SSH/SCP-соединению.
Каждому компьютеру нужен только его собственный ключ. Владелец файла должен
совпадать с локальным пользователем, права файла — `0600`, каталога — `0700`.
Символические ссылки не принимаются.

## 3. Запуск Goose и коннекторов на обоих компьютерах

Для этой приёмки используйте Linux/WSL на обоих компьютерах. Склонируйте
репозиторий, выполните `uv sync --frozen` и создайте приватный каталог настроек:

```bash
install -d -m 700 ~/.config/acp-gateway-connector
cp deploy/docker/computer.example.yaml ~/.config/acp-gateway-connector/computer.yaml
```

В `url` укажите **уже работающий локальный сервер Goose**, его точный TLS-отпечаток,
а также те же `default_cwd` и `session_mode: approve`, что заданы на VPS для
соответствующего маршрута. Шаблон использует `wss://127.0.0.1:3284/acp`;
замените порт на фактический. Если Goose уже работает как служба, используйте
её. [Инструкция Goose](work-goose.md) описывает серверный режим.

Создайте `~/.config/acp-gateway-connector/computer.env` с правами `0600`
и строкой `LOCAL_GOOSE_SECRET=...`, содержащей секрет локального сервера.
Поместите рядом перенесённый ключ регистрации как `computer.key`, также
с правами `0600`. Токены owner API, MCP и Telegram на коннекторе не нужны.

На домашнем компьютере, из каталога репозитория:

```bash
uv run acpgw --config ~/.config/acp-gateway-connector/computer.yaml --env-file ~/.config/acp-gateway-connector/computer.env config check
uv run acpgw --config ~/.config/acp-gateway-connector/computer.yaml --env-file ~/.config/acp-gateway-connector/computer.env connector --dispatcher-url ws://VPS_IP:18766/acpgw/connect --computer-id home --token-file ~/.config/acp-gateway-connector/computer.key
```

На рабочем компьютере выполните те же команды с `--computer-id work`.
До завершения приёмки оставьте коннекторы в терминалах. Второй коннектор
с тем же идентификатором компьютера заменяет первый, поэтому идентификаторы
двух компьютеров должны различаться.

На VPS:

```bash
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers status
```

Оба компьютера должны быть online и объявлять агент `goose`.
Вначале допустимо состояние `agent_not_initialized`: ACP-соединение открывается
при первом запросе. После успешного запроса ожидается `agent_ready: true`.
Само состояние `online` ещё не подтверждает правильность секрета, каталога
и режима Goose.

## 4. Приёмка через Telegram

Откройте личный чат с отдельным ботом. Делайте паузу в несколько секунд между
командами: канал допускает пять сообщений за десять секунд.

1. Отправьте `/start`, `/agent`. В списке должны быть `home/goose` и `work/goose`.
2. Отправьте `/agent home/goose`, `/new`, затем: «Запомни кодовое слово AMBER.
   Ответь OK».
3. Отправьте `/agent work/goose`, `/new`, затем: «Запомни кодовое слово COBALT.
   Ответь OK».
4. Переключайтесь через `/agent ...` и спрашивайте кодовое слово. На домашнем
   маршруте ожидается AMBER, на рабочем — COBALT. `/sessions` показывает
   сессии только выбранного маршрута.
5. На каждом маршруте попросите безобидное действие инструментом:
   «Выполни ровно эту команду: `echo ACPGW_ACCEPTANCE`». Сначала отклоните
   карточку, затем повторите запрос и разрешите однократно. Карточка должна
   указывать правильный маршрут `home/goose` или `work/goose`. Отклонённое
   действие не должно выполниться. Результат разрешённого проверьте на нужном
   компьютере.
6. Попросите выполнить `sleep 30`, подтвердите, затем отправьте `/stop`.
   Задание Gateway должно отмениться. Остановка уже запущенного дочернего
   процесса зависит от Goose; проверяйте её отдельно на компьютере.
7. Остановите рабочий коннектор через Ctrl+C. Запрос к рабочему маршруту должен
   завершиться ошибкой, а домашний должен продолжить работать. Запустите рабочий
   коннектор и снова спросите кодовое слово. Восстановление требует поддержки
   `session/load` в Goose и сохранённых сессий.
8. Когда активных заданий нет, перезапустите **только этот Gateway**:
   `docker compose restart gateway`. Проверьте переподключение коннекторов,
   `/agent`, `/sessions` и сохранённые кодовые слова.

При проверке обрывов сохраняйте идентификатор из `Working: JOB_ID`.
Команда `/result JOB_ID` возвращает сохранённый результат, если сообщение
не доставлено; `/approvals` повторно показывает ожидающие карточки.
Не повторяйте автоматически действие инструментом после сетевого сбоя:
оно могло уже выполниться. Запросы к отключённым компьютерам не ставятся
в очередь; надёжное восстановление неопределённого результата ещё предстоит
реализовать. Перезапуск Gateway не продолжает автоматически выполнявшиеся
задания. Финальные ответы приходят отдельными сообщениями; потоковое
редактирование сообщений не является условием этой приёмки.

## 5. Второй этап: домен и собственный nginx

Направьте DNS-запись своего домена на IP VPS. `gateway.example.com` — пример:
замените его своим доменом в `runtime/nginx.conf` и адресах коннекторов.
Получите доверенный TLS-сертификат у своего поставщика сертификатов или через
ACME-клиент. Выпуск и продление сертификатов организуются отдельно от этого стека.
Поместите полную цепочку в `runtime/tls/fullchain.pem`, а закрытый ключ —
в `runtime/tls/privkey.pem`. Ограничьте доступ к ключу: `chmod 600`.

Дополнительный [Compose-файл nginx](../../deploy/docker/compose.nginx.yaml)
запускает собственный nginx в сети проекта. Он публикует HTTPS-порт 443;
HTTP-порт этой конфигурации не требуется. Проверьте, что 443 свободен:
`ss -ltn sport = :443`. Если он занят другой службой, укажите свободный порт,
например `ACPGW_HTTPS_PORT=18443` в `.env`, и добавьте `:18443` в адреса
коннекторов. Не останавливайте другую службу ради освобождения её порта.
Разрешите выбранный HTTPS-порт в политике firewall.

[Конфигурация nginx](../../deploy/docker/nginx.conf.example) подключает
[точный location коннекторов](../../deploy/docker/nginx-location.conf.example).
Он сохраняет путь `/acpgw/connect`, заголовки WebSocket и авторизации.
Docker DNS разрешается во время запроса, поэтому отсутствие Gateway не мешает
nginx запуститься. Owner API и MCP не проксируются; другие пути возвращают 404.

Оставьте прямой тестовый порт доступным на время проверки HTTPS:

```bash
docker compose -f compose.yaml -f compose.direct.yaml -f compose.nginx.yaml config --quiet
docker compose -f compose.yaml -f compose.direct.yaml -f compose.nginx.yaml run --rm --no-deps nginx nginx -t
docker compose -f compose.yaml -f compose.direct.yaml -f compose.nginx.yaml up -d
docker compose -f compose.yaml -f compose.direct.yaml -f compose.nginx.yaml logs --tail 50 nginx
```

После продления сертификата или изменения настроек проверьте и перезагрузите nginx:

Редактируйте смонтированный `runtime/nginx.conf` на месте. Если редактор заменяет
файл новым, пересоздайте контейнер nginx с теми же Compose-файлами и командой
`up -d --force-recreate nginx`, чтобы перед проверкой подключить новый файл.

```bash
docker compose -f compose.yaml -f compose.nginx.yaml exec nginx nginx -t
docker compose -f compose.yaml -f compose.nginx.yaml exec nginx nginx -s reload
```

Перезапустите каждый коннектор в терминале, изменив только адрес на:

```text
wss://gateway.example.com/acpgw/connect
```

Для публичного сертификата домена используется обычная проверка доверенного
центра сертификации и имени хоста; аргумент с отпечатком коннектору не нужен.
Повторите Telegram-сценарии, включая простой соединения дольше двух минут
и отключение одного компьютера. Оба компьютера должны оставаться online
в логах VPS. Обычный GET из браузера не подтверждает работу авторизованного
WebSocket/ACP-соединения.

После успешной работы обоих компьютеров через WSS уберите временный прямой порт:

```bash
# Из deploy/docker в репозитории ACP Gateway, когда нет активных заданий:
docker compose -f compose.yaml -f compose.nginx.yaml up -d
docker compose -f compose.yaml -f compose.nginx.yaml ps
```

Удалите временное разрешение порта 18766 через действующий механизм управления
firewall. Пересоздание ненадолго отключит коннекторы; они должны подключиться
снова через WSS. Этот Compose-проект управляет только своими Gateway, nginx
и сетью. После приёмки установите локальные коннекторы как службы по
[инструкции](service.md#separate-computer-connector-service).

## Автоматическая проверка подготовки

Соберите образ локально и запустите отдельный smoke-тест:

```bash
docker build -t acpgw:local .
uv run python scripts/smoke_docker.py --image acpgw:local
```

Он проверяет настоящий Docker runtime и Compose-конфигурацию, прямое соединение
по IP и порту, два независимых имитатора Goose, точный путь nginx, WSS,
дополнительный L4-прокси с PROXY protocol, неверные ключи, изоляцию отключения
и загрузку сессий. Удаляются только временные контейнеры и сеть самого теста;
настоящий бот и рабочие службы не используются. Автотесты Telegram используют
настоящие типы aiogram и имитацию сессии Bot API. Доставка через настоящий
Telegram и физические компьютеры проверяются во время живой приёмки.

Директивы WebSocket соответствуют
[официальной документации nginx](https://nginx.org/en/docs/http/websocket.html),
а сеть проекта —
[семантике сетей Docker Compose](https://docs.docker.com/reference/compose-file/networks/).
