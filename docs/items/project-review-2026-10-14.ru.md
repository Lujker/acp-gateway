# Независимое ревью ACP Gateway — 2026-10-14

**Статус:** READY — независимое ревью и исправление всех трёх подтверждённых находок завершены; source/native регрессии прошли.

## Запрос, база и границы

Владелец запросил обзор всего проекта отдельным агентом GPT-6 Astra,
проверку найденных проблем, запись результатов и исправление подтверждённых
дефектов. Перед запуском ревью записаны будущие P4.15/P4.16 в roadmap и
[контракт будущего управления](remote-connector-management.ru.md).
Их runtime реализация в этот проход не выполнялась.

База независимого ревью: `95322da`, branch feat/vps-acceptance, опубликованный rc7.
Исходное WSL-дерево было чистым; известное Windows-only изменение режима hook
сохранено. Production VPS/коннекторы/Hermes и переписка пользователя не
использовались для тестов. Release tags/assets rc7 не менялись.

Ревьюер: отдельный subagent `astra_project_review`, модель `gpt-6-astra`.
Он сделал широкий обзор критических ветвей runtime-подсистем, installation/
service/release/deployment, два безопасных воспроизведения и повторный просмотр
исправлений. Это не формальное доказательство отсутствия дефектов и не заявление
о построчном прочтении каждого fixture/smoke-файла.
Первичный отчёт, coverage, ограничения и ложные гипотезы:
[отчёт Astra](project-review-astra-2026-10-14.ru.md).

## Проверка всех находок основным агентом

| Наш ID / Astra | Приоритет | Проверка и результат |
|---|---|---|
| R1 / A2 | P2 | Подтверждено: per-turn asyncio.Queue не имел count/byte bound; thoughts/tool payloads обходили поздний response-length guard. Исправлено TurnBuffer и failure/quarantine, включая позднее разрешение. |
| R2 / A1 | P1 | Подтверждено синтетическим canary без сети: CWD config направлял установленный secret_env на свой endpoint. Исправлено platform-only default discovery. |
| R3 / A3 | P2 | Подтверждено source и нативными Windows тестами: общие export/read не проверяли ACL, хотя service-install проверял. Исправлен общий путь до secret bytes. |

Отклонённых находок среди этих трёх нет. Дополнительные гипотезы без
воспроизведения не переименованы в доказанные уязвимости; они перечислены в
отчёте Astra. Принятые ограничения Hermes cold resume/cancel/shared-control,
optional CLI и тестового IP/port transport не объявлялись дефектами.

### R1: bounded turn events и разрешения после overflow

`agents/turn_buffer.py`: максимум 256 ожидающих событий и оценка 4 MiB Python
payloads, включая nested raw_input. Обход размера без JSON/string copies,
с ограничением объёма и защитой от повторных ссылок. Чтение освобождает бюджет.
Для terminal signal зарезервирован отдельный slot; completion на capacity не
зависает. Overflow очищает backlog, ставит единственный error sentinel и
закрывает дальнейшее enqueue, а не отдаёт молча урезанный успешный ответ.

`AgentClient` завершает turn с PromptFailed, закрывает повреждённый transport,
сохраняет quarantine и не повторяет prompt. Разрешения проверяют quarantine
как до вызова handler, так и после await: pending human decision не может
вернуть allow между overflow и закрытием транспорта consumer-ом.

Закрывается общий transport этого AgentClient: соседние turns на нём могут
завершиться ошибкой. Это намеренное fail-closed поведение, не обещание изоляции
подключения на уровне session. Astra проверил через настоящий ACP SDK/
MemoryTransport два prompts: PromptFailed/TransportDisconnected, ровно два
отправленных prompts и одно подключение, без replay/reconnect. Соседний Hermes
restore=disabled остался uncertain. Другие AgentClient/компьютеры не используют
этот же transport.

Регрессии: `test_turn_buffer.py`, дополнения `test_agent_client_safety.py`;
FIFO/completion at capacity, event/byte overflow, thought/tool payload, release
budget, already-finished/still-running upstream, no replay, новые и pending
permission requests после quarantine.

