# Установка через uv/PyPI и независимые настройки

Версия инструкции — **0.2.0rc3**. Один пакет `acp-gateway` устанавливается на
VPS и ПК; роль выбирается в базовой настройке. Git checkout, Docker и скачивание
wheel/lock файлов для обычной установки не нужны.

## 1. Установка на Ubuntu

Под обычным SSH-пользователем (в текущей установке `gatewayuser`, Ubuntu 22.04.5):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install --python 3.12 "acp-gateway==0.2.0rc3"
uv tool update-shell
export PATH="$(uv tool dir --bin):$PATH"
acpgw version --json
```

Ожидается version `0.2.0rc3`, manager `uv`, source `registry`.
Python и зависимости предоставляет uv. Существующую установку обновляем по
разделу ниже; повторная установка не означает повторную настройку.

## 2. Меню setup

```bash
acpgw setup
```

Выбрать номер или название раздела. Изменения сохраняются только после
подтверждения выбранного раздела. После завершения или отмены вернётся меню.
Открытие меню и `exit` сами по себе ничего не создают.

| Раздел | Что настраивается |
| --- | --- |
| base | Первичная настройка роли и сети; не требует агентов или Telegram |
| gateway | API/connector listener; без повторной настройки других разделов |
| agents | Локальные профили или маршруты к агентам на компьютерах |
| connection | Исходящее подключение ПК к диспетчеру |
| telegram | Bot token, разрешённые user IDs и progress status |
| hermes | Подключение gateway MCP к локальному Hermes Agent |
| computers | Выпуск/ротация/отзыв отдельных ключей компьютеров |
| agent-tools | Отдельные команды установки/provider/службы локального Goose |
| service | Установка, запуск и автозапуск службы или Windows task |
| policy | Разрешённые действия и лимиты prompt/response/approval |
| rate-limits | Ограничение числа запросов |
| retention | Сроки хранения истории и audit |
| logging | Уровень, формат, файл и ротация логов |
| check | Проверка конфигурации; live probes запускаются только по выбору |

Раздел можно открыть напрямую:

```bash
acpgw setup --section base --role server
acpgw setup --section telegram
acpgw setup --section agents
```

`--role` применяется в базовой настройке. Для конкретной конфигурации:

```bash
acpgw --config "$HOME/.config/acp-gateway/config.yaml" --env-file "$HOME/.config/acp-gateway/.env" setup
```

`--config-dir` у setup выбирает другой каталог. Все действия меню сохраняют
выбранные YAML/env пути. `setup --non-interactive` сохраняет прежний режим
создания templates для автоматизации. `--section all` — явный совместимый
общий мастер старых workflows, для обычной настройки он не требуется.

## 3. Первичная настройка VPS

В меню выбрать `base`, роль `server`:

| Вопрос | Ответ для текущего IP-теста |
| --- | --- |
| Local API/MCP port | 8765 |
| Connector listen address | **0.0.0.0**, ввести явно |
| Connector port / URL path | 18766 / /acpgw/connect |
| TLS certificate path | пусто |
| Allow unencrypted public test ingress? | yes |
| Save these settings? | yes |

Этот шаг завершится без вопросов о Windows, агенте, Telegram или автозапуске.
Можно выбрать `exit` и вернуться к остальным настройкам позже.
Текущий временный маршрут — `ws://SERVER_IP:18766/acpgw/connect`; TCP 18766
открыт для всех по решению владельца из-за динамических IP домашних ПК.
Owner API 8765 остаётся на loopback. Для постоянного WSS/переноса будет другой этап.

## 4. Отдельные шаги на VPS

- `telegram`: включить бот, скрыто ввести token, указать числовой ID владельца.
  Один бот обслуживается одним polling process; старый polling остановить отдельно.
- `computers` → `enroll`: ID первого ПК `home`, display name по желанию.
  Default ключ — `~/.config/acp-gateway/enrollment/home.key`; файл создаётся
  приватно, в чат его не присылать. Для второго ПК отдельный ID и ключ.
