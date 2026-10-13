# Самостоятельная установка 0.2.0rc1: VPS + Telegram + Windows Hermes

Эта инструкция предназначена для первого самостоятельного развёртывания.
Сначала установи один VPS и текущий Windows-компьютер, затем вернись с
результатами для совместной приёмки. Второй ПК подключается тем же способом,
но со своим computer ID и ключом.

**Используй готовый кандидат 0.2.0rc1.** Он пока не опубликован в PyPI/GitHub.
Обычная установка acp-gateway из реестра может дать старый 0.1.0. Последующие
изменения проекта не меняют уже проверенный wheel этого кандидата.

## 1. Что где работает

| Хост | Что запускается | Имя в примерах |
| --- | --- | --- |
| Linux VPS | Dispatcher, owner API, Telegram polling | пользователь gwuser |
| Windows, обычный пользователь | Уже установленный Hermes Desktop + ACP Gateway connector | computer ID home |
| Telegram | Личный чат владельца с ботом | route home/hermes |

VPS не запускает Hermes и не хранит provider keys. Windows не запускает
вторую копию Telegram polling. Owner API остаётся на 127.0.0.1:8765;
команды управления диспетчером выполняются по SSH на VPS.

На обоих хостах используется один wheel. Для native Windows запускай обычную
PowerShell под тем же пользователем, что Hermes, **не WSL**.

## 2. Подготовь значения

Заменяй примеры своими значениями, сохраняя согласованность двух конфигураций:

| Значение | Пример |
| --- | --- |
| SSH VPS | gwuser@203.0.113.10 |
| Первый прямой адрес | ws://203.0.113.10:18766/acpgw/connect |
| Постоянный адрес | wss://gateway.example.com/acpgw/connect |
| Внутренний listener | 18766, path /acpgw/connect |
| Computer ID | home |
| Local agent alias | hermes |
| Route на VPS/в боте | home/hermes |
| Рабочая папка Hermes | C:\Users\YOUR_USER\work |
| Telegram allowed user ID | твой числовой user ID, например 123456789 |

Рабочая папка должна существовать в Windows и совпадать в локальном и VPS
профилях. В WSS URL не используются https:// и /api: это именно WebSocket
connector path. На первом этапе указывай в allowlist одного владельца.

Подготовь token от BotFather. Ввести его нужно скрытым вводом мастера на VPS;
в YAML и команды он не вставляется. Если этот бот уже где-то работает, останови
его прежний polling. Старый webhook тоже мешает polling; Gateway сам его не
удаляет. При конфликте webhook вернись с ошибкой, не сбрасывай его вслепую.

## 3. Возьми проверенный комплект

На текущем компьютере комплект находится здесь:

    \\wsl.localhost\Ubuntu-22.04\home\user\projects\acp-gateway\dist\acpgw-0.2.0rc1-acceptance.tar.gz

Рядом находится файл с тем же именем и окончанием .sha256. Копируй оба файла
из одной актуальной выдачи. Внутри есть wheel, source distribution, bundle с
install scripts и runtime lock, manifest, JUnit и инструкция.

Для переноса на VPS из WSL:

~~~bash
ARTIFACT=/home/user/projects/acp-gateway/dist/acpgw-0.2.0rc1-acceptance.tar.gz
scp "$ARTIFACT" "$ARTIFACT.sha256" gwuser@203.0.113.10:~/
~~~

Адрес и логин замени. Если SSH использует нестандартный порт, для ssh нужен
-p PORT, для scp — -P PORT, либо заранее настроенный SSH Host alias.
Подключись по SSH **обычным пользователем**, не запускай
Gateway от root. Для systemd user нужен полноценный пользовательский сеанс;
nginx/firewall/lingering настраиваются через sudo при наличии прав.

## 4. VPS: распакуй комплект и установи uv/Gateway

На VPS, из каталога с загруженными файлами:

