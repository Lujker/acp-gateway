# Обновление до 0.2.3

Сначала обновите диспетчер, затем коннекторы по одному. Завершите активные
задания и подтверждения. Настройки, ключи, ID компьютеров и данные сохраняются.

> **База диспетчера переходит со schema 9 на 10.** Перед обновлением сохраните
> копию `gateway.db`. Откат на 0.2.2 возможен только с этой копией: старый образ
> или пакет не запускается на schema 10.

## Из Telegram

`/updates` → **Check versions** показывает установленные версии и доступную
stable `0.2.3`. Обновите диспетчер, дождитесь переподключения компьютеров, затем
обновите каждый коннектор отдельно. Этот путь работает для uv-служб Linux/Windows
и Docker-диспетчера с настроенным updater; ограничения — в
[удалённых обновлениях](remote-updates.md).

## uv-диспетчер (Linux/WSL)

```bash
acpgw update --version 0.2.3 --with-services --service-role gateway
acpgw update --status
```

Дождитесь `succeeded`, затем проверьте `acpgw version --json` и
`acpgw --config /path/to/config.yaml --env-file /path/to/private.env status`.

## Windows-коннектор (uv)

В PowerShell под пользователем, который запускает задачу коннектора:

```powershell
$Cfg = Join-Path $env:LOCALAPPDATA 'acpgw-connector'
$Acpgw = Join-Path ((uv tool dir --bin).Trim()) 'acpgw.exe'
$G = @('--config', "$Cfg\config.yaml", '--env-file', "$Cfg\.env")
& $Acpgw @G update --version 0.2.3 --with-services --service-role connector
& $Acpgw update --status
```

На Windows exit 0 означает постановку в очередь. Не переустанавливайте задачу,
пока статус `queued`/`running`. После `succeeded`:

```powershell
& $Acpgw version --json
& $Acpgw @G config check
& $Acpgw @G service --role connector status
```

Коннектор из Git checkout ([editable](windows-source.md)) обновляется
`git pull --ff-only` при остановленной задаче, затем задача запускается снова.

## Docker-диспетчер

Опубликованный образ: `ghcr.io/lujker/acp-gateway:0.2.3`. Порядок как в
[инструкции 0.2.1](update-0.2.1.ru.md): сохранить `runtime/`, `state/` (включая
копию `gateway.db`), `.env` и Compose-файлы, закрепить новый image в
`compose.release.yaml`, выполнить `compose up` без сборки, проверить `version`,
health и подключение компьютеров. Не запускайте `acpgw update` внутри контейнера.

## Новый ПК

Установите пакет и подключите ПК к диспетчеру через pairing, без копирования
ключей и fingerprint:

```sh
uv tool install --python 3.12 "acp-gateway==0.2.3"
uv tool update-shell
acpgw connect
```

Подтвердите запрос через `/pairing` в Telegram или `acpgw pairing approve ...`
на диспетчере. Подробно — [pairing коннектора](connector-pairing.md). Профили
Codex/Claude настраиваются по [Codex](codex-managed.md) и
[Claude Code](claude-code-managed.md); управление профилями из Telegram —
[agent profiles](telegram-agent-profiles.md).

## Проверка после обновления

```bash
acpgw version --json
acpgw update --check
```

`acpgw update --check` должен показать установленную `0.2.3` без более новой
stable-версии. Обновление не меняет Hermes, Goose, Codex, Claude Code и другие
harness-бинарники.
