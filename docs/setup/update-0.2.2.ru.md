# Обновление до 0.2.2

Сначала обновите диспетчер, затем коннекторы по одному. Завершите активные
задания и подтверждения. Данные, ключи, ID компьютеров и настройки сохраняются;
новый SQLite schema 9 только добавляет таблицы pairing.

## uv-диспетчер (Linux/WSL)

Если 0.2.1 установлен как managed uv service, используйте существующий явный
путь обновления:

```bash
acpgw update --version 0.2.2 --with-services --service-role gateway
acpgw update --status
```

Дождитесь `succeeded`, затем проверьте:

```bash
acpgw version --json
acpgw --config /path/to/config.yaml --env-file /path/to/private.env status
```

После запуска 0.2.2 updater выбранной роли готовится автоматически. В
`config.yaml` не нужно добавлять `updates.enabled: true`: это значение теперь по
умолчанию. Если там явно стоит `false`, оно сохраняется как opt-out.

## Windows-коннектор (uv)

В PowerShell под пользователем, который запускает задачу коннектора:

```powershell
$Cfg = Join-Path $env:LOCALAPPDATA 'acpgw-connector'
$Acpgw = Join-Path ((uv tool dir --bin).Trim()) 'acpgw.exe'
$G = @('--config', "$Cfg\config.yaml", '--env-file', "$Cfg\.env")
& $Acpgw @G update --version 0.2.2 --with-services --service-role connector
& $Acpgw update --status
```

На Windows exit 0 означает постановку в очередь. Не переустанавливайте задачу,
пока статус `queued`/`running`. После `succeeded`:

```powershell
& $Acpgw version --json
& $Acpgw @G config check
& $Acpgw @G service --role connector status
```

Повторите для каждого ПК. Переподключение и повторное enrollment не нужны.
Существующие ключи продолжают работать; новый pairing нужен только для новых ПК
или явной однократной миграции старых.

## Docker-диспетчер

Опубликованный образ: `ghcr.io/lujker/acp-gateway:0.2.2`. Порядок такой же, как в
[инструкции 0.2.1](update-0.2.1.ru.md): остановить gateway, сохранить `runtime/`,
`state/`, `.env` и Compose-файлы, закрепить новый image в
`compose.release.yaml`, выполнить `compose up` без сборки, проверить `version`,
health и подключение компьютеров. Не запускайте `acpgw update` внутри контейнера.

## Pairing нового ПК

В обычном пути больше не нужны копирование ключа, YAML и fingerprint:

1. На диспетчере один раз выполните `acpgw owner bootstrap --telegram-id ...`
   или локальный bootstrap без Telegram ID.
2. На ПК запустите `acpgw connect`, введите адрес диспетчера, owner ID и имя.
3. Сверьте request/verification/target и подтвердите запрос через `/pairing` в
   Telegram или `acpgw pairing approve ...`.

Старые ПК остаются привязанными. Явная команда
`acpgw pairing activate-legacy-rebind --confirm` — отдельный однократный переход:
она отзывает authority непривязанных legacy-компьютеров. Не выполняйте её, пока
не готовы перепривязать такие ПК через `acpgw connect`.

## Проверка после обновления

```bash
acpgw version --json
acpgw update --check
acpgw --config /path/to/config.yaml --env-file /path/to/private.env status
```

`acpgw update --check` должен показать установленную `0.2.2` и отсутствие более
новой stable-версии. Обновления диспетчера/коннекторов по-прежнему не обновляют
Hermes, Goose, Codex или другие harness-бинарники.
