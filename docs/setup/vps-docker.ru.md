# Диспетчер на VPS в Docker

Как поднять диспетчер ACP Gateway в Docker на своём VPS и как перенести туда
уже работающий нативный диспетчер (`uv tool` + служба systemd) без повторной
регистрации компьютеров. Коннекторы на ПК остаются как есть: меняется только
адрес диспетчера.

Пошаговая первая приёмка с двумя компьютерами и Telegram —
[VPS runbook](vps-acceptance.ru.md); здесь — постоянное развёртывание.

## 1. Какой вариант выбрать

| Вариант | Когда | Compose-файлы | Адрес для коннекторов |
| --- | --- | --- | --- |
| **За reverse proxy хоста** | На VPS уже работает nginx (или другой прокси) в Docker со своим доменом и сертификатом | `compose.yaml` + `compose.proxy.yaml` | `wss://ВАШ_ДОМЕН/acpgw/connect` |
| Свой nginx с TLS | Порт 443 свободен, сертификат кладёте сами | `compose.yaml` + `compose.nginx.yaml` | `wss://ВАШ_ДОМЕН/acpgw/connect` |
| Прямой порт | Только проверка: соединение **не шифруется** | `compose.yaml` + `compose.direct.yaml` | `ws://IP:18766/acpgw/connect` |

Во всех вариантах owner API и MCP остаются на `127.0.0.1` внутри
контейнера, наружу выходит только вход коннекторов. Контейнер работает без
root, с файловой системой только на чтение, без capabilities и с лимитами
512 МБ памяти и одного CPU.

Свой nginx и прямой порт описаны в [VPS runbook](vps-acceptance.ru.md),
разделы 2 и 5. Ниже — вариант за reverse proxy и перенос.

## 2. Подготовка

Нужны Docker Engine с Compose v2, Git и Python 3 (только для `prepare.py`).
Клонируйте репозиторий в постоянный каталог и выберите проверенный релиз:

```bash
git clone https://github.com/Lujker/acp-gateway.git ~/projects/acp-gateway
cd ~/projects/acp-gateway
git checkout v0.2.0rcN          # тег релиза, который ставите
python3 deploy/docker/prepare.py
cd deploy/docker
```

`prepare.py` создаёт приватные `runtime/` (настройки и токены), `state/`
(SQLite) и `.env` (UID/GID для Compose); существующие файлы не
перезаписывает. Запускайте его от пользователя, которому будет принадлежать
развёртывание. Все эти файлы исключены из Git и из контекста сборки.

Образ собирается на самом VPS из этого checkout:

```bash
docker compose build
```

## 3. За reverse proxy хоста

### 3.1 Сеть прокси

Узнайте Docker-сеть, в которой работает контейнер прокси, и допишите её имя
в `deploy/docker/.env`:

```bash
docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' ИМЯ_КОНТЕЙНЕРА_ПРОКСИ
echo 'ACPGW_PROXY_NETWORK=имя_сети' >> .env
```

`compose.proxy.yaml` подключает диспетчер к этой сети под именем
`acpgw-dispatcher` и не публикует портов на хосте. Сеть должна существовать
заранее: Compose диспетчера её не создаёт и не удаляет.

Диспетчер окажется в одной сети с остальными контейнерами прокси. Если там
есть сервисы, которых он не должен видеть, заведите отдельную сеть только для
прокси и диспетчера и подключите к ней контейнер прокси.

### 3.2 Путь в прокси

Добавьте в HTTPS-сервер прокси точный путь коннекторов. Для nginx образец —
[`nginx-location.conf.example`](../../deploy/docker/nginx-location.conf.example):
в блоке `http {}` прокси должны быть `resolver 127.0.0.11` и
`map $http_upgrade $connection_upgrade`; если их нет, используйте
`proxy_set_header Connection "upgrade"`. Адрес диспетчера — в переменной:
nginx ищет его на каждом запросе, поэтому отсутствие диспетчера не мешает
прокси стартовать, а путь просто отвечает 502.

```nginx
location = /acpgw/connect {
    set $acpgw_ingress acpgw-dispatcher:8766;
    proxy_pass http://$acpgw_ingress;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-ACP-Computer $http_x_acp_computer;
    proxy_buffering off;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
    proxy_connect_timeout 5s;
}
```

Путь должен совпадать с `connector.connect_path` в `runtime/gateway.yaml`.
Только HTTPS: в HTTP-сервер этот путь не добавляйте. Коннектор шлёт пульс
каждые 20 секунд, поэтому таймаута 120 секунд хватает; если перед прокси
стоит ещё один L4-прокси, его таймаут должен быть не меньше.