### R2: явное доверие checkout config/env

`find_config_file()` и `find_env_file()` больше не ищут файлы в CWD.
По умолчанию используются только platform config dir; `--config`, `--env-file`,
ACPGW_CONFIG/ACPGW_ENV_FILE сохраняются. Запуск CLI в чужом checkout больше не
подменяет endpoint установленного профиля и не читает его локальный .env.

Это сознательное изменение совместимости: для checkout-файлов нужно явно
указать обе пары путей или переменные окружения. README и setup guides обновлены.
Installed setup/service configs с сохранёнными/явными путями не мигрируются.
При явном выборе чужого конфига граница доверия остаётся за владельцем.

Регрессии `test_config.py` и `test_cli.py`: installed config/env против CWD,
нет installed config при существующем installed env, cwd-only files, explicit
paths/environment overrides, secret redaction и CLI default. Три прежних CLI
теста переведены на explicit paths; старое ожидание CWD больше не является
корректным контрактом. Никаких настоящих секретов/соединений для canary не было.

### R3: Windows ACL до чтения/экспорта bearer

`ComputerRegistry.issue()` проверяет приватный Windows parent до создания
файла и пустой leaf до записи token. При отказе не меняется действующий credential/
generation, rollback удаляет только созданный им файл. Права чужого каталога
не исправляются автоматически. POSIX owner/mode/O_EXCL guards сохранены.

`read_credential()` на Windows проверяет file/parent ACL и reparse до open/decode;
это применяется foreground/setup/CLI путям, не только Task Scheduler install.
Переиспользован существующий проверенный `windows_acl.check_private_paths`.

POSIX doubles проверяют порядок, пустой файл до записи и rollback. Native
Windows использовал только disposable temporary directories и synthetic
in-memory registry: private export/read проходят, public-file read отказывает,
public-parent rotation отказывает до экспорта и сохраняет старый credential,
junction parent отвергается при чтении и выдаче. Elevation/production tasks
не требовались. Первый fixture Set-Acl для directory потребовал SeSecurityPrivilege;
заменён на FileInfo/DirectoryInfo.SetAccessControl. Product ACL policy не ослаблялась.

## Проверки

Evidence: ignored `dist/project-review-2026-10-14/`, JUnit XML — источник counts;
RTK err скрывает успешный stdout, поэтому числа не выводились из пустого лога.

- Baseline до исправлений: **1309 passed / 31 expected skips**, 202.991s.
- Первый R1 focused suite: **48 passed**, 8.435s.
- Config/buffer/daemon focused: **73 passed**, 35.495s.
- Credential/ACL focused Linux: **54 passed**, 2.177s.
- Installed preview wheel native Windows: **134 passed**, 46.17s; отдельно
  CLI compatibility: **8 passed**, 4.08s. `windows-final.xml`, `windows-cli.xml`.
- Финальный source suite: **1325 passed / 35 expected skips**, 198.096s;
  `source-final.xml/.log`. Четыре новых skips — native Windows ACL tests на Linux.
- Ruff/check/format и git diff --check прошли перед финальным suite.
- Повторный независимый просмотр Astra не нашёл новых блокирующих замечаний;
  проверены pending permission race, adjacent-turn failure, config opt-in и
  ACL preflight ordering. Его focused suites прошли, count не выдумывается.

## Поставка и оставшаяся приёмка

Исправления имеют development version **0.2.0rc8.dev0**. Wheel собран и установлен
только в отдельном Windows validation environment; production tool не заменялся.
Новый GitHub/PyPI release не создавался. Опубликованный rc7 не содержит этих
изменений и не был переписан. Будущий release и обновление владельцем — отдельный
этап, вместе с прежними P4.8.5 live UI/reboot/logon/network checks.

По трём найденным кодовым дефектам открытых исправлений не осталось; финальный
source suite и native проверки подтвердили исправления. Физическая приёмка существующей deployment
остаётся PARTIAL; завершение этого обзора не закрывает P2.3/P4.8.5.