~~~bash
sha256sum -c acpgw-0.2.0rc1-acceptance.tar.gz.sha256
mkdir -p "$HOME/acpgw-packages/0.2.0rc1"
tar -xzf acpgw-0.2.0rc1-acceptance.tar.gz -C "$HOME/acpgw-packages/0.2.0rc1"
CANDIDATE="$HOME/acpgw-packages/0.2.0rc1/acpgw-0.2.0rc1-acceptance"
(cd "$CANDIDATE" && sha256sum -c SHA256SUMS)
tar -xzf "$CANDIDATE/acpgw-0.2.0rc1-bundle.tar.gz" -C "$HOME/acpgw-packages/0.2.0rc1"
RELEASE="$HOME/acpgw-packages/0.2.0rc1/acpgw-0.2.0rc1"
~~~

Проверка должна дать OK. Если checksum не совпал — не устанавливай: проверь,
что архив и sidecar относятся к одной копии.

Если uv отсутствует, установи его официальным способом:

~~~bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
~~~

Установи wheel с ограничениями версий из комплекта:

~~~bash
uv tool install --python 3.12 --constraints "$RELEASE/requirements.lock.txt" "$RELEASE/acp_gateway-0.2.0rc1-py3-none-any.whl"
uv tool update-shell
export PATH="$(uv tool dir --bin):$PATH"
acpgw version --json
~~~

Ожидаемая версия — 0.2.0rc1. Если uv сообщает, что tool уже установлен,
не добавляй --force на работающую службу: см. раздел повторной настройки ниже.

## 5. VPS: мастер server и Telegram

~~~bash
acpgw setup --config-dir "$HOME/.config/acp-gateway" --role server --interactive
~~~

На первом запуске ответь так:

| Вопрос мастера | Ответ |
| --- | --- |
| Local API/MCP port | 8765 |
| Connector listen address | 0.0.0.0 для первого IP-теста; 127.0.0.1 при работе только через nginx |
| Connector port | 18766 |
| Connector URL path | /acpgw/connect, либо твой заранее выбранный path |
| TLS certificate path | пусто, если TLS завершает nginx |
| Allow unencrypted public test ingress? | yes только для временного IP-теста |
| Agent type | hermes |
| Agent alias | hermes |
| Display name | Home Hermes |
| Working directory on the agent computer | точный Windows cwd из раздела 2 |
| Computer ID | home |
| Add another agent? | no |
| Enable Telegram? | yes |
| Telegram bot token from BotFather | скрытый ввод token |
| Allowed Telegram user IDs | твой числовой ID |
| Connect this gateway to a local Hermes installation? | no — Hermes находится на Windows |
| Install and start the systemd user service? | no на первом проходе, включим явно ниже |
| Save these settings? | yes |
| Export enrollment keys for new configured computers? | yes |

Мастер сохранит config.yaml, .env и setup.json. Ключ для Windows будет
экспортирован в:

    ~/.config/acp-gateway/enrollment/home.key

Если computer уже зарегистрирован, мастер сохраняет прежний ключ и сообщает
already enrolled. Это не выпуск нового ключа. Не удаляй SQLite ради повторного
setup и не запускай rotate без необходимости.

Для всех следующих VPS команд используй функцию с явными путями:

~~~bash
gateway_cli() {
  acpgw --config "$HOME/.config/acp-gateway/config.yaml" --env-file "$HOME/.config/acp-gateway/.env" "$@"
}
gateway_cli config check
gateway_cli doctor
gateway_cli computers list
~~~

Функцию нужно заново определить в новом SSH-сеансе или сохранить в своём shell
profile. Config/keys/data находятся вне unpacked release и uv tool environment.

## 6. VPS: служба и доступность listener

~~~bash
gateway_cli service install
gateway_cli service enable
gateway_cli service status
gateway_cli status
gateway_cli service logs
~~~

Enable включает автозапуск и запускает сейчас. Не запускай одновременно
gateway_cli start/serve в терминале и ту же службу.

Чтобы systemd user запускался после reboot без SSH-login:

~~~bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
~~~

