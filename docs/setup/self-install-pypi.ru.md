# Установка из PyPI: Ubuntu 24.04 + Windows/Hermes

Целевая версия: **0.2.0rc2**, кандидат для живой проверки. До публикации этой
версии команды установки не сработают. На всех компьютерах ставится один пакет
`acp-gateway`, команда называется `acpgw`; роль выбирается при настройке.

Идём по этапам. После каждого проверяем результат, затем продолжаем.
Для первой установки не нужны checkout Git, Docker, архивы из WSL или nginx.
Существующую установку сначала проверяем через `acpgw version --json` и
`acpgw service status`; команды ниже предназначены для новой установки.

## 1. Схема и выбранные значения

| Где | Роль |
| --- | --- |
| Ubuntu 24.04, обычный SSH-пользователь | Dispatcher и единственный Telegram polling |
| Текущий Windows, пользователь Hermes Desktop | Connector и существующий Hermes |
| Второй Windows, позже | Другой connector со своим ID и ключом |

Примеры: listener TCP `18766`, path `/acpgw/connect`, первый computer ID `home`,
alias `hermes`, route `home/hermes`. Адрес подключения:
`ws://SERVER_IP:18766/acpgw/connect`. Заменить SERVER_IP реальным адресом.
Owner API слушает `127.0.0.1:8765`; этот порт наружу не открываем.

Прямой `ws://` не шифрует трафик. Это согласованный временный IP-тест;
ограничиваем входящий порт адресами connector-компьютеров. Постоянный WSS
через домен и перенос dispatcher проверяются отдельным этапом.

Заранее выбрать существующую рабочую папку Windows (например,
`C:\Users\YOUR_USER\work`). Её точный путь понадобится и на VPS, и на Windows.
Подготовить Telegram bot token и числовой Telegram user ID владельца.
Token вводится скрыто в мастере; ключи и `.env` в чат не присылать.
Если тот же бот уже работает где-то ещё, сначала остановить прежний polling.

## 2. Ubuntu: проверить исходное состояние

В обычном SSH-сеансе:

```bash
cat /etc/os-release
whoami
command -v uv || true
command -v acpgw || true
systemctl --user is-system-running
```

Нужен обычный пользователь, не root. Если нет user bus или ответ `degraded`,
сначала разбираем вывод; службы пока не создаём.

## 3. Ubuntu: установить uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

Если curl отсутствует: `sudo apt update`, затем `sudo apt install curl ca-certificates`.
Ожидается версия uv. Python 3.12 пакетный менеджер предоставит при установке.

## 4. Ubuntu: установить опубликованный пакет

Скачиваем только список закреплённых зависимостей и контрольные суммы из
официального GitHub Release. Сам пакет uv скачивает из PyPI.

```bash
VERSION=0.2.0rc2
PKGDIR="$HOME/acpgw-packages/$VERSION"
mkdir -p "$PKGDIR"
cd "$PKGDIR"
BASE="https://github.com/Lujker/acp-gateway/releases/download/v$VERSION"
curl -fL "$BASE/requirements.lock.txt" -o requirements.lock.txt
curl -fL "$BASE/SHA256SUMS" -o SHA256SUMS
grep '  requirements.lock.txt$' SHA256SUMS > requirements.sha256
test -s requirements.sha256 && sha256sum -c requirements.sha256
```

Продолжать только после `requirements.lock.txt: OK`:

```bash
uv tool install --python 3.12 --index-url https://pypi.org/simple --constraints "$PKGDIR/requirements.lock.txt" "acp-gateway==$VERSION"
uv tool update-shell
export PATH="$(uv tool dir --bin):$PATH"
acpgw version --json
```

Ожидается `0.2.0rc2`. Явная версия выбирает release candidate; установка без
номера может выбрать старый стабильный `0.1.0`. При ошибке не добавляем `--force`.

## 5. Ubuntu: настроить dispatcher

```bash
acpgw setup --config-dir "$HOME/.config/acp-gateway" --role server --interactive
```

Основные ответы:

| Вопрос | Ответ |
| --- | --- |
| Local API/MCP port | `8765` |
| Connector listen address | `0.0.0.0` |
| Connector port / URL path | `18766` / `/acpgw/connect` |
| TLS certificate path | пусто |
| Allow unencrypted public test ingress? | yes для этого IP-теста |
| Agent type / alias | hermes / hermes |
| Working directory on the agent computer | выбранный точный Windows-путь |
| Computer ID | home |
| Add another agent? | no на первом проходе |
| Enable Telegram? | yes |
| Bot token | скрытый ввод |
| Allowed Telegram user IDs | числовой ID владельца |
| Connect this gateway to a local Hermes installation? | no |
| Install and start the systemd user service? | no, включим следующим шагом |
| Save / Export enrollment keys | yes / yes |

Ключ первого компьютера: `~/.config/acp-gateway/enrollment/home.key`.
Для следующих команд определить функцию; в новом SSH-сеансе повторить её:

```bash
gateway_cli() {
  acpgw --config "$HOME/.config/acp-gateway/config.yaml" --env-file "$HOME/.config/acp-gateway/.env" "$@"
}
gateway_cli config check
gateway_cli doctor
gateway_cli computers list
```

## 6. Ubuntu: включить службу

