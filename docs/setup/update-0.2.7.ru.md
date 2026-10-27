# Обновление до 0.2.7

Состояние по [журналу выпуска](../releases/0.2.7-publication.md) от 2026-10-26:
диспетчер 0.2.7, коннекторы 0.2.6. **Переход 0.2.6 → 0.2.7 нужен только
диспетчеру**: автозаголовки диалогов и исправление тестов release CI.
Коннекторы на 0.2.6 можно оставить. Более старые коннекторы следует обновить,
чтобы получить discovery и исправления ответов/resume Claude Code.

Перед обновлением завершите активные задания и подтверждения, проверьте idle
и сохраните конфиги/env/ключи, данные, native history и `pins/managed-sessions`.
Повторный setup/pairing при обычном обновлении не нужен.

## Через Telegram

`/updates` → **Check versions**, затем обновление выбранного компонента.
Сначала диспетчер, после проверки — нужные коннекторы по одному.
Этот путь требует готового независимого updater: поддерживается uv registry,
для Docker нужен host companion и разрешённый immutable image. Editable Git
не поддерживает обновление из бота. Ручная смена Docker-образа уже подтверждена;
успешный Telegram update после ACL-исправления остаётся открытой приёмкой.
[Ограничения и настройка](remote-updates.md).

## uv-диспетчер и коннекторы

Запускайте под владельцем службы с фактическими config/env путями:

```sh
acpgw --config /absolute/config.yaml --env-file /absolute/private.env update --version 0.2.7 --with-services --service-role gateway
acpgw update --status
```

Для коннектора замените `--service-role gateway` на `--service-role connector`.
На Windows используйте PowerShell и сохранённые пути:

```powershell
$Cfg = Join-Path $env:LOCALAPPDATA 'acpgw-connector'
$Acpgw = Join-Path ((uv tool dir --bin).Trim()) 'acpgw.exe'
$G = @('--config', "$Cfg\config.yaml", '--env-file', "$Cfg\.env")
& $Acpgw @G update --version 0.2.7 --with-services --service-role connector
& $Acpgw update --status
```

На Windows exit 0 означает постановку в очередь. Дождитесь `succeeded`,
не переустанавливайте задачу при `queued`/`running`. Затем проверьте версию,
`config check`, статус службы и переподключение прежнего computer ID.
Для Windows editable Git используйте [отдельный порядок](windows-source.md#update-and-test):
остановка задачи, `git pull --ff-only`, metadata/dependencies и запуск задачи.

## Docker-диспетчер

Официальный образ:

```text
ghcr.io/lujker/acp-gateway@sha256:e86d1b578c713e70452cb90b64aa2ac24fe121e5f4eab08c64885d881294b1f9
```

Скачайте образ, остановите writers и сохраните согласованную копию `state/`
(включая SQLite/WAL), `runtime/`, `.env` и текущие Compose overrides. В последнем
`compose.release.yaml` закрепите image выше и `pull_policy: never`. Сохраните
все используемые `-f` overrides, затем выполните для `gateway`:

```sh
docker compose -f compose.yaml -f compose.proxy.yaml -f compose.release.yaml config --quiet
docker compose -f compose.yaml -f compose.proxy.yaml -f compose.release.yaml up -d --no-build --no-deps --pull never --wait --wait-timeout 120 gateway
```

Пути и список overrides зависят от установки. Подробности backup/Compose —
[Docker runbook](vps-docker.ru.md); пример остановки и копирования —
[инструкция 0.2.1](update-0.2.1.ru.md#1-docker-диспетчер-на-существующем-vps),
где старую версию образа нужно заменить на приведённый digest.
Не запускайте `acpgw update` внутри контейнера.

## Данные и проверка

В 0.2.7 нет новой миграции относительно 0.2.6: schema остаётся 10.
При переходе с 0.2.2 и ниже сохраните pre-upgrade БД: переход 9 → 10 не позволяет
запустить 0.2.2 на новой базе. После активации legacy-rebind нельзя восстанавливать
старую authority; [ограничения pairing/rollback](connector-pairing.md#one-time-mandatory-legacy-rebind).

Проверьте `acpgw version --json`, health/status, Telegram/MCP и прежние компьютеры.
Диспетчер должен сообщать 0.2.7, коннекторы — 0.2.6 или 0.2.7 в зависимости от
выбранного обновления. Native Codex/Claude/Hermes этим обновлением не заменяются.
Autotitle сработает на следующем сообщении в безымянном диалоге, без массового
переименования старой истории. Модельную проверку проводите отдельно с бюджетом.
