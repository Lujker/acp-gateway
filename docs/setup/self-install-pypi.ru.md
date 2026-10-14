# Установка через uv/PyPI и независимые настройки

Версия инструкции — **0.2.0rc6**. Один пакет `acp-gateway` устанавливается на
VPS и ПК; роль выбирается в базовой настройке. Git checkout, Docker и скачивание
wheel/lock файлов для обычной установки не нужны.

## 1. Установка на Ubuntu

Под обычным SSH-пользователем (в текущей установке `vpnadmin`, Ubuntu 22.04.5):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install --python 3.12 "acp-gateway==0.2.0rc6"
uv tool update-shell
export PATH="$(uv tool dir --bin):$PATH"
acpgw version --json
```

Ожидается version `0.2.0rc5`, manager `uv`, source `registry`.
Python и зависимости предоставляет uv. Существующую установку обновляем по
разделу ниже; повторная установка не означает повторную настройку.

## 2. Меню setup

```bash
acpgw setup
```

Меню показывает разделы для выбранной роли. На чистой установке доступны `base` и `exit`.
Выбрать номер или название раздела. Изменения сохраняются только после
подтверждения выбранного раздела. После завершения или отмены вернётся меню.
Открытие меню и `exit` сами по себе ничего не создают.

| Раздел | Что настраивается |
| --- | --- |
| base | Первичная настройка роли и сети; не требует агентов или Telegram |
| gateway | API/connector listener; без повторной настройки других разделов |
| agents | Профили на ПК агента; диспетчеру передаются автоматически |
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
- `service` → `install`, затем `enable`. Это отдельное действие, настройка
  Telegram/агентов сама по себе автозапуск не устанавливает.
- `check`: проверить конфигурацию и по желанию подключения.

Можно запустить сервер без агентов. После подключения коннектора rc4 маршрут
`home/hermes` появится автоматически, включая Telegram, CLI и MCP. Профиль и
Windows cwd вводятся только на ПК. Каталог сохраняется после отключения ПК
и перезапуска диспетчера; это не означает доступность отключённого агента.

Регистрация компьютера по ключу остаётся отдельной операцией. Автоматический
pairing и user/workspace ownership относятся к следующему этапу.

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

В новом окне проверить `uv --version`. Для фонового Windows запуска нужен
uv не ниже 0.12.4; при старой версии сначала `uv self update`. В старом uv
виртуальный `pythonw.exe` мог открывать консоль; исправление описано в
[официальном changelog](https://github.com/astral-sh/uv/blob/main/changelogs/0.12.x.md).

```powershell
uv tool install --python 3.12 --managed-python 'acp-gateway==0.2.0rc6'
uv tool update-shell
$Acpgw = Join-Path ((uv tool dir --bin).Trim()) 'acpgw.exe'
& $Acpgw version --json
& $Acpgw agents probe hermes --role desktop-serve --json
$Cfg = Join-Path $env:LOCALAPPDATA 'acpgw-connector'
& $Acpgw setup --config-dir $Cfg --role connector
```

В rc5 Windows logging и фоновый запуск задачи исправлены; дополнительные
зависимости через `--with` не нужны.

Hermes Desktop должен быть открыт как приложение, provider/model настроены;
probe ожидает `ready`. Автозапуск Desktop настраивается отдельно: ярлык
`Hermes Desktop.lnk` в пользовательской папке `shell:startup` должен запускать
GUI `Hermes.exe`. Запуск `hermes gateway run` не открывает Desktop. Коннектор
запускается после входа этого же пользователя в Windows и ожидает Hermes.

Служба использует `pythonw.exe`, пишет приватный журнал и не открывает консоль.
Для среды, созданной старым uv до 0.12.4, обновить uv до обновления пакета.
После обновления с rc4 один раз переустановить задачу (`service install`) перед
`service enable`, чтобы заменить прежний запуск `python.exe` на `pythonw.exe`.

В меню сначала `base`: создаст приватную конфигурацию без обязательного соединения.
Затем выйти, приватно скопировать ключ с VPS (SSH_USER/SERVER_IP заменить):

```powershell
scp SSH_USER@SERVER_IP:.config/acp-gateway/enrollment/home.key (Join-Path $Cfg 'home.key')
& $Acpgw setup --config-dir $Cfg
```

- `agents`: тип Hermes, alias `hermes`, реальная существующая папка проекта на этом ПК,
  discovery directory из probe,
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
computer ID/key. Маршрут создаётся автоматически. Reboot/logon/sleep/network, model/approvals и перенос/WSS
проверяются отдельными этапами.

## 6. Обновление до rc6 через uv

Сначала обновить диспетчер, затем коннекторы. Коннектор rc4 объявляет metadata
через hello v2; старый диспетчер его не примет. Диспетчер rc4 принимает старые
коннекторы через hello v1 с прежними ручными маршрутами. Адреса агентов и
история сохраняются; повторно выпускать ключ `home` не нужно.

Для установки без работающей службы:

```bash
uv tool install --upgrade --python 3.12 "acp-gateway==0.2.0rc6"
acpgw version --json
acpgw config check
```

Сначала выйти из старого мастера через Ctrl+C. Настройка хранится вне uv tool,
повторный setup не требуется для самого обновления. Команда выше намеренно
заменяет исходное ограничение версии, включая `==0.2.0rc4`: `uv tool upgrade acp-gateway` сохраняет прежнее
ограничение и может оставить прежнюю версию.

Для уже работающих служб/активных jobs сначала завершить работу и штатно
остановить процессы. Управляемое обновление/rollback с сохранением состояния
службы — `acpgw update --version 0.2.0rc6 --with-services`; подробности в
[distribution.md](distribution.md). Прямые uv-команды не выполняют lifecycle
проверки приложения. Не обновлять tool environment во время работы connector.


Если служба уже работает, сначала завершить jobs. Для прямого обновления через uv
на Linux: `acpgw service stop`, команда uv выше, затем `acpgw service start`.
На Windows: `acpgw --config "$Cfg\config.yaml" service --role connector disable`,
`uv tool install --upgrade --python 3.12 --managed-python 'acp-gateway==0.2.0rc6'`,
затем `acpgw --config "$Cfg\config.yaml" service --role connector install` и
`acpgw --config "$Cfg\config.yaml" service --role connector enable`.
Указывать фактические config/env пути при нестандартной установке. Не запускать
второй процесс вручную поверх службы. После обновления: `version --json`,
`config check`, `service status`; на VPS — `computers status` и `agents list`.

Рабочая папка является cwd новых сессий, а не песочницей файлового доступа.
Перед её изменением завершить активную работу. Существующие диалоги хранят
свой cwd: после смены папки создавать новый диалог, старый не переносится автоматически.
Смена alias или computer ID создаёт другой адрес; для сохранения привязки истории
оставлять прежние `home` и `hermes`.

`doctor` проверяет сохранённую настройку без сети. `doctor --live` дополнительно
проверяет API, Telegram getMe, TCP/TLS диспетчера и observer/initialize агента;
не создаёт диалоги, не отправляет prompts и не подтверждает действия. TCP/TLS
доступность сама по себе не подтверждает регистрацию: смотреть `computers status`.
Для изменения работающего Windows connector сначала выполнить `service disable`,
изменить нужный раздел setup, затем `service enable`.


В rc6 для штатного обновления работающей установки предусмотрен
`acpgw update --version 0.2.0rc6 --with-services` с фактическими config/env путями.
Сначала dispatcher, затем каждый connector. На Windows сохраняется роль connector;
при необходимости указать `--service-role connector`. После операции проверить
`update --status`, `version --json` и `service status`.
`update --stable --with-services` ищет только более новую стабильную PyPI версию;
RC нужно выбирать через --version. При отсутствии новой stable пакет/службы не
меняются. Telegram панель rc6 — `/menu`; cold resume после connector restart
остаётся вне MVP, следующий prompt отправлять из нового `/new` диалога.