```bash
gateway_cli service install
gateway_cli service enable
gateway_cli service status
gateway_cli status
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

Ожидаются работающая служба и `Linger=yes`. Отдельный `serve/start` одновременно
со службой не запускаем. На firewall сервера и в панели VPS разрешаем TCP 18766
с внешних IP Windows-компьютеров. Правила выбираем после проверки действующего
firewall; SSH-доступ сохраняем. Порт 8765 остаётся закрытым снаружи.

## 7. Windows: установить uv и тот же пакет

Обычная PowerShell под пользователем Hermes, **не WSL**:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Открыть новое окно PowerShell:

```powershell
uv --version
$Version = '0.2.0rc2'
$PkgDir = Join-Path $env:LOCALAPPDATA "acpgw-packages\$Version"
New-Item -ItemType Directory -Force -Path $PkgDir | Out-Null
$Base = "https://github.com/Lujker/acp-gateway/releases/download/v$Version"
Invoke-WebRequest "$Base/requirements.lock.txt" -OutFile (Join-Path $PkgDir 'requirements.lock.txt')
Invoke-WebRequest "$Base/SHA256SUMS" -OutFile (Join-Path $PkgDir 'SHA256SUMS')
$Rows = @(Get-Content (Join-Path $PkgDir 'SHA256SUMS') | Where-Object { $_ -match '^([a-f0-9]{64})  requirements\.lock\.txt$' })
if ($Rows.Count -ne 1) { throw 'Missing or ambiguous runtime lock checksum' }
$Expected = ($Rows[0] -split '\s+')[0]
if ((Get-FileHash (Join-Path $PkgDir 'requirements.lock.txt') -Algorithm SHA256).Hash -ne $Expected) { throw 'Runtime lock checksum mismatch' }
uv tool install --python 3.12 --index-url https://pypi.org/simple --constraints (Join-Path $PkgDir 'requirements.lock.txt') "acp-gateway==$Version"
if ($LASTEXITCODE -ne 0) { throw 'Installation failed' }
uv tool update-shell
$Acpgw = Join-Path ((uv tool dir --bin).Trim()) 'acpgw.exe'
& $Acpgw version --json
& $Acpgw agents probe hermes --role desktop-serve --json
```

Ожидаются версия `0.2.0rc2` и Hermes `ready`. Hermes должен быть открыт под
тем же пользователем, provider/model настроены в Desktop.

## 8. Windows: получить ключ и настроить connector

Заменить `SSH_USER@SERVER_IP` реальными данными. Создаём **новый** каталог:

```powershell
$Cfg = Join-Path $env:LOCALAPPDATA 'acpgw-connector'
if (Test-Path -LiteralPath $Cfg) { throw 'Config already exists: inspect before continuing' }
New-Item -ItemType Directory -Path $Cfg | Out-Null
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls.exe $Cfg /inheritance:r /grant:r "*${Sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'
if ($LASTEXITCODE -ne 0) { throw 'Private directory ACL setup failed' }
scp SSH_USER@SERVER_IP:.config/acp-gateway/enrollment/home.key (Join-Path $Cfg 'home.key')
if ($LASTEXITCODE -ne 0) { throw 'Key transfer failed' }
& $Acpgw setup --config-dir $Cfg --role connector --interactive
```

В мастере: URL `ws://SERVER_IP:18766/acpgw/connect`, unencrypted test = yes,
computer ID `home`, key file — полный путь `$Cfg\home.key`, pin пустой.
Agent type/alias `hermes`, cwd точно как на VPS, existing instance `desktop-serve`.
Discovery directory взять из успешного probe (обычно
`%USERPROFILE%\.local\state\hermes\gateway-locks`), profile — фактический профиль
Hermes. Других агентов пока не добавлять. На установку logon task ответить no,
на сохранение yes.

## 9. Windows: включить connector и проверить связь

Этот блок определения путей и функции повторяется в новом окне PowerShell:

```powershell
$Cfg = Join-Path $env:LOCALAPPDATA 'acpgw-connector'
$Acpgw = Join-Path ((uv tool dir --bin).Trim()) 'acpgw.exe'
function Connector-Cli {
  & $Acpgw --config (Join-Path $Cfg 'config.yaml') --env-file (Join-Path $Cfg '.env') @args
}
Connector-Cli config check
Connector-Cli doctor --live
Connector-Cli service --role connector install
Connector-Cli service --role connector enable
Connector-Cli service --role connector status
```

На VPS: `gateway_cli computers status`. Ожидается online для `home`.
При ошибке: Windows `Connector-Cli service --role connector logs`,
VPS `gateway_cli service logs`. Не удаляем ключ/DB и не перевыпускаем enrollment
для исправления сетевой ошибки. Windows делает исходящее подключение;
входящий порт для connector на ПК не требуется.

## 10. Первый запрос и второй компьютер

В личном чате с ботом, по очереди:

```text
/start
/agent
/agent home/hermes
/new
```

Затем: `Ответь одним словом PONG`. Проверить ответ, сохранение чата в Hermes
и `/sessions`, `/model`. Это реальный вызов модели; настройки остаются в Hermes.
CLI и Telegram пока имеют раздельные диалоги. Повторять запрос после неизвестного
результата автоматически нельзя; сначала смотрим состояние.

Второй ПК подключаем после успешной проверки первого: отдельный computer ID
(например, `work`), отдельный ключ, фактический cwd второго ПК, route `work/hermes`.
Добавление профиля на dispatcher и запланированный restart выполняем отдельно,
после завершения активной работы. Старый ключ `home` на второй ПК не копируем.

Hermes должен запускаться при входе пользователя собственным механизмом
автозагрузки; задача Gateway запускает только connector. Reboot/logon, sleep,
network changes, approvals/model switching и update/rollback проверяются отдельно.
Перенос на другой сервер требует сохранения config/env/data/enrollment,
согласованного переключения и проверки состояния; заново создавать ключи
для простого изменения URL не требуется.

Источники: [uv](https://docs.astral.sh/uv/getting-started/installation/),
[поставка и обновления](distribution.md), [Windows connector](windows-connector.md),
[границы живой приёмки](p48-acceptance.md).
