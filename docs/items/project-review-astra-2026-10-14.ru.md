# Независимое ревью GPT-6 Astra — 2026-10-14

**Статус:** обзор завершён; три находки переданы основному агенту.
База: `95322da`, `feat/vps-acceptance`, rc7. Номера строк относятся к этой базе:
основной агент параллельно исправляет код. Актуальные результаты исправлений —
[общий журнал](project-review-2026-10-14.ru.md).

Ревью охватывало проект, а не только последний diff. Production VPS, реальные
Telegram/Hermes-разговоры, ключи и службы не использовались. Ревьюер не менял
runtime-код, тесты, конфигурацию, Git-историю или релизы.

## Подтверждённые находки

### A1 — P1: автоматический конфиг из CWD получает секреты установленного профиля

**Источник:** `src/acp_gateway/config.py:400–430`, особенно 405, 413, 418–419;
потребитель — `AgentClient._auth_headers()`.

`find_config_file()` предпочитает `./config.yaml`, а независимый
`find_env_file()` при отсутствии `./.env` выбирает установленный `.env`.
Запуск установленного CLI из чужого checkout автоматически соединяет
контролируемый checkout endpoint/имя `secret_env` с секретами владельца.
Конфиг может назначить переменную установленного .env учётными данными нового
агента. При подключении она попадёт в заголовок запроса. TLS проблему не
устраняет: конфиг может указать чужой endpoint и fingerprint либо TOFU.

Предусловие: владелец запускает команду, соединяющуюся с агентом, из каталога
с недоверенным config.yaml без явного выбора конфигурации. Одно чтение YAML
само по себе ничего в сеть не отправляет. Явное сознательное подключение
чужого конфига с секретами — другая граница доверия.

**Воспроизведено без сети:** временный installed/.env содержал синтетический
REVIEW_CANARY; project/config.yaml задавал wss://attacker.invalid/acp и
secret_env: REVIEW_CANARY, но project/.env отсутствовал. После chdir(project),
подмены paths.config_dir() на временный installed и load_config():
`cwd_config=config.yaml installed_env=True outbound_endpoint=wss://attacker.invalid/acp synthetic_secret_referenced=True`.
Проверен сформированный auth header, реальные секреты не читались.

**Исправление:** platform config/env по умолчанию; project paths только через
явные --config/--env-file или environment overrides. Если CWD необходим для
совместимости, минимум — выбор config/env как одной доверенной пары без fallback
к секретам другого каталога; доверие CWD всё равно нужно определить явно.

**Регрессии:** оба направления смешения; CWD без .env и только с .env; явные
пути; environment overrides; service/update из другого рабочего каталога.

### A2 — P2: очередь событий ACP-turn не ограничена

**Источник:** `src/acp_gateway/agents/client.py:695–704,794–796`;
обработка MessageChunk в GatewayCore._run().

На turn создаётся asyncio.Queue() без maxsize, callback безусловно вызывает
put_nowait(normalize(...)). Ограничения relay/WS действуют до этой очереди.
max_response_length проверяется позднее и ограничивает только ответ, а не
thought/tool/other events и backlog. Быстрый агент или медленный consumer может
исчерпать память диспетчера, прервав и другие разговоры.

**Воспроизведено:** fake conn.prompt() синхронно вызвал настоящий _on_update()
10 000 раз для agent_thought_chunk с текстом 2 KiB до возврата результата.
Использованы настоящие AgentClient.prompt()/normalize, без сети:
`queued_events=10000 queue_maxsize=0; consumed_events=10001`.

**Исправление:** count/byte bound, включая nested raw_input, отдельная ёмкость
terminal signal; overflow — явная ошибка и карантин неизвестного исхода,
без молчаливой потери answer chunks и без prompt replay.

**Регрессии:** completion ровно на capacity; thought/tool burst; oversized
event; освобождение бюджета; upstream уже завершился/ещё работает; late result;
отсутствие replay; новые и уже ожидающие permissions после карантина.

Просмотрен первоначальный diff основного агента с TurnBuffer: reserved terminal
slot и sealing сохраняли завершение. Передана дополнительная рекомендация:
уже ожидающий permission handler должен повторно проверить карантин после
await, иначе allow возможен между синхронным overflow и закрытием транспорта
consumer-ом. Закрытие общего direct transport прерывает и соседние turns;
это следует считать выбранным fail-closed поведением и проверить.

### A3 — P2: общие credential-функции не проверяют Windows ACL

**Источник:** `src/acp_gateway/storage/computers.py:129–137`;
`src/acp_gateway/connectors/control.py:278–290`; вызовы через
cli/computers.py, ConnectorLaunch.from_args() и connectors/commands.py.

ComputerRegistry.issue() создаёт bearer-файл через os.open(..., 0o600);
приватные права явно устанавливаются только на POSIX. Windows mode bits
не создают приватную DACL: действует наследование ACL родителя. CLI при этом
сообщает об экспорте в приватный файл. read_credential() проверяет owner/mode
только на POSIX; Windows ACL/reparse preflight в общей функции отсутствует.

**Сценарий:** Windows enroll/rotate --token-file в каталоге с читающей ACL
других пользователей экспортирует доступный им bearer; foreground connector
принимает такой файл. Другой пользователь может представиться компьютером.
Отдельная проверка Task Scheduler install защищает service path, а не эти
пути CLI.

**Доказательство:** проверен source и полный путь вызовов. Нативное
воспроизведение с выдачей публичной ACL ревьюер не выполнял; native pass
здесь не заявляется.

**Исправление:** shared Windows preflight до чтения/записи secret bytes;
приватная DACL или отказ неподходящему родителю; reparse/link refusal.
При ошибке сохраняется rollback registry и удаление только собственного файла.

