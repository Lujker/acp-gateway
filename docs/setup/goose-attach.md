# Inspect an existing Goose ACP endpoint

Scope update 2026-10-20: P4.9 targets Goose as an alternative to Hermes across
CLI/Desktop and platform combinations. This is future scope, not verified parity;
see [requirements](../items/goose-alternative.ru.md). It does not block the nearer
P4.17/P4.15.1 work. This guide describes the existing limited probe only.

The development CLI can inspect an explicitly supplied local Goose endpoint:

```bash
acpgw agents probe goose --url http://127.0.0.1:3000 --secret-env GOOSE_SERVER__SECRET_KEY --json
```

The named variable must already contain the **backend access key**, not the model
provider's API key. The key is sent as `X-Secret-Key`; its value is never an argv
argument or report field. An empty selected variable is an error. If the server
does not require a key, omit `--secret-env`.

The CLI accepts numeric loopback addresses only, without URL credentials, query
parameters or fragments. `http(s)` becomes `ws(s)` and an empty path becomes
`/acp`. Use the matching Windows/WSL/Linux/macOS environment, next to the existing
backend. No port scan, process/memory inspection or provider/keyring extraction
is performed.

TLS requires the expected SHA-256 certificate fingerprint supplied separately:

```bash
acpgw agents probe goose --url https://127.0.0.1:3000 --secret-env GOOSE_SERVER__SECRET_KEY --tls-fingerprint EXPECTED_SHA256 --json
```

Replace the placeholder with the fingerprint of your existing backend. No TOFU
pin is saved. The certificate is verified before sending credentials; redirects
and environment proxies are not followed. Without a matching pin, TLS probing
stops with `auth-required`.

The only outgoing ACP message is `initialize`, with filesystem and terminal
capabilities disabled. Unsolicited requests, including permission questions,
are ignored without sending a decision/error. The connection is then closed.
There is no session creation, load, prompt, mode change or cancel. Reports include
agent/version/protocol and **advertised** load capability; they do not prove safe
session adoption, concurrent Desktop control or working model credentials.
Probe traffic is bounded to 64 KiB per frame, at most 64 accepted messages and
the configured timeout. Diagnostics redact backend echoes of the supplied key.

The statuses are `ready`, `auth-required`, `incompatible`, `offline`, and
`manual-endpoint-required`. The last means no URL was given; it does not claim
that Goose is absent. `ready` requires protocol 1 and a Goose agent identity.

## Discovery limitations

The existing network Goose adapter can attach to a supplied ACP URL and key.
In interactive setup, select Goose and decline preparing a managed local
listener to configure an existing endpoint. Check it with this command first;
do not infer an endpoint from a default port or treat an installed CLI as a
running ACP service. After a dynamic endpoint/key changes, manual configuration
must be refreshed until an actual rediscovery recipe is supported.

The upstream Desktop [architecture](https://github.com/aaif-goose/goose/blob/main/ui/desktop/README.md)
uses a bundled ACP server. The inspected [launcher](https://github.com/aaif-goose/goose/blob/main/ui/desktop/src/gooseServe.ts)
allocates a port and builds authenticated URLs; the inspected
[lease registry](https://github.com/aaif-goose/goose/blob/main/ui/desktop/src/gooseServeLeaseRegistry.ts)
keeps URL/key state in application memory. These observations concern upstream
`main` inspected on 2026-10-13, not every installed Goose release. They do not
establish a Hermes-style rendezvous file contract. Automatic Desktop discovery
needs a verified export/attach interface for the installed version; it is still
open in P4.9. The probe does not scrape redacted logs or modify Desktop to obtain
its key.

Tests cover the recorded Goose 1.53.0 initialization contract through a real
ACP SDK mock, TLS/authentication refusal, disabled callbacks, bounded replies and
credential redaction. No live Goose Desktop was available in the current Linux
environment; live/native acceptance remains separate.
