# Work Goose: доступ к `goose serve` из LAN

> Runbook к пункту `P0.1` ([`road-map.md`](../../road-map.md)). Проверено
> 2026-10-06: goose 1.53.0 в WSL рабочего ноутбука, Gateway на домашнем ПК в
> той же LAN (`scripts/spike_acp.py`, записи в [`road-notes.md`](../../road-notes.md)).

## Что должно получиться

- `goose serve` в WSL рабочего ноутбука слушает порт `3284` с TLS и общим
  секретом;
- порт доступен с других машин LAN;
- Goose Desktop и ACP Gateway подключаются к `https://<адрес-ноутбука>:3284`
  с тем же секретом и отпечатком сертификата.

## 1. `goose serve` в WSL

Запуск (вручную, для проверки):

```bash
GOOSE_SERVER__SECRET_KEY='<длинный случайный секрет>' \
goose serve --host 0.0.0.0 --port 3284 --tls
```

При старте goose печатает строку `GOOSED_CERT_FINGERPRINT=AA:BB:...` — это
SHA-256 сертификата; он меняется, только если сертификат перевыпущен.

Постоянный запуск: владелец оформил `goose serve` как сервис внутри WSL.
<!-- TODO: вписать имя unit-файла, путь к нему, где хранится секрет и как
     смотреть логи (`journalctl --user -u ...`), — детали у владельца. -->

## 2. Доступ к порту из LAN (Windows на рабочем ноутбуке)

Входящее правило Hyper-V firewall для WSL (PowerShell от администратора):

```powershell
New-NetFirewallHyperVRule -Name GooseServe -DisplayName "goose serve (WSL)" `
  -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol TCP -LocalPorts 3284
```

`{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}` — идентификатор WSL как создателя ВМ.
Hyper-V firewall управляет входящим трафиком WSL в режиме mirrored
networking (`.wslconfig`: `[wsl2] networkingMode=mirrored`).
<!-- TODO: подтвердить у владельца, что включён именно mirrored-режим. -->

Проверка с другой машины LAN (сертификат самоподписанный, секрет не нужен):

```bash
openssl s_client -connect <адрес-ноутбука>:3284 </dev/null 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256
```

Отпечаток должен совпасть с `GOOSED_CERT_FINGERPRINT`.

## 3. Режим подтверждений

Сессии, созданные через ACP, у этого goose стартуют в режиме `auto` (без
подтверждений). Gateway сам переводит свои сессии в `smart_approve` (решение
владельца 2026-10-06) через `session/set_mode`; настраивать goose для этого не
нужно.

## 4. Подключение Gateway

`config.yaml` на хосте Gateway:

```yaml
agents:
  - alias: work
    title: Work Goose
    kind: goose
    url: https://<адрес-ноутбука>:3284   # /acp дописывается автоматически
    secret_env: AGENT_WORK_SECRET
    tls_fingerprint: "<GOOSED_CERT_FINGERPRINT>"
    default_cwd: /home/<user>
```

`.env`: `AGENT_WORK_SECRET=<тот же секрет>`, права `600`. Проверка:
`uv run acpgw config check`, затем
`uv run python scripts/spike_acp.py init`.

## Если не подключается

- `TLS fingerprint mismatch` — goose перевыпустил сертификат: сверить новый
  `GOOSED_CERT_FINGERPRINT` и обновить конфиг (и Goose Desktop).
- Таймаут соединения — сервис goose не запущен, порт не открыт в Hyper-V
  firewall или у ноутбука сменился адрес (стоит закрепить его DHCP-резервацией).
- Соединение закрывается сразу после рукопожатия — неверный секрет.