### 3.3 Настройка и запуск

В `runtime/gateway.yaml`:

```yaml
data_dir: /data
connector:
  enabled: true
  host: 0.0.0.0        # внутри контейнера; наружу порт не публикуется
  port: 8766
  connect_path: /acpgw/connect
```

TLS на самом диспетчере не нужен (`tls_cert`/`tls_key` не задавайте): его
снимает прокси. В `runtime/gateway.env` — токен Telegram-бота и токены
owner API и MCP. Затем:

```bash
docker compose -f compose.yaml -f compose.proxy.yaml config --quiet
docker compose -f compose.yaml -f compose.proxy.yaml run --rm --no-deps gateway config check
docker compose -f compose.yaml -f compose.proxy.yaml up -d
docker compose -f compose.yaml -f compose.proxy.yaml ps
```

Проверка снаружи без ключа — ответ диспетчера, а не 404 или 502 прокси:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://ВАШ_ДОМЕН/acpgw/connect
```

## 4. Перенос нативного диспетчера в Docker

Компьютеры хранятся в `gateway.db` только хешами ключей, поэтому после
переноса базы ключи на ПК остаются действительными. Новый диспетчер должен
быть **той же версии или новее** — базу старой версией не открывать.

1. Подготовьте Docker-развёртывание по разделам 2–3, но **не запускайте**.
2. На старом сервере остановите службу: `systemctl --user stop acp-gateway`.
   Telegram-бот может опрашивать только один процесс, и база не должна
   меняться во время копирования.
3. Перенесите три вещи (пути старой установки показывает `acpgw paths`):
   - `config.yaml` → `runtime/gateway.yaml`, и в нём: добавить
     `data_dir: /data`; у `connector` поставить `host: 0.0.0.0`,
     `port: 8766`, убрать `tls_cert`/`tls_key`, оставить `connect_path`;
     у `gateway` оставить только `port` (host внутри контейнера — loopback);
   - `.env` → `runtime/gateway.env` (права `0600`);
   - `gateway.db` → `state/gateway.db` согласованной копией вместе с
     журналом WAL:

     ```bash
     python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' \
       ~/.local/share/acp-gateway/gateway.db /tmp/gateway.db
     ```

     Файл скопировать в `state/`, владельцем сделать пользователя из `.env`
     (`ACPGW_UID`), права `0600`.
4. Запустите диспетчер в Docker (раздел 3.3) и проверьте, что компьютеры на
   месте: `acpgw computers list` (команды — раздел 5).
5. На каждом ПК смените адрес диспетчера: остановить службу коннектора
   (`service --role connector disable` на Windows), в `acpgw setup`
   раздел `connection` указать `wss://ВАШ_ДОМЕН/acpgw/connect` с тем же
   computer ID и ключом, затем `service --role connector enable`.
6. Проверьте `computers status` (оба online) и Telegram: `/start`, `/agent`,
   короткий запрос.
7. **Откат**, пока старую установку не удалили: остановить Docker-диспетчер
   (`docker compose ... stop gateway`), запустить старую службу, вернуть ПК
   прежний адрес. Работа, сделанная после переноса, останется только в
   Docker-копии базы.
8. После приёмки на старом сервере: `systemctl --user disable acp-gateway`,
   закрыть порт прямого входа в firewall, удалить установку, когда она точно
   не нужна для отката.

## 5. Эксплуатация

Команды диспетчера выполняются внутри контейнера:

```bash
alias acpgw-docker='docker compose -f compose.yaml -f compose.proxy.yaml exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env'
acpgw-docker status
acpgw-docker computers status
acpgw-docker computers enroll laptop --token-file /data/enrollment/laptop.key
docker compose -f compose.yaml -f compose.proxy.yaml logs --tail 100 gateway
```

Новый ключ из `/data/enrollment/` лежит на хосте в `state/enrollment/`;
перенесите его на ПК приватно и удалите с сервера.

**Обновление** — когда нет активных заданий:

```bash
cd ~/projects/acp-gateway
git fetch --tags && git checkout v0.2.0rcM
cd deploy/docker
docker compose -f compose.yaml -f compose.proxy.yaml build
docker compose -f compose.yaml -f compose.proxy.yaml up -d
```

`runtime/`, `state/` и `.env` при обновлении не меняются. Откат — checkout
прежнего тега и те же две команды, если новая версия не меняла схему базы.
`acpgw update` к Docker-развёртыванию не применяется: он обновляет установки
через uv.

**Бэкап** — копия `state/gateway.db` (тем же способом, что в разделе 4) и
`runtime/`. Без бэкапа при потере сервера компьютеры регистрируются заново
новыми ключами.
