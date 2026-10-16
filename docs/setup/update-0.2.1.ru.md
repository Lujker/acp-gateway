# Обновление до 0.2.1: Docker-диспетчер и uv-коннекторы

Сначала обновите диспетчер, затем коннекторы по одному. Завершите текущие задания
и подтверждения. Повторный setup, новые ключи компьютеров и смена адресов не нужны.
После перезапуска коннектора начните новый Hermes-диалог через `/new`.

## 1. Docker-диспетчер на существующем VPS

Команды для существующего `~/projects/acp-gateway/deploy/docker` с
`compose.yaml` + `compose.proxy.yaml`, host nginx, `runtime/`, `state/`, `.env`.
Для другого каталога замените путь. Если используются дополнительные Compose
overrides, сохраните их в списке `-f`; `compose.release.yaml` поставьте последним.
Обновлять checkout для перехода на готовый registry image не требуется.

На VPS в одной Bash-сессии:

```bash
set -e
cd "$HOME/projects/acp-gateway/deploy/docker"
docker pull ghcr.io/lujker/acp-gateway:0.2.1

dc_old() { docker compose -f compose.yaml -f compose.proxy.yaml "$@"; }
OLD_IMAGE=$(docker inspect "$(dc_old ps -q gateway)" --format '{{.Image}}')
umask 077
BACKUP="$HOME/acpgw-backups/before-0.2.1-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
dc_old stop gateway
cp -a runtime state .env compose.yaml compose.proxy.yaml "$BACKUP/"
if [ -f compose.release.yaml ]; then cp -a compose.release.yaml "$BACKUP/"; fi
printf '%s\n' "$OLD_IMAGE" > "$BACKUP/old-image-id.txt"
printf 'Backup: %s\n' "$BACKUP"

cat > compose.release.yaml <<'YAML'
services:
  gateway:
    image: ghcr.io/lujker/acp-gateway:0.2.1
    pull_policy: never
YAML
dc() { docker compose -f compose.yaml -f compose.proxy.yaml -f compose.release.yaml "$@"; }
dc config --quiet
dc up -d --no-build --no-deps --pull never --wait --wait-timeout 120 gateway
dc exec -T gateway acpgw version --json
dc exec -T gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env status
dc exec -T gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers status
```

Ожидается `version: 0.2.1`, healthy контейнер и подключение компьютеров.
Сохраните показанный путь `BACKUP`: там приватные конфиги/ключи и согласованная
копия SQLite вместе с WAL/SHM, сделанная после остановки writers.
При ошибке запуска смотрите `dc logs --tail 100 gateway`; не запускайте второй
бот/`serve` и не повторяйте обновление вслепую. Не используйте `down -v`, повторный
`prepare.py` или `acpgw update` внутри контейнера.

При последующих Compose-командах продолжайте добавлять `-f compose.release.yaml`
последним: базовый checkout всё ещё содержит `image: acpgw:local`.
`--no-build` выбирает готовый образ, `--no-deps` ограничивает пересоздание gateway,
`--wait` ждёт running/healthy; см. [официальную справку Compose up](https://docs.docker.com/reference/cli/docker/compose/up/).

## 2. Windows-коннектор, установленный через uv

Откройте обычную PowerShell под тем Windows-пользователем, который запускает
Hermes и задачу коннектора. Используйте фактический каталог и env-файл установки:

```powershell
$Cfg = Join-Path $env:LOCALAPPDATA 'acpgw-connector'
$Acpgw = Join-Path ((uv tool dir --bin).Trim()) 'acpgw.exe'
$G = @('--config', "$Cfg\config.yaml", '--env-file', "$Cfg\.env")
& $Acpgw @G update --version 0.2.1 --with-services --service-role connector
```

Команда выполняет managed update с резервной копией и восстановлением состояния
задачи. **На Windows первоначальный exit 0 означает постановку в очередь.**
Периодически запрашивайте статус; пока он queued/running, ничего не переустанавливайте:

```powershell
& $Acpgw update --status
```

Только после `succeeded`:

```powershell
& $Acpgw version --json
& $Acpgw @G config check
& $Acpgw @G service --role connector status
```

Ожидается `0.2.1` и восстановленное исходное состояние службы. Повторите на другом ПК.
Пути/ключи/ID остаются прежними. Если при обновлении раньше меняли `connection`,
новые сохранённые настройки попадут в аргументы задачи только через `service install`.

### Если старая версия отказывает: Live connector activity is unavailable

У старого watcher могла прекратиться публикация состояния. Не используйте force
для обхода активных/неопределённых работ. После завершения всех заданий можно
сделать явный переход через uv. Сначала штатно отключите задачу:

```powershell
& $Acpgw @G service --role connector disable
if ($LASTEXITCODE -ne 0) { throw 'Connector did not stop; inspect logs before continuing' }
& $Acpgw @G service --role connector status
```

Убедитесь, что задача остановлена и нет foreground-коннектора. Сохраните приватную
копию `$Cfg` и фактического каталога данных (его показывает конфигурация/`acpgw paths`)
после остановки. Затем:

```powershell
uv tool install --upgrade --python 3.12 --managed-python 'acp-gateway==0.2.1'
if ($LASTEXITCODE -ne 0) { throw 'uv update failed; keep connector stopped and inspect the error' }
& $Acpgw version --json
& $Acpgw @G service --role connector install
if ($LASTEXITCODE -ne 0) { throw 'Task reinstall failed; inspect the error before enabling' }
& $Acpgw @G service --role connector enable
& $Acpgw @G service --role connector status
```

Прямой uv-путь не выполняет managed backup/rollback, поэтому копия нужна отдельно.
`uv tool upgrade acp-gateway` может сохранить старое `==0.2.0` ограничение;
`tool install --upgrade ... ==0.2.1` намеренно заменяет его. Не выполняйте прямую
uv-команду поверх работающей задачи. Дополнительный `setup`/enrollment не нужен.

## 3. Linux/WSL uv-коннектор

Для установленной systemd user службы:

```bash
acpgw --config /absolute/config.yaml --env-file /absolute/private.env \
  update --version 0.2.1 --with-services --service-role connector
acpgw update --status
acpgw version --json
acpgw service --role connector status
```

Здесь managed coordinator ждёт завершения. При ошибке сначала разберите сохранённый
статус/логи. Для foreground-установки сначала остановите процесс, затем обновляйте
uv tool с точным ограничением `acp-gateway==0.2.1` и запускайте снова.

## 4. После обновления

В Telegram откройте `/updates`: диспетчер и коннекторы должны показать `0.2.1`.
Сама установка новой версии не включает удалённое выполнение обновлений.
Для следующих релизов отдельно включите `updates.enabled` и независимый companion
на каждом нужном хосте по [инструкции updater](remote-updates.md).
Для Docker companion работает на хосте, а не внутри gateway-контейнера.
macOS проверяется владельцем позже и не входит в текущий remote-updater scope.

Если нужен откат Docker, остановите gateway, сохраните отдельно новые данные и
используйте старый image ID из `BACKUP/old-image-id.txt`. Возвращайте согласованную
копию БД из этого же backup только явно: это отменяет изменения данных после backup.
Не открывайте новую схему старой программой без проверки совместимости.
