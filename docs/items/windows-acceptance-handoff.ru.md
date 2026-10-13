# Передача P4.8.4 агенту в Windows

**Статус:** PARTIAL — native Scheduler/ACL/locks и Hermes attach проверены;
остаток P4.8.4 указан в результатах ниже.

## Результат единого прогона 2026-10-13

**221 passed, 3 skipped** на Windows 10 build 19045, CPython 3.12.13,
PowerShell 5.1, Hermes Desktop 0.21.6. Все **13 обязательных native Scheduler
cases** выполнены без skip. Три skips — только POSIX permissions/ownership.
Пакет установлен в отдельное venv из preview wheel SHA256
`3f2f39d15599c1a8bb0c4ca349855bd05dcd039edb36cda0ba7aa23a8c54f70b`.

Пройдены ACL, ownership/locks, graceful stop/force/fencing, Hermes ACP/relay,
bindings после рестарта, setup/doctor и Goose explicit probe на mocks.
Настоящий Desktop ready до/после на том же endpoint; профиль default выбран
мастером без сохранения. Model prompts/approval decisions настоящему Hermes
не отправлялись, его ACL и конфигурация не менялись.

Первый прогон выявил OWNER RIGHTS в private DACL Windows Python: исправлена
проверка с обязательным trusted owner. Затем исправлены только тестовые
ожидания symlink и слишком широкий mock поиска программ, скрывавший PowerShell.
Тестовые задачи удаляются fixture cleanup; рабочая задача не изменяется.

Полный report.json, tests.xml и stage logs:
`C:\Users\vladz\AppData\Local\Temp\acpgw-windows-acceptance-_9t5i9w9`.
Копия отчёта включена в `dist/p4.8-preview/native-acceptance.json`.
Отдельно остаются reboot/logon, физический sleep/wake/network, полный package
update lifecycle, реальные approvals и VPS/два ПК/Telegram, live Goose/matrix.

## Единый финальный прогон (2026-10-13)

После первого успешного пакета добавлены selection guards, saved connector
launch и HermesSessionManager. Текущий старый preview относится к прежним
221 native tests; его нельзя считать поставкой нового кода. Новый расширенный
runner включает Telegram, saved-launch и live-manager tests. Перед следующим
единым native прогоном нужен wheel именно завершённых новых исходников с
обновлёнными evidence/hashes. Финальная сборка по решению владельца выполняется
после функциональных доработок, а не после каждого промежуточного этапа.

После отключения sandbox в текущем окружении появился WSLInterop; Windows
PowerShell 5.1 запускается из WSL. Отдельный Windows-агент больше не обязателен:
важно использовать нативный Windows Python под пользователем Hermes Desktop.
Описанное ниже отсутствие interop — историческое состояние предыдущего этапа.

После подготовки финального `dist/p4.8-preview`, из корня актуального проекта
в Windows PowerShell выполнить одну команду:

```powershell
uv run --no-project --python 3.12 --managed-python python scripts/accept_windows_track.py
```

Runner проверяет SHA256 артефактов и совпадение с протестированным wheel,
создаёт отдельное Windows-окружение и устанавливает wheel с locked dependencies.
Затем проверяет импорт из site-packages, observer-доступ к настоящему Desktop
до/после, выбор существующего backend мастером без сохранения конфигурации,
23 opt-in Scheduler/ACL/force/setup-rollback cases, graceful subprocess, bindings/recovery,
Hermes ACP/relay/setup и Goose probe на mocks. Все 23 Windows cases обязаны
выполниться без skip. Отчёт `report.json`, JUnit и логи сохраняются в новом
каталоге `%TEMP%\acpgw-windows-acceptance-*`; его путь выводится в конце.
Можно передать `--output-dir C:\new\acceptance` и `--hermes-lock-dir ...`.

Runner не отправляет model prompts в настоящий Hermes, не отвечает на его
approvals и не изменяет рабочую задачу connector. Временные тестовые задачи
имеют уникальные имена. Reboot/logon, физический sleep/network, настоящие
approvals и VPS/два ПК/Telegram остаются отдельной живой приёмкой.
Ниже сохранены первоначальные команды и результаты для сопоставления.

## Почему проверка передавалась

Текущая среда использует ядро `6.18.33.2-microsoft-standard-WSL2` и видит
Windows-файлы через `/mnt/c`. Но в доступном окружении процесса агента нет
`/proc/sys/fs/binfmt_misc/WSLInterop`; каталог `binfmt_misc` пуст. Запуск
`/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe` завершился
`OSError: [Errno 8] Exec format error`, включая попытку вне sandbox.
Точная причина отсутствия interop в этой среде не установлена.