- `agents`: тип **hermes** (ввести явно), alias `hermes`, computer ID `home`,
  точный существующий Windows cwd. Это маршрут `home/hermes`; Hermes на VPS не нужен.
- `service` → `install`, затем `enable`. Это отдельное действие, настройка
  Telegram/агентов сама по себе автозапуск не устанавливает.
- `check`: проверить конфигурацию и по желанию подключения.

Можно запустить сервер без агентов и подключать их позже. Текущий rc3 всё ещё
требует отдельного key enrollment и server route; автоматический pairing и
user/workspace ownership не заявляются реализованными.

Для systemd user после выхода из SSH/reboot:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

Ожидается `Linger=yes`. Если нет user bus, сначала разбираем ошибку.
Работающую службу не дублируем foreground `start/serve`.

## 5. Windows/Hermes

Обычная PowerShell под пользователем Hermes, не WSL:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

В новом окне:

```powershell
uv tool install --python 3.12 --managed-python 'acp-gateway==0.2.0rc3'
uv tool update-shell
$Acpgw = Join-Path ((uv tool dir --bin).Trim()) 'acpgw.exe'
& $Acpgw version --json
& $Acpgw agents probe hermes --role desktop-serve --json
$Cfg = Join-Path $env:LOCALAPPDATA 'acpgw-connector'
& $Acpgw setup --config-dir $Cfg --role connector
```

Hermes должен быть открыт, provider/model настроены; probe ожидает `ready`.
В меню сначала `base`: создаст приватную конфигурацию без обязательного соединения.
Затем выйти, приватно скопировать ключ с VPS (SSH_USER/SERVER_IP заменить):

```powershell
scp SSH_USER@SERVER_IP:.config/acp-gateway/enrollment/home.key (Join-Path $Cfg 'home.key')
& $Acpgw setup --config-dir $Cfg
```

- `agents`: тип Hermes, alias `hermes`, тот же cwd, discovery directory из probe,
  existing instance `desktop-serve`, фактический profile.
- `connection`: `ws://SERVER_IP:18766/acpgw/connect`, test yes, ID `home`,
  key file — полный `$Cfg\home.key`. Эти два раздела можно проходить в любом порядке.
- `service`: install, затем enable, когда оба раздела готовы.

Telegram на connector отключён. Входящие порты на Windows не нужны.
Входящий TCP 18766 нужен только на диспетчере. Запуск незавершённого connector
откажет с указанием недостающей настройки, не зарегистрирует пустой manifest.

На VPS: `acpgw computers status`. Затем в Telegram `/start`, `/agent`,
`/agent home/hermes`, `/new` и текст «Ответь одним словом PONG».
Это реальный вызов модели. Второй ПК подключаем после первого, с отдельным
computer ID/key/route. Reboot/logon/sleep/network, model/approvals и перенос/WSS
проверяются отдельными этапами.

## 6. Обновление установленного rc2 через uv

Для текущей начальной установки, пока daemon/service ещё не запущены:

```bash
uv tool install --upgrade --python 3.12 "acp-gateway==0.2.0rc3"
acpgw version --json
acpgw setup
```

Сначала выйти из старого мастера через Ctrl+C. Настройка хранится вне uv tool,
повторный setup не требуется для самого обновления. Команда выше намеренно
заменяет исходное `==0.2.0rc2`: `uv tool upgrade acp-gateway` сохраняет прежнее
ограничение и может оставить rc2.

Для уже работающих служб/активных jobs сначала завершить работу и штатно
остановить процессы. Управляемое обновление/rollback с сохранением состояния
службы — `acpgw update --version 0.2.0rc3 --with-services`; подробности в
[distribution.md](distribution.md). Прямые uv-команды не выполняют lifecycle
проверки приложения. Не обновлять tool environment во время работы connector.
