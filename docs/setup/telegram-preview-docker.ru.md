# Просмотр новых Telegram-функций на Docker VPS без релиза

> Решение владельца 2026-10-16: оставшиеся приёмочные проверки предварительно
> закрыты. Использовать этот список при сообщениях о проблемах, не как блокер
> текущей разработки. macOS коннектор владелец проверит отдельно позже.


Это порядок для существующего развёртывания из [Docker-инструкции](vps-docker.ru.md):
`~/projects/acp-gateway/deploy/docker`, `compose.yaml` + `compose.proxy.yaml`,
nginx хоста и сохранённые `runtime/`, `state/`, `.env`.
Если каталог на VPS другой, замените только путь `DEPLOY` ниже.

## Обновление из рабочей ветки — основной способ

Для проверенной установки `/home/vpnadmin/projects/acp-gateway` образ
`acpgw:local` собирается непосредственно из checkout. Изменения доступны в
`main` (начиная с `d42c430`), без выпуска нового релиза. Дождитесь завершения
активных задач, затем выполните на VPS:

```bash
cd /home/vpnadmin/projects/acp-gateway
git fetch origin
git switch main
git pull --ff-only
cd deploy/docker
docker compose -f compose.yaml -f compose.proxy.yaml up -d --build gateway
docker compose -f compose.yaml -f compose.proxy.yaml ps
docker compose -f compose.yaml -f compose.proxy.yaml logs --tail=50 gateway
```

Если локального `main` ещё нет, вместо `git switch main` используйте
`git switch --track origin/main`.
При следующих обновлениях `main` достаточно `git pull --ff-only` в корне
проекта и той же команды Compose `up -d --build gateway` в `deploy/docker`.
Compose использует существующие `runtime/`, `state/`, `.env` и proxy network.
Пересоздание контейнера ненадолго прерывает работу бота. На старой установке
проверена версия `0.2.0rc9.dev0`; публикация ветки сама по себе её не обновляет.

## Альтернатива: архив без доступа к GitHub

Для передачи исходников без Git подготовлен архив
`acp-gateway-telegram-preview-20261015.tar.gz` — снимок исходников для сборки
отдельного локального Docker-образа, без публикации GitHub/PyPI/registry.
Настоящие конфиги, токены, БД, `.git` и виртуальное окружение в архив не входят.
`PREVIEW-MANIFEST.json` и `verify-preview.py` проверяют список и SHA-256 файлов.
Номер пакета остаётся `0.2.0`; отличайте preview по тегу образа и manifest.

## 1. Передать архив из WSL на VPS

Замените `USER@VPS` на ваш SSH-адрес. В терминале WSL:

```bash
scp /home/vzelenskiy/projects/acp-gateway/dist/acp-gateway-telegram-preview-20261015.tar.gz /home/vzelenskiy/projects/acp-gateway/dist/acp-gateway-telegram-preview-20261015.tar.gz.sha256 USER@VPS:~/
ssh USER@VPS
```

Архив также можно передать привычным SFTP-клиентом вместе с `.sha256`.

## 2. Проверить архив и собрать образ, пока старый контейнер работает

На VPS, в одной SSH-сессии; команды рассчитаны на Bash:

```bash
set -e
cd "$HOME"
sha256sum -c acp-gateway-telegram-preview-20261015.tar.gz.sha256
SRC=$(mktemp -d "$HOME/acpgw-preview.XXXXXXXX")
tar -xzf "$HOME/acp-gateway-telegram-preview-20261015.tar.gz" -C "$SRC"
python3 "$SRC/verify-preview.py"
docker build -t acpgw:telegram-preview-20261015 "$SRC"

DEPLOY="$HOME/projects/acp-gateway/deploy/docker"
cd "$DEPLOY"
dc() { docker compose -f compose.yaml -f compose.proxy.yaml "$@"; }
dc ps
BASE_VERSION=$(dc exec -T gateway acpgw --version)
printf '%s\n' "$BASE_VERSION"
dc exec -T gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env status
OLD_IMAGE=$(docker inspect "$(dc ps -q gateway)" --format '{{.Image}}')
docker image tag "$OLD_IMAGE" acpgw:before-telegram-preview-20261015
```

Сборка не заменяет работающий контейнер. В том числе проверьте `/jobs active`
в Telegram и дождитесь завершения всех задач/подтверждений.
Не запускайте второй `serve` с тем же ботом.

Создайте отдельный override; основной Compose и nginx не переписываются:

```bash
cat > compose.telegram-preview.yaml <<'YAML'
services:
  gateway:
    image: acpgw:telegram-preview-20261015
    pull_policy: never
YAML
dcp() { docker compose -f compose.yaml -f compose.proxy.yaml -f compose.telegram-preview.yaml "$@"; }
dcp config --quiet
dcp run --rm --no-deps --pull never gateway config check
```