Ожидается Linger=yes. Если нет user bus или systemctl --user не работает,
сохрани ошибку и вернись к нам; не заменяй user service случайной root-службой.

Для временного IP-теста firewall/VPS security group должны пропускать TCP 18766
с твоего адреса. Owner API 8765 наружу не открывается. Connector сам делает
исходящее соединение; входящий порт на Windows для него открывать не требуется.

## 7. Windows: установи тот же wheel

Перейди в обычную PowerShell. Если uv отсутствует:

~~~powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
~~~

Открой новое окно PowerShell и проверь uv --version.

Скопируй два файла комплекта, распакуй и установи wheel:

~~~powershell
$ArchiveSource = '\\wsl.localhost\Ubuntu-22.04\home\user\projects\acp-gateway\dist\acpgw-0.2.0rc1-acceptance.tar.gz'
$Archive = Join-Path $env:USERPROFILE 'Downloads\acpgw-0.2.0rc1-acceptance.tar.gz'
Copy-Item -LiteralPath $ArchiveSource -Destination $Archive
Copy-Item -LiteralPath ($ArchiveSource + '.sha256') -Destination ($Archive + '.sha256')
$Expected = ((Get-Content -LiteralPath ($Archive + '.sha256') -Raw).Trim() -split '\s+')[0]
if ((Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash -ne $Expected) {
    throw 'Archive checksum mismatch'
}
$ReleaseRoot = Join-Path $env:LOCALAPPDATA 'acpgw-packages\0.2.0rc1'
New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
tar.exe -xzf $Archive -C $ReleaseRoot
if ($LASTEXITCODE -ne 0) { throw 'Archive extraction failed' }
$Candidate = Join-Path $ReleaseRoot 'acpgw-0.2.0rc1-acceptance'
tar.exe -xzf (Join-Path $Candidate 'acpgw-0.2.0rc1-bundle.tar.gz') -C $ReleaseRoot
if ($LASTEXITCODE -ne 0) { throw 'Bundle extraction failed' }
$Release = Join-Path $ReleaseRoot 'acpgw-0.2.0rc1'
uv tool install --python 3.12 --constraints (Join-Path $Release 'requirements.lock.txt') (Join-Path $Release 'acp_gateway-0.2.0rc1-py3-none-any.whl')
if ($LASTEXITCODE -ne 0) { throw 'Gateway installation failed' }
uv tool update-shell
$Acpgw = Join-Path ((uv tool dir --bin).Trim()) 'acpgw.exe'
& $Acpgw version --json
~~~

Ожидается 0.2.0rc1. Это native uv tool, не .venv проекта в WSL.
Существующую глобальную установку сначала проверь по разделу 14.

## 8. Windows: private config directory и ключ

Создай отдельный новый каталог. Команды ACL ниже предназначены именно для
нового каталога: существующую установку ими не переделывай.

~~~powershell
$Cfg = Join-Path $env:LOCALAPPDATA 'acpgw-connector'
if (Test-Path -LiteralPath $Cfg) {
    throw 'Config directory already exists; use the repeat-setup section'
}
New-Item -ItemType Directory -Path $Cfg | Out-Null
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls.exe $Cfg /inheritance:r /grant:r "*${Sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'
if ($LASTEXITCODE -ne 0) { throw 'Private directory ACL setup failed' }
scp gwuser@203.0.113.10:.config/acp-gateway/enrollment/home.key (Join-Path $Cfg 'home.key')
if ($LASTEXITCODE -ne 0) { throw 'Enrollment key transfer failed' }
~~~

Ключ копируется файлом с VPS, не вставляется в YAML/чат/аргументы. Каталог
разрешает доступ текущему пользователю, SYSTEM и Administrators.
Если нет scp.exe, скопируй home.key своим SFTP-клиентом прямо в этот каталог.

Создай рабочую папку Hermes, если её ещё нет; пример нужно заменить на свой:

~~~powershell
$WorkDir = Join-Path $env:USERPROFILE 'work'
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
~~~

## 9. Windows: запусти Hermes и мастер connector

Открой уже установленный Hermes Desktop. Убедись, что в нём настроен хотя бы
один provider/model. Сначала проверь identity без создания чата:

~~~powershell
& $Acpgw agents probe hermes --role desktop-serve --json
~~~

Ожидается ready. Стандартный rendezvous directory:
%USERPROFILE%\.local\state\hermes\gateway-locks.
Переопределение возможно через HERMES_GATEWAY_LOCK_DIR. Это **не**
%LOCALAPPDATA%\hermes\locks. В мастере выбери фактический каталог probe.

При missing/offline проверь, что Hermes открыт под этим же Windows-пользователем.
При auth-required не отключай ACL/auth проверки; сохрани вывод для уточнения.

Запусти мастер:

~~~powershell
& $Acpgw setup --config-dir $Cfg --role connector --interactive
~~~

| Вопрос мастера | Ответ |
| --- | --- |
| Server connection URL | ws://IP:18766/acpgw/connect для первого IP-теста, либо сразу твой WSS URL |
| Use this unencrypted test connection? | yes только для временного IP-теста |
| Computer ID registered on the server | home |
| Enrollment key file copied privately from the server | полный путь к $Cfg\home.key |
| Server SHA-256 pin | пусто для нормального CA/domain certificate |
| Agent type | hermes |
| Agent alias | hermes |
| Display name | Home Hermes |
| Working directory on the agent computer | тот же точный cwd, что на VPS |
| Hermes discovery directory | каталог, в котором probe дал ready |
| Existing Hermes instance | desktop-serve |
| Hermes profile | default либо фактический настроенный profile |
| Add another agent? | no |
| Install and start the Windows connector logon task? | no на первом проходе |
| Save these settings? | yes |

Windows роль connector автоматически отключает Telegram polling.
Никакой URL/secret-key Goose здесь вводить не нужно. Credentials модели остаются
в Hermes. Мастер сохраняет connection в setup.json, data/logs — в private
каталоге рядом с config.

Теперь определи wrapper — повторяй этот блок в новом PowerShell-сеансе:

~~~powershell
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
Connector-Cli service --role connector logs
~~~

Enable включает logon task и запускает сейчас. Task Running означает процесс,
а соединение проверяй на VPS:

~~~bash
gateway_cli computers status
~~~

Ожидаются computer home online и route home/hermes. Если offline, смотри logs
коннектора, URL, firewall, computer ID и соответствие enrollment key.
Standalone command acpgw dispatcher — диагностический listener; вместо него
нужна уже запущенная интегрированная gateway service на VPS.

## 10. Domain/path и переход с IP на WSS

Если nginx/HTTPS уже настроен, добавь location внутрь существующего TLS server.
Path должен совпадать с connector.connect_path на VPS:

~~~nginx
location = /acpgw/connect {
    proxy_pass http://127.0.0.1:18766;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
~~~

В proxy_pass не добавляй другой path: показанный вариант сохраняет исходный.
Пример предполагает nginx на том же VPS. Внешний proxy должен иметь собственный
маршрут к listener; 127.0.0.1 на другом хосте указывает не на этот VPS.

~~~bash
sudo nginx -t
sudo systemctl reload nginx
~~~

Если nginx -t не прошёл, сначала исправь конфигурацию, не делай reload.
На постоянном маршруте нужен действующий сертификат на имя домена.

На Windows сначала отключи задачу и поменяй только connection:

~~~powershell
Connector-Cli service --role connector disable
Connector-Cli connect
~~~

Укажи wss://gateway.example.com/acpgw/connect, прежний home и прежний home.key.
Новый key не нужен. Pin оставь пустым при нормальной CA-проверке.
Если мастер предлагает установить logon task, можно ответить no и затем явно:

~~~powershell
Connector-Cli service --role connector install
Connector-Cli service --role connector enable
Connector-Cli doctor --live
~~~

На VPS снова проверь gateway_cli computers status.

После успешного WSS подключения можно закрыть прямой публичный тестовый порт.
Если nginx работает на этом же VPS, в gateway_cli config edit --section gateway
поменяй listener host на 127.0.0.1, оставь port/path и пустой TLS certificate;
подтверди сохранение и восстановление службы. Доменные 443 и proxy остаются.
Закрой доступ к 18766 в firewall/security group; owner API 8765 всегда loopback.

**Делай переход IP → WSS до создания рабочих /new.** Перезапуск Windows connector
не восстанавливает прежние handles Hermes. Если тестовые чаты уже были, они
остаются записаны, но продолжение может потребовать явного нового диалога.
Никакой prompt при смене URL автоматически не повторяется.

## 11. Автозапуск Hermes при входе в Windows

Gateway управляет своей задачей, но не включает автозапуск стороннего Hermes.

1. Если Hermes есть в Параметры → Приложения → Автозагрузка, включи его там.
2. Иначе открой Win+R → shell:appsfolder.
3. Вторым окном Win+R → shell:startup.
4. Перенеси ярлык установленного Hermes из первого окна во второе.
5. Используй автозагрузку текущего пользователя, не shell:common startup.

Это штатный [порядок Microsoft](https://support.microsoft.com/en-gb/windows/experience/startup-boot/configure-startup-applications-in-windows).
Если приложение уже запускается своим механизмом, второй ярлык не добавляй.
Не создавай задачу запуска случайного hermes.exe и не запускай Desktop как SYSTEM.

Connector уже включён через service --role connector enable. Его задача называется
ACP Gateway Connector-<SID текущего пользователя> и стартует после входа,
а не до logon. После следующего обычного входа открой Hermes/проверь probe,
Connector-Cli service --role connector status и VPS computers status.
Физический reboot/sleep/network разберём отдельно после первой установки.

## 12. Первый разговор через Telegram и CLI

В личном чате с ботом:

    /start
    /agent
    /agent home/hermes
    /new
    /sessions
    /model

После выбора диалога можно отправить обычный текст, например:
«Ответь одним словом PONG». Это уже настоящий вызов настроенной модели Hermes.
Проверь ответ и сохранение именно этого чата в Desktop; оставь Desktop читателем.

Tool approval появится только если его запросит сам Hermes. Gateway не включает
новую политику подтверждений в Hermes. Для pending cards есть /approvals; только
allow once/reject once. Без запроса Hermes отсутствие кнопки не является ошибкой.

CLI работает через SSH на VPS:

~~~bash
gateway_cli new --agent home/hermes
gateway_cli sessions --agent home/hermes
gateway_cli ask --agent home/hermes 'Ответь одним словом PONG'
gateway_cli status
~~~

**CLI и Telegram пока имеют раздельные conversations.** Пустой CLI sessions
после /new в Telegram — ожидаемое поведение. IDs из Telegram нельзя подставлять
в CLI model set: ownership отвергнет такой запрос. Shared default модели
общий для route/profile, но это не объединение чатов.

Команды модели в Telegram:

    /models
    /model
    /model set PROVIDER MODEL ID REVISION
    /model reconcile ID REVISION
    /model state
    /model default PROVIDER MODEL
    /model default
    /model default clear

ID и REVISION бери из ответа /model. При confirmation_required повтори то же
действие с выданным TOKEN в конце. После switch/смены route старый token не годится.

Аналог CLI для собственного CLI-диалога:

~~~bash
gateway_cli model list --agent home/hermes --session ID
gateway_cli model set PROVIDER MODEL --agent home/hermes --session ID --revision REVISION
gateway_cli model reconcile --agent home/hermes --session ID --revision REVISION
gateway_cli model state --agent home/hermes --session ID
gateway_cli model default set PROVIDER MODEL --agent home/hermes
~~~

Здесь ID и active_revision бери из gateway_cli sessions. Accepted/deferred — не
обещание выполненного запроса выбранным provider: show/readback даёт observation.
Deferred может ждать собственного следующего хода; unknown не исправляется
повторной отправкой set. При unknown сохрани state/journal и вернись для разбора.

## 13. Повседневное управление

VPS:

~~~bash
gateway_cli service status
gateway_cli service logs
gateway_cli service restart
gateway_cli computers status
~~~

Windows:

~~~powershell
Connector-Cli service --role connector status
Connector-Cli service --role connector logs
Connector-Cli maintenance --role connector status
Connector-Cli connector-sessions list --agent hermes
Connector-Cli connector-sessions journal HANDLE --agent hermes
~~~

Planned stop делай после завершения jobs/approvals/model work. Не используй
--force как обычный способ остановки. service status работает и при выключенном
процессе; owner API status на VPS требует работающий daemon.

В Hermes MVP безопасный /stop/cancel и cold restore не поддерживаются.
Остановка connector/Hermes не отменяет работу upstream гарантированно.
Не удаляй DB, locks или старые чаты для «лечения» uncertainty.

## 14. Повторная настройка и последующее обновление

Не повторяй начальную установку поверх работающей службы. Сначала version --json
и service status; config/env/keys/data остаются прежними.

Для текущего 0.2.0rc1 при следующем предоставленном проверенном wheel:

~~~bash
gateway_cli update --from /absolute/path/NEXT.whl --constraints /absolute/path/requirements.lock.txt --with-services
gateway_cli update --status
~~~

На Windows:

~~~powershell
Connector-Cli update --from C:\path\NEXT.whl --constraints C:\path\requirements.lock.txt --with-services
Connector-Cli update --status
~~~

Queued ещё не означает завершение. Реальную приёмку этого lifecycle на целевых
службах проведём совместно. Не используй эти NEXT примеры без нового wheel.

Если глобально установлен прежний 0.1.0, сначала штатно останови его daemon/
службу и сохрани config/env/data; Windows connector старой версии может ещё не
поддерживаться. После подтверждённой остановки uv tool install --force с тем же
wheel/constraints допустим для замены tool environment, затем запускай мастер
с выбранным config-dir. Если состояние прежней установки непонятно — вернись
с version/status, не удаляй данные и не форсируй замену.

Для повторной Windows настройки действующей версии:

~~~powershell
Connector-Cli service --role connector disable
Connector-Cli config edit
~~~

Сохрани нужные прежние значения; не меняй computer ID/key/profile случайно.
После редактирования launch options переустанови stopped task и включи:
service --role connector install, затем enable.

## 15. С чем вернуться для совместных проверок

Достаточно сообщить, где остановился, и приложить вывод:

- VPS: acpgw version --json, gateway_cli config check, doctor --live,
  service status, computers status и последние service logs.
- Windows: version --json, Connector-Cli config check, doctor --live,
  service --role connector status/logs, agents probe hermes --role desktop-serve --json.
- Что ответил бот на /start, /agent, /new, /model и первый текст.
- Какие два URL пробовал и на каком route работал.

Не присылай .env, home.key, Hermes token, provider keys или SQLite.
Встроенные diagnostics маскируют зарегистрированные secrets; перед отправкой
вывода всё равно проверь, что туда случайно не попала команда с token.

Дальше вместе проверим реальные approvals/model switching/defaults,
reconnect, logon/reboot/sleep/network, update/rollback и второй компьютер.

## Источники и границы

Команды Gateway сверены с candidate 0.2.0rc1; source ref
3da8dffbd5d265c0b135e3aa1bd212e8e618c779. Runtime wheel остаётся неизменным.
Общая матрица — [приёмка](p48-acceptance.md), подробности —
[Windows connector](windows-connector.md) и [VPS без Docker](vps-native-hermes.ru.md).
Официальная установка uv: [Astral](https://docs.astral.sh/uv/getting-started/installation/),
команды сверены через Context7. Это инструкция развёртывания, не заявление,
что твоя установка и живой маршрут уже прошли приёмку.