Это ограничение текущего окружения агента, а не WSL вообще. При работающем
[WSL interop](https://learn.microsoft.com/en-us/windows/wsl/filesystems#run-windows-tools-from-linux)
можно вызвать Windows PowerShell/Python из WSL и проверить native Windows код.
Для такой приёмки Python должен быть Windows Python (`sys.platform == 'win32'`),
даже если запуск инициирован из WSL. Перезапуск WSL или изменение системного
binfmt для этой проверки не выполнялись. Пользователь предложил запустить
агента непосредственно в Windows; это позволяет проверить тот же код без
зависимости от текущего interop.

## Задача Windows-агенту

Продолжить P4.8.4: проверить development Windows connector Task Scheduler
adapter, исправить обнаруженные проблемы и записать фактические результаты
в `road-notes.md` и `road-map.md`. Прочитать
`docs/setup/windows-connector.md` и ограничения в `docs/setup/hermes-attach.md`.

Исходники текущей работы находятся в
`/home/vzelenskiy/projects/acp-gateway`. Значительная часть изменений пока
не закоммичена, включая новый `src/acp_gateway/windows_service.py` и тесты.
Обычный clone/pull может их не содержать. Использовать именно актуальное
рабочее дерево: доступ к нему через Windows/WSL share либо Windows-копию
с сохранением изменённых и новых файлов. Не сбрасывать существующие изменения.
Не использовать Linux `.venv` как Windows environment.

Запускать под тем же Windows-пользователем, что и Hermes Desktop. Сначала
подтвердить `sys.platform == 'win32'`, наличие Windows PowerShell и доступ к
Scheduler COM. Установленный Hermes и его настройки не требуется изменять.

## Первый прогон

В native PowerShell из актуальной копии проекта, с установленным Windows uv:

```powershell
# Отдельное Windows-окружение, в том числе при чтении проекта через WSL share.
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA 'acpgw-windows-acceptance\.venv'
uv sync --frozen --group dev
uv run --frozen --group dev python -c "import sys; print(sys.platform, sys.executable); assert sys.platform == 'win32'"
$env:ACPGW_TEST_WINDOWS_TASK = '1'
try {
    uv run --frozen --group dev python -m pytest -q tests/integration/test_windows_connector_task.py
} finally {
    Remove-Item Env:\ACPGW_TEST_WINDOWS_TASK -ErrorAction SilentlyContinue
}
```

Тест создаёт уникальную временную задачу `ACP Gateway Test-<uuid>-<SID>`,
временные приватные файлы и соединяется с закрытым loopback port. Проверяет
install disabled → enable/running → отказ reinstall running → restart →
disable → logs → uninstall, с cleanup в finally. Production task и Hermes
не должны участвовать. Если тест падает, проверить cleanup и записать причину;
не считать skipped успешной native приёмкой.

Затем проверить отдельно:

1. Отказ установки при публичных ACL config/env/key/log; отсутствие изменений
   задач при таком отказе. На 2026-10-13 отдельный discovery preflight уже
   проверяет ACL rendezvous файлов Hermes до чтения токена; новая native
   приёмка ещё требуется, см. дополнение ниже.
2. Отказ управления чужой задачей/другой установкой и запрет дублей между
   foreground и Task Scheduler процессами с одним `data_dir`.
3. Setup и package maintenance требуют disable enabled/queued/running task.
4. Реальное attach к работающему Hermes: observer probe без model prompt,
   затем отдельная dedicated conversation для диспетчера. Не захватывать
   существующий пользовательский Desktop-диалог для записи.
5. Остановка коннектора сохраняет работающий Desktop. Текущий Scheduler stop
   немедленный; graceful drain ещё не реализован, неизвестный результат prompt
   нельзя автоматически повторять.
6. Reconnect после обрыва сети/sleep-wake. Reboot + вход пользователя проверить
   отдельным этапом; logon-task не обещает работу до входа пользователя.

Для каждого пункта зафиксировать версию Windows/Python/Hermes, команду,
результат и оставшиеся ограничения без credentials. VPS/Telegram и второй ПК
остаются отдельной P4.8.5 приёмкой; один локальный успешный тест их не закрывает.

## Следующий Windows-прогон: discovery ACL, 2026-10-13

Результаты первого Windows-этапа ниже приняты. Новая разработка добавляет
`src/acp_gateway/windows_acl.py` и вызывает его из Hermes discovery перед
чтением record/token. Проверка read-only, без Scheduler/изменения ACL, принимает
только пути через JSON stdin; сбой закрывает discovery с auth-required.
В production async probe/transport она работает вне event loop. Native-tested
service helper не менялся.

Повторить из актуального рабочего дерева с Windows Python и
`ACPGW_TEST_WINDOWS_TASK=1`:

```powershell
uv run --frozen --group dev python -m pytest -q tests/integration/test_windows_connector_task.py tests/unit/test_windows_acl.py tests/unit/test_hermes_discovery.py
uv run --frozen --group dev python -m acp_gateway agents probe hermes --role desktop-serve --json
```

В opt-in suite добавлены четыре `test_native_hermes_discovery_acl_before_token_read`
cases: private endpoint принимается; публичный record/token и writable directory
отклоняются до чтения bytes. Это только временные файлы теста. Дополнительно
повторить observer probe настоящего Hermes: если реальные ACL отвергнуты,
зафиксировать только principals/rights и причину, без token/истории; не менять
ACL установленного Hermes автоматически. Успех предыдущей приёмки не считается
успехом этих новых cases. После прогона снять opt-in env flag, как указано выше.

## Дополнение: graceful stop и проверка поставки, 2026-10-13

После первой Windows-приёмки изменился сам Scheduler stop path. Теперь штатные
stop/disable/restart/uninstall посылают локальный запрос и ждут exit 0 до
20 секунд. Runtime record содержит свежий instance ID и control ID задачи;
stale request не останавливает следующий запуск, а manual connector не считается
экземпляром task. Relay/attachments закрываются, чужие approvals не решаются,
Hermes interrupt/resume не вызываются. Успешный helper возвращает
`stopMode=graceful`. Таймаут даёт ошибку; kill возможен только через явный
`--force`. Старую задачу без control ID остановить с `--force` и переустановить.

В native suite обновлены graceful assertions и добавлен
`test_native_explicit_force_and_restart_instance_fencing`. Повторить suite:
13 opt-in Windows cases (8 первоначальных + 4 discovery ACL + 1 force/fencing).
Cleanup временных задач использует явный force, чтобы ошибка теста не оставляла
test task. Production task не участвует.

Подготовлена preview-поставка в `dist/p4.8-preview`: wheel, sdist, runtime/dev
locked requirements, `SHA256SUMS`, `PREVIEW.json`. Это текущий dirty working tree,
версия 0.1.0 **не означает публикацию нового релиза**. Не устанавливать preview
поверх production Gateway; проверить в отдельном native Windows environment.

Из корня актуального рабочего дерева в native PowerShell:

```powershell
$preview = (Resolve-Path '.\dist\p4.8-preview').Path
$wheelEnv = Join-Path $env:LOCALAPPDATA ('acpgw-p48-wheel-check-' + [Guid]::NewGuid().ToString('N'))
uv venv $wheelEnv --python 3.12 --managed-python
$wheelPython = Join-Path $wheelEnv 'Scripts\python.exe'
uv pip install --python $wheelPython -r "$preview\requirements.dev.lock.txt" "$preview\acp_gateway-0.1.0-py3-none-any.whl" --no-deps
& $wheelPython -I -c "import acp_gateway; print(acp_gateway.__file__); assert 'site-packages' in acp_gateway.__file__"
$env:ACPGW_TEST_WINDOWS_TASK = '1'
try {
    & $wheelPython -m pytest -q tests/integration/test_windows_connector_task.py tests/unit/test_windows_service.py tests/unit/test_windows_acl.py tests/unit/test_connector_lifecycle.py tests/unit/test_connector_commands.py tests/integration/test_connector_graceful_stop.py tests/integration/test_goose_probe.py
} finally {
    Remove-Item Env:\ACPGW_TEST_WINDOWS_TASK -ErrorAction SilentlyContinue
}
& $wheelPython -m acp_gateway agents probe hermes --role desktop-serve --json
```

Не использовать `uv run` для этого wheel acceptance: он может восстановить
editable installation из checkout. Вызов именно `$wheelPython` проверяет wheel,
а tests берутся из рабочего дерева. До установки сверить SHA256 wheel/requirements
с `SHA256SUMS`; записать digest в отчёт. При импорте из `src` вместо site-packages
пакетная приёмка недействительна. Новая Goose диагностика — explicit URL, без
автопоиска Desktop; см. `docs/setup/goose-attach.md`.

## Исторический результат Linux-этапа перед первой Windows-приёмкой

109 профильных тестов сервисных команд, установки/обслуживания, wizard и
secret scanner прошли. Ruff check/format чистые. В полном прогоне 886 тестов
прошли, 4 пропущены, единственный отказ scanner на двух статических тестовых
значениях исправлен и перепроверен профильным набором. Эти результаты
не доказывают корректность PowerShell/COM/ACL на реальной Windows.

## Результат Windows-этапа 2026-10-12

Нативная приёмка выполнена: **33 passed** профильных Windows теста и
**72 passed** Hermes regression. Временных задач после cleanup: **0**.
Исправлены наследование модулей PS7, ACL helper и Windows lock-file assertion.
Реальный Hermes 0.21.6 observer/dedicated conversation без model prompt прошёл;
Desktop оставался доступен. Фактические команды, среда и границы доказательств
записаны в [road-notes.md](../../road-notes.md).

Остаются reboot/logon, физический sleep/wake и смена сети, Hermes rendezvous/token
ACL, graceful drain и устанавливаемая поставка. VPS/Telegram/второй ПК — P4.8.5.