Последняя команда проверяет конфиг, а не запускает бота. Build context базового
Compose сохранён, но далее используется уже собранный image с `--no-build`.

## 3. Короткое переключение с согласованной резервной копией

Только после завершения текущих задач:

```bash
dc stop gateway
umask 077
BACKUP="$HOME/acpgw-backups/telegram-preview-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -a runtime state .env "$BACKUP/"
printf '%s\n' "$OLD_IMAGE" > "$BACKUP/old-image-id.txt"
printf '%s\n' "$BASE_VERSION" > "$BACKUP/base-version.txt"
printf 'Backup: %s\n' "$BACKUP"
dcp up -d --no-build --no-deps --pull never gateway
dcp ps
dcp logs --tail 100 gateway
```

После остановки writers SQLite не работают, поэтому копия `state/` вместе с
возможными WAL/SHM согласована. Резервная копия содержит ключи и токены; она
остаётся приватной на сервере. Сохраняйте показанный путь `BACKUP`.
Если копирование или запуск завершились ошибкой, сначала разберите её;
для возврата старого контейнера используйте раздел 5.

Пересоздаётся только `gateway`; bind mounts сохраняются. Не выполняйте
`docker compose down -v`, `prepare.py` или `acpgw update` для этого переключения.
Адрес, порты, nginx и ключи компьютеров не меняются. Коннекторы для этих
Telegram-изменений обновлять/переустанавливать не требуется.

## 4. Приёмка

```bash
dcp exec -T gateway python -c 'import acp_gateway.channels.telegram_context, acp_gateway.channels.telegram_shortcuts; print("Telegram preview modules: OK")'
dcp exec -T gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env status
dcp exec -T gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers status
```

Дождитесь `healthy` и подключения обоих ПК. В Telegram:

1. `/menu`, `/context`: компьютер, агент, папка, диалог; карточка обновляется
   после смены выбора и остаётся отдельной от ответа.
2. `/recent`: предпросмотр диалога другого агента не меняет выбор; отдельная
   кнопка выбирает точный диалог.
3. `/agent` → ☆ Добавить → `/favorites`: добавить, выбрать, удалить маршрут;
   недоступный ПК не подменяется другим.
4. Переименовать диалог кнопкой, проверить `/cancel` и обычный следующий текст.
5. Выполнить короткую задачу: «Принято» → выполнение/подтверждение → итог;
   у результата проверить «Продолжить» и «Новый диалог» без повтора запроса.
6. `/delivery JOB_ID`, `/result JOB_ID`, `/model state`: понятные состояния,
   сохранённый результат/сведения не запускают агент заново.
7. На двух ПК проверить параллельные диалоги, allow/deny и независимость маршрутов.

Для первых проверок можно явно создать новый диалог. Существующий живой Hermes
диалог может пережить перезапуск только диспетчера, пока коннектор/Hermes живы;
нельзя обещать cold resume после перезапуска коннектора. Бот объяснит отказ.

## 5. Откат

Сначала завершите новые задачи, закройте ввод названия (`/cancel`) и остановите preview. В той же SSH-сессии:

```bash
dcp stop gateway
if [ "$(cat "$BACKUP/base-version.txt")" != "acpgw 0.2.0" ]; then
    AFTER=$(mktemp -d "$DEPLOY/state-after-preview.XXXXXXXX")
    mv state "$AFTER/state"
    cp -a "$BACKUP/state" state
fi
cat > compose.telegram-preview.yaml <<'YAML'
services:
  gateway:
    image: acpgw:before-telegram-preview-20261015
    pull_policy: never
YAML
dcp up -d --no-build --no-deps --pull never gateway
dcp ps
```

В этой итерации нет новых миграций относительно исходной базы разработки
0.2.0, поэтому для VPS уже на 0.2.0 откат image сохраняет актуальную БД.
Если исходный VPS был старее, приведённый блок сначала сохраняет текущий
`state/` отдельно и возвращает согласованную копию из `BACKUP`, затем запускает
старый image. Данные тестового периода сохранятся отдельно, а старая версия
получит свою схему. Путь `BACKUP` должен быть тем, который вы сохранили в разделе 3. Они не объединяются автоматически. Не открывайте новую схему старой
версией без проверки совместимости.

После удачной приёмки можно оставить override с preview-image. При будущих
пересозданиях добавляйте его третьим `-f`; `acpgw:local` из базового Compose
по-прежнему обозначает старый образ. Публикация релиза — отдельное поручение.

Команды сверены с [Docker Compose production guide](https://docs.docker.com/compose/how-tos/production/)
и [справочником up](https://docs.docker.com/reference/cli/docker/compose/up/).
Работающий VPS в рамках подготовки архива не изменялся.