**Регрессии:** публичный файл/родитель; reparse leaf; ACL failure до записи;
private success; failed rotation не меняет рабочий credential. Нужен отдельный
disposable native test Windows.

## Покрытие и ограничения

| Область | Проверенные ветви |
|---|---|
| Agents | connect/reconnect, mode/load binding, prompt/cancel, permissions, normalization, transport/TLS |
| Hermes | dedicated ownership, detach/quarantine, model confirmation/recovery, manager journal/retire |
| Core | admission/maintenance, selection revisions, busy/creation accounting, job cleanup, approvals, execution auto |
| API/channels | owner/MCP isolation, Host/body checks, owned sessions/jobs, SSE recovery, Telegram private chats/callbacks/delivery |
| Connectors | grant/revoke, generation/epoch fencing, replacement, relay bounds, local policy, catalog, maintenance IPC |
| Storage | migrations, ownership, retention, model intents/observations, delivery unknown outcomes, credential issuance |
| Install/service | leases, copied workers, snapshot/rollback, service restore, systemd definitions, Task Scheduler/ACL |
| Delivery | checkout/release installers, release builder, Docker/proxy boundaries, release workflow |

Это широкий source review критических ветвей всех runtime-подсистем, а не
утверждение о чтении каждой строки всех CLI/UI/smoke-script файлов или формальное
доказательство безопасности. Установка/удаление, Docker build, CI-публикация,
реальная сеть и native Windows acceptance в этой сессии не запускались.

Прочитаны ROUTING/RTK; CodeGraph/Graphify отсутствовали. RTK использовался для
discovery/tests; raw exact source потребовался для полного control flow и строк.

## Проверки и исключённые ложные находки

- Выполнены два изолированных воспроизведения A1/A2.
- `rtk err .venv/bin/pytest -q tests/unit/test_computers.py tests/unit/test_connector_policy.py tests/unit/test_model_state.py tests/unit/test_maintenance_worker.py` завершился с кодом 0. RTK скрыл обычный summary; число тестов не выдумывается.
- Baseline/full/native проверки основного агента не выдаются за собственные.
- /mcp использует вложенный OwnerAccess с отдельным token; owned session/job checks присутствуют.
- Revoke/replacement имеет generation/epoch fencing; old handler не снимает новое подключение. Неопределённая outbound delivery не повторяется автоматически.
- Принятые ограничения Hermes cold resume/cancel/shared Desktop control, IP/port без TLS и optional CLI не объявлялись дефектами.
- 32 Hermes handles и 4096 relay stream IDs рассмотрены как явные пределы; стратегия долгосрочной очистки/смены epoch требует отдельной нагрузки.
- Гипотезы TLS-chain pinning/TOFU races, отказов файловой системы и потери питания не получили воспроизведения и не включены в подтверждённые дефекты.

## Передача

Все находки и воспроизведения переданы основному агенту до завершения обзора.
Этот отчёт не присваивает исправлениям READY без его итоговых тестов.
Требуются повторная проверка изменённых границ и native Windows evidence A3.
Финальный статус и остатки фиксируются в общем журнале ревью.



## Повторное ревью исправлений — 2026-10-14

По запросу основного агента проверены текущие изменения runtime, тестов и
документации по R1/A2, R2/A1, R3/A3. Новых блокирующих замечаний не найдено.
Это source/diff verdict; итоговый native/full-suite verdict остаётся за
основным агентом.

- **R1/A2:** TurnBuffer ограничивает очередь по count/bytes, сохраняет отдельный
  terminal slot при полной ёмкости, sealing не позволяет late completion
  замаскировать overflow. Карантин выставляется синхронно в update callback.
  Добавленная повторная проверка _uncertain после await permission handler
  закрывает ранее переданную гонку уже ожидающего allow.
- Дополнительно выполнен независимый эксперимент с настоящим ACP SDK
  AgentSideConnection/AgentClient и парой MemoryTransport, без сокетов:
  два prompts одновременно ожидают результат, один получает overflow.
  Исходы строго PromptFailed и TransportDisconnected; отправлено ровно
  два prompts, создано ровно одно подключение, replay/reconnect не было.
  Generic-сосед получил restore-required; сосед с restore=disabled остался
  uncertain. Таким образом, проверенный соседний turn не объявлен completed.
  Закрытие соседних turns остаётся намеренным fail-closed эффектом общего
  direct transport, а не изоляцией на уровне одного session.
- **R2/A1:** CWD больше не выбирает config/env автоматически; explicit flags
  и environment overrides сохраняются. Регрессии учитывают отсутствие
  platform config при существующем platform .env и чужие CWD-файлы.
  README и setup docs объясняют явный выбор checkout-файлов. Поиск не выявил
  оставленного утверждения о приоритете CWD в setup docs.
- **R3/A3:** приватность Windows parent проверяется до создания файла,
  пустой созданный файл — до записи secret bytes. Ошибка leaf ACL остаётся
  внутри rollback/удаления собственного файла. Read проверяет Windows
  file/parent до open/decode. Отдельные native cases не заменены POSIX doubles.
  Fixture-only замечание передано: Set-Acl может требовать SeSecurityPrivilege;
  если native fixture упадёт по этой причине, использовать FileInfo/
  DirectoryInfo.SetAccessControl, не ослабляя product preflight.

Повторно выполнено:
`rtk err .venv/bin/pytest -q tests/unit/test_turn_buffer.py tests/unit/test_agent_client_safety.py tests/unit/test_config.py tests/unit/test_credential_windows_boundary.py tests/integration/test_agent_transport_factory.py`.
Код завершения 0; RTK не показал count. Native Windows тесты в этом повторном
ревью не запускались. Проверка ACL не заявляет защиту от параллельной
злонамеренной замены дерева самим доверенным владельцем или администратором.

