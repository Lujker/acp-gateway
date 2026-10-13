# Linux VPS без Docker: dispatcher, Telegram и два Windows Hermes

Основной маршрут P4.8: один dispatcher в uv tool на VPS, исходящие WSS-подключения
двух Windows connectors, отдельные диалоги в уже запущенных Hermes Desktop.
Нужна актуальная сборка P4.8; опубликованный 0.1.0 и прежний preview не покрывают
все текущие изменения. Этот документ — сценарий развёртывания, а не отчёт о его
выполнении. [Матрица P4.8.5](../items/p48-delivery-plan.ru.md#отдельный-этап-установка-и-приёмка-p485)
разделяет проверки агента и действия пользователя.

## 1. Подготовить хост и установить пакет

На VPS нужны отдельный непривилегированный Linux-пользователь, uv, systemd с user
manager, постоянный диск и исходящий HTTPS к Telegram. Пользователь предоставляет
SSH-доступ, домен/DNS, сертификат и возможность настройки firewall/lingering.
На Windows нужны uv и Hermes Desktop под тем же пользователем, что connector.
Ключи Hermes остаются на ПК; VPS получает только отдельные computer credentials.

Из проверенного Git checkout на VPS:

```bash
sh scripts/install.sh --config-dir "$HOME/.config/acp-gateway" --role server --interactive
uv tool update-shell
```

Вместо Git можно установить предоставленный проверенный wheel с его locked
requirements, затем отдельно запустить мастер:

```bash
uv tool install --python 3.12 --constraints /absolute/path/requirements.lock.txt /absolute/path/acp_gateway-VERSION-py3-none-any.whl
acpgw setup --config-dir "$HOME/.config/acp-gateway" --role server --interactive
```

`VERSION` и пути заменить фактическими. Для последующих команд ниже `acpgw`
должен быть в PATH; при использовании checkout заменять его на
`sh /absolute/path/acp-gateway/scripts/run.sh`.

## 2. Настроить server, Telegram и маршруты

В мастере выбрать server, connector listener `127.0.0.1:18766`, URL path
`/acpgw/connect`, пустой TLS certificate при TLS termination на proxy. Локальный
owner API/MCP оставить на `127.0.0.1:8765`. Добавить два connector profile:

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
connector:
  enabled: true
  host: 127.0.0.1
  port: 18766
  connect_path: /acpgw/connect
telegram:
  enabled: true
  allowed_user_ids: [123456789]
policy:
  approver_channels: [telegram]
```

Это фрагмент настроек: сохранить остальные поля, созданные мастером. Заменить
user ID и оба Windows cwd реальными значениями. Cwd должен существовать на
соответствующем ПК и совпадать с локальным профилем connector. Allowlist пока
означает доверенных владельцу бота пользователей; персональная изоляция аккаунтов
будет в P4.10. Для текущего сценария указать одного владельца.

Bot token вводить скрытым вводом мастера: он сохраняется в приватном `.env`.
Остановить другой polling-процесс этого bot и устранить прежний webhook, если
он использовался. Настройка и диагностика описаны в [Telegram](telegram.md).
Сохранить absolute `data_dir` вне checkout/uv tool; SQLite, enrollment и pins
должны переживать обновление. Для логов достаточно journal; при `logging.file`
выбрать постоянный absolute path и заранее создать приватный каталог.

В примерах далее функция явно выбирает одни и те же файлы для всех команд:

```bash
gateway_cli() {
  acpgw --config "$HOME/.config/acp-gateway/config.yaml" \
        --env-file "$HOME/.config/acp-gateway/.env" "$@"
}
gateway_cli config check
gateway_cli doctor
```

## 3. Предоставить WSS

На существующем HTTPS proxy направить только connector path к
`http://127.0.0.1:18766/acpgw/connect`, сохранив path и WebSocket upgrade.
Пример location внутри уже настроенного nginx HTTPS server с действующим
сертификатом для `gateway.example.com`:

```nginx
location = /acpgw/connect {
    proxy_pass http://127.0.0.1:18766;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

Проверить конфигурацию proxy перед reload. В firewall открыть его HTTPS-порт;
8765/18766 остаются локальными. Не направлять наружу owner API или MCP.
Итоговый URL обоих ПК: `wss://gateway.example.com/acpgw/connect`.

Альтернатива без proxy: `connector.host: 0.0.0.0`, `tls_cert` и `tls_key`
с absolute путями к читаемым службой сертификату/ключу. Тогда открыть только
выбранный TLS connector port, например 18766, и использовать
`wss://gateway.example.com:18766/acpgw/connect`. Публичный plaintext WS
для штатной эксплуатации не использовать. После обновления сертификата
перезапустить listener; получение/продление сертификата настраивается на хосте.

## 4. Постоянный запуск и enrollment

После сохранения config зарегистрировать user service, если мастер ещё не сделал
это. Не запускать одновременно foreground dispatcher и ту же службу.

```bash
gateway_cli service install
gateway_cli service enable
gateway_cli service status
gateway_cli status
gateway_cli service logs
```

Unit: `~/.config/systemd/user/acp-gateway.service` (при нестандартном
XDG_CONFIG_HOME — под ним). `enable` включает автозапуск и запускает сейчас.
Чтобы user manager запускался после reboot без SSH-login, администратор
настраивает lingering для пользователя dispatcher:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

Если нет user bus, подготовить user session на VPS по [service runbook](service.md).
Выход из SSH не должен останавливать службу; настоящий reboot остаётся отдельной
приёмкой. Приложению не нужны root-права.

Если мастер уже экспортировал home/work keys, использовать их. Иначе выполнить
enrollment один раз, пока dispatcher остановлен, затем запустить службу:

```bash
gateway_cli service stop
gateway_cli computers enroll home --name 'Home computer' --token-file "$HOME/.config/acp-gateway/enrollment/home.key"
gateway_cli computers enroll work --name 'Work computer' --token-file "$HOME/.config/acp-gateway/enrollment/work.key"
gateway_cli service start
gateway_cli computers status
```

Каталог enrollment должен существовать и быть приватным. Не выполнять повторную
выдачу для уже зарегистрированного computer: существующий key сохраняется,
замена выполняется явно через rotate. Каждому ПК передать только его key
защищённым каналом, вне чата и репозитория.

## 5. Подключить Windows home/work

На каждом ПК из native PowerShell установить актуальный пакет или выполнить
checkout installer с `--role connector --interactive`. Подробности:
[скрипты](distribution.md#checkout-scripts-linuxwsl-and-windows),
[Hermes discovery](hermes-attach.md), [Windows task](windows-connector.md).
Мастер выбирает существующий Hermes Desktop/profile, local alias `hermes`,
cwd этого ПК, WSS URL, home либо work computer ID и absolute path его key.
Сохранённый `setup.json` содержит launch recipe, без содержимого credential.

Мастер готовит постоянные data/log paths и новые private directories;
существующие небезопасные ACL нужно исправить явно по Windows runbook.
Мастер предлагает установку и включение task перед Save; при выборе этого
варианта отдельный install/enable ниже уже не нужен. При отказе можно сначала
проверить foreground, а затем зарегистрировать task вручную.
Затем с фактическими путями:

```powershell
acpgw --config C:\private\acpgw\config.yaml --env-file C:\private\acpgw\.env doctor --live
acpgw --config C:\private\acpgw\config.yaml --env-file C:\private\acpgw\.env start
# После проверки foreground остановить его через Ctrl+C.
acpgw --config C:\private\acpgw\config.yaml --env-file C:\private\acpgw\.env service --role connector install
acpgw service --role connector enable
acpgw service --role connector status
acpgw service --role connector logs
```

Install использует saved connection: повторно вводить URL/key не требуется.
Task работает после входа этого Windows-пользователя. Running task означает
живой процесс; VPS `computers status` подтверждает соединение, а `doctor --live`
и явный запрос проверяют Hermes. Connector Hermes не включает Telegram polling.

## 6. Отдельная приёмка и обновление

Проверки агента: config/doctor, оба online computer ID, status/logs, изолированная
установка и update/rollback, mock routing/approvals, отсутствие replay.
Действия пользователя: отправить реальные `/agent`, `/new`, сообщения и решения
approval; визуально сверить Desktop; выполнить reboot/sleep/network события.
Полный сценарий живого маршрута и ожидания reconnect:
[P4.8.5](vps-hermes-acceptance.ru.md#проверки-и-ожидаемое-поведение).
Idle VPS reconnect сохраняет chat при живых connector/Hermes. Restart connector
или потеря Hermes attachment пока не восстанавливает старый handle; active choice
сохраняется, новый диалог создаётся только явным `/new`.

До update завершить jobs/approvals. Для Linux systemd теперь доступен opt-in
coordinator с reservation, остановкой, проверкой пакета и восстановлением
enabled/running. Он обслуживает выбранную owned unit с теми же config/env;
другие процессы этой установки должны быть остановлены. Для checkout:

```bash
sh /absolute/path/acp-gateway/scripts/update.sh --config-dir "$HOME/.config/acp-gateway" --with-services
gateway_cli status
```

В текущих исходниках Windows coordinator также реализован. Для checkout:

```powershell
powershell -NoProfile -File .\scripts\update.ps1 --config-dir C:\private\acpgw --with-services
```

Он резервирует connector, останавливает owned task и восстанавливает прежние
enabled/running после завершения package helper. Для прямого CLI queued означает
только запуск операции; завершение проверить через `acpgw update --status`.
Полная приёмка установленного Windows uv-пакета с этим циклом ещё открыта.
Ручной вариант: `service --role connector disable`, update script без
`--with-services`, затем восстановить прежнее состояние задачи.
При изменении interpreter/launch definition переустановить owned service пока он
остановлен. Скрипт ждёт реального результата update. Config, keys и выбранная БД
сохраняются; мастер повторно не запускается. Ошибка health check запускает откат.
Последствия явного rollback базы и диагностика maintenance:
[distribution](distribution.md#internal-commands).
