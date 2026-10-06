# Work Goose: reaching `goose serve` from the LAN

> Runbook for roadmap item `P0.1` ([`road-map.md`](../../road-map.md)).
> Verified on 2026-10-06: goose 1.53.0 in WSL on the work laptop, ACP Gateway
> on a home PC in the same LAN (`scripts/spike_acp.py`; details in
> [`road-notes.md`](../../road-notes.md)).

## Target state

- `goose serve` runs in WSL on the work laptop, listening on port `3284` with
  TLS and a shared secret;
- the port is reachable from other machines in the LAN;
- Goose Desktop and ACP Gateway connect to `https://<laptop-address>:3284`
  with the same secret and certificate fingerprint.

## 1. `goose serve` in WSL

Manual start, to check that it works:

```bash
GOOSE_SERVER__SECRET_KEY='<long random secret>' \
goose serve --host 0.0.0.0 --port 3284 --tls
```

On startup goose prints `GOOSED_CERT_FINGERPRINT=AA:BB:...` — the SHA-256 of
its certificate. It changes only when the certificate is regenerated.

Permanent setup: on the reference machine `goose serve` runs as a service
inside WSL.
<!-- TODO: add the unit name and path, where the service reads the secret from,
     and how to read its logs (`journalctl --user -u ...`). -->

## 2. Opening the port to the LAN (Windows on the work laptop)

Inbound Hyper-V firewall rule for WSL (PowerShell as administrator):

```powershell
New-NetFirewallHyperVRule -Name GooseServe -DisplayName "goose serve (WSL)" `
  -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol TCP -LocalPorts 3284
```

`{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}` is the VM creator id of WSL. The
Hyper-V firewall governs inbound WSL traffic in mirrored networking mode
(`.wslconfig`: `[wsl2] networkingMode=mirrored`).
<!-- TODO: confirm that the reference machine uses mirrored mode. -->

Check from another machine in the LAN (the certificate is self-signed; no
secret needed):

```bash
openssl s_client -connect <laptop-address>:3284 </dev/null 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256
```

The fingerprint must match `GOOSED_CERT_FINGERPRINT`.

## 3. Approval mode

Sessions created over ACP on this goose start in `auto` mode (no approvals).
The gateway switches its own sessions to `smart_approve` with
`session/set_mode` (owner decision, 2026-10-06); goose needs no extra
configuration for that.

## 4. Connecting the gateway

`config.yaml` on the gateway host:

```yaml
agents:
  - alias: work
    title: Work Goose
    kind: goose
    url: https://<laptop-address>:3284   # /acp is appended automatically
    secret_env: AGENT_WORK_SECRET
    tls_fingerprint: "<GOOSED_CERT_FINGERPRINT>"
    default_cwd: /home/<user>
```

`.env`: `AGENT_WORK_SECRET=<the same secret>`, mode `600`. Then check:
`uv run acpgw config check` and `uv run python scripts/spike_acp.py init`.

## Troubleshooting

- `TLS fingerprint mismatch` — goose regenerated its certificate: compare the
  new `GOOSED_CERT_FINGERPRINT` and update the config (and Goose Desktop).
- Connection timeout — the goose service is down, the port is not open in the
  Hyper-V firewall, or the laptop's address changed (a DHCP reservation
  helps).
- The connection closes right after the handshake — wrong secret.
