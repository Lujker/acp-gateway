# VPS dispatcher and connector development

The first target is one owner, Linux/WSL computers and existing network ACP
agents. The same package will provide a VPS dispatcher and an outgoing
`acpgw connector` mode. Computers need no inbound public port. The current
owner API remains loopback-only; connector ingress is a separate authenticated
endpoint. The VPS is trusted with prompts, answers and approval data; agent
tool credentials and work files stay on computers.

| Stage | Deliverable |
|---|---|
| 1 | Implemented: versioned control frames, stable computer IDs, local enrollment, credential rotation/revocation |
| 2 | Implemented and mock-tested: ingress in serve, guarded ACP relay, requests/results/human approvals/cancel |
| 3 | Namespaced configured routes implemented; multiple-computer/channel acceptance remains |
| 4 | Native keepalive, reconnect, epoch fencing and bounded queues implemented; durable result recovery remains |
| 5 | VPS deployment, connector services/upgrades and local stdio bridging |

Existing core, policy, approval audit, sessions, Telegram/MCP, ACP client,
binary build and Linux user services are reused. Native Windows and macOS
are separate platform work; they do not block the Linux/WSL route.
The first network ACP relay is implemented and tested against the recorded
Goose mock. Real Goose end-to-end acceptance and deployment remain outstanding.

Enrollment is an owner operation on the VPS. Each computer has its own
random credential, independent of owner/MCP/Telegram/agent tokens. The
database stores only a credential digest. A token is exported into a new
private file, never printed or placed in a URL. Rotation replaces the old
credential; revocation disables the identity. Credentials are transmitted
only in the WebSocket handshake authorization header. The owner selected plain
WS for current IP/port testing, with TLS disabled; that link is unencrypted.
When explicitly using WSS, validate TLS before sending credentials. Enrollment is local in the first stage; remote pairing
and multi-user delegation are not exposed yet.

Control frames carry a protocol version, type, stable computer ID and agent
manifest. The server assigns each accepted connection a new epoch UUID.
Reject unknown versions/types, duplicate agent aliases, oversized frames,
unexpected fields and invalid identifiers; protocol errors never echo input.
The manifest does not advertise URLs or credentials: local profiles determine
which agents the connector may access. The relay must check that the computer
ID in the hello matches the authenticated credential before exposing agents.

Recovery rules for the first working route: an offline target rejects new
work immediately; there is no implicit offline queue. Disconnecting a prompt
does not permit automatic replay because actions may already have executed.
Connection recovery and session loading are distinct operations. Late approval
decisions must be rejected, and the connector never supplies an automatic allow.
Durable result recovery and replay rules will be implemented explicitly in
stage 4. A computer's disconnection must not affect other computers.

The planning estimate is 8–12 implementation iterations: roughly 1–2 developer
weeks for a restricted first route, 3–5 for the broader requirements. These
are preliminary estimates, not release dates; recovery and stdio interoperability
are the main uncertainties. Releases and binary builds remain manual.

## Stage 1 usage

Run these commands locally on the future dispatcher host. They use the selected
configuration's `data_dir` and work without a running gateway API:

```bash
acpgw --config /path/to/config.yaml computers enroll work-laptop \
  --name "Work laptop" --token-file /private/path/work-laptop.key
acpgw --config /path/to/config.yaml computers list
acpgw --config /path/to/config.yaml computers rotate work-laptop \
  --token-file /private/path/work-laptop-next.key
acpgw --config /path/to/config.yaml computers revoke work-laptop
```

Use `uv run acpgw` for a source checkout. Credential file parents must already
exist; choose a directory controlled by the owner. Files are created exclusively
with POSIX mode `0600`; existing files and symlinks are refused. Credentials
contain 256 bits of randomness. Only a domain-separated SHA-256 digest is stored
in SQLite schema 4. Listing returns metadata and generation, never a digest or
credential. Rotation invalidates the previous credential immediately for future
authentication. Revocation is idempotent and terminal for that computer ID;
rotating or enrolling a revoked ID is refused. Use a new ID for a replacement.
Issued files stay on disk until the owner removes them. Normal write/database
failures roll back issuance and remove the new file; process termination or a
power failure can interrupt the file/database handoff; inspect the registry and
rotate to a new file if the exported credential is missing or unusable.

Control protocol v1 implements `hello`, `welcome`, `ping`, `pong` and `error`.
Frames are bounded to 64 KiB of UTF-8 JSON; hello contains 1–100 uniquely named
agents. Unknown fields, duplicate JSON keys, invalid identifiers and unsupported
versions are rejected with input-independent errors. Connection IDs are UUIDs.
WS/WSS transport, authorization and heartbeat scheduling are implemented below.
Relay frames and usage are described below.

## Relay preparation: transport injection

`AgentClient` accepts an optional asynchronous `transport_factory`. Each call
must open a fresh ACP stream implementing `AgentTransport`: `send`, `receive`,
idempotent `close`, and a `closed` event. EOF returns `None`; delivery failures
raise `ConnectionError`. Factories own routing, authentication, TLS policy,
connection timeouts and cleanup of partially opened resources; normalize open
failures to the existing agent error types for retry/fatal-error handling.
Once a stream is returned, the client owns its lifecycle, including closing it
on failed or cancelled initialization. Closing a relay stream does not
close the shared computer connection.

Without a factory the existing direct WebSocket connection, authentication and
certificate pinning are used. With a factory the client does not probe the local
agent endpoint or transmit its configured secret; `tls_pin` is `None`, and
connection logs mark TLS as transport-managed. ACP initialization, session modes,
load/replay suppression, updates, permissions, cancellation and reconnection
remain in the same client. Integration tests exercise these operations against
the recorded mock agent over an in-memory ACP stream, including a disconnected
turn that fails without replay.

`RelayTransport` now implements this seam over the computer connection.
The computer's local ACP socket is wrapped in the policy below. The owner
accepted the architecture below on 2026-10-08.

## Accepted relay architecture

- Connector ingress is a second listener in `serve`, sharing the runtime
  with core/channels/approvals. The standalone `dispatcher` listener remains
  a diagnostic mode; use `serve` for task routing.
- Relay carries ACP JSON-RPC in a multiplexed envelope with agent alias, stream
  ID and connection epoch. The VPS reuses AgentClient with RelayTransport; the
  computer applies PolicyTransport before forwarding traffic to its local agent.
- The newest fully authenticated registration replaces the old connection and
  receives a fresh epoch. Old stream results and approvals must be rejected.
  Registration replacement and relay epoch fencing are implemented.
- One reader demultiplexes traffic, with WebSocket keepalive, separate control/data
  limits and byte-bounded stream queues. Overflow fails the affected stream.
- Core addresses remote agents as `computer/agent`, distinguishing computer
  connectivity from agent readiness. Network failures retry with backoff;
  access/TLS failures require correction, and uncertain prompts are not replayed.
- In-process access revocation fences streams immediately; commands from another
  process are detected within one second. Ingress limits concurrent connections
  and handshakes to 64 and opening handshakes to five seconds. Deployment-specific
  rate limits remain nginx/VPS acceptance work.
- TLS is disabled for current IP/port tests. Domains, root paths and subpaths
  remain supported for a later nginx front end. TLS can be enabled explicitly on
  the listener or terminated at nginx; plaintext upstream stays on loopback in
  that deployment. The direct gateway-to-agent transport keeps its own policy.

## Local ACP policy for the first relay route

`LocalAgentPolicy.from_profile(profile)` selects the local profile's
`default_cwd` and `session_mode`. `PolicyTransport(local_transport, policy)`
guards one computer-side ACP stream before it is exposed to the dispatcher.
It implements the same transport interface as the direct WebSocket transport.
The `connector` CLI applies this boundary on every local relay stream.
The existing direct gateway mode keeps its original behavior.

The request allowlist is `initialize`, `session/new`, `session/load`,
`session/list`, `session/prompt`, `session/set_mode` and `session/cancel`.
Requests are rebuilt from supported wire fields; extension metadata is dropped.
Initialization always advertises disabled file/terminal capabilities and no
client extensions. New/load requests always send empty `mcpServers` and
`additionalDirectories`, even if the dispatcher supplies executable MCP servers
or other workspace roots. The first route accepts text-only prompts with a
local total character limit; file/resource/image/audio inputs are refused.

Working directories are exact, locally selected absolute POSIX paths. The
default allowlist contains only `default_cwd`; the policy constructor can take
additional exact directories when a future local configuration exposes them.
Child directories, traversal and unrelated directories are refused. Session
listing also uses an allowed directory. The connector does not resolve paths
on its own filesystem: a network ACP agent may run on a different machine.
This check constrains the requested cwd, not filesystem access by the agent's
tools; it does not resolve remote symlinks or sandbox an existing session.
Subtree rules need an agent-side path/sandbox contract before being enabled.

For a configured session mode, prompts are blocked until a new/load response
reports that mode or the matching set-mode request succeeds. Changing to a
different mode is refused. Failed reloads and mode changes invalidate readiness;
an unsolicited mode update away from the configured mode blocks later prompts.
One session cannot have parallel prompts on the same stream.

Only permission requests associated with an active prompt are forwarded.
`allow_always` and `reject_always` options are removed; duplicated option IDs are
refused, including collisions with hidden always options. A decision must match
a live request and an advertised once-only option. Cancel and prompt completion
invalidate its options; a later cancelled outcome remains accepted, while a late
selection or duplicate response is refused. Other client requests, including
filesystem and terminal requests, are answered locally with method-not-found.
The connector never manufactures an allow decision. The dispatcher remains
trusted to supply the human's decision; this policy cannot authenticate a human
independently of the dispatcher.

Per-stream tracking is bounded to 64 outstanding ACP requests, 64 permissions
and 1024 attached sessions. A violation closes that stream, clears tracking and
fails outstanding work without replay; other streams remain independent.
The relay layer adds wire frame limits, stream/epoch demultiplexing and reconnect
scheduling independently of this policy wrapper.

## Stage 2: WS/WSS connection and ACP relay

`serve` owns the computer listener alongside the owner API and channels.
On the VPS configure the listener and explicit remote routes. A route names
the computer and its local agent alias; no agent URL or agent secret belongs
in the VPS profile. The cwd and requested mode must match the computer's policy.

```yaml
# dispatcher.yaml (VPS)
connector:
  enabled: true
  host: 0.0.0.0
  port: 8766
  connect_path: /
agents:
  - alias: goose
    computer_id: work-laptop
    backend: connector
    kind: goose
    default_cwd: /work
```

Set the owner API token in the VPS environment or private `.env`, enroll the
computer, then start the gateway:

```bash
acpgw --config /path/to/dispatcher.yaml serve
```

The owner API and MCP remain loopback-only. `connector.enabled` defaults to
false, `connector.host` to `127.0.0.1`, and the path to `/connect`. TLS is off
unless both `connector.tls_cert` and `connector.tls_key` are provided. Existing
direct agent profiles can coexist with connector routes. Core/CLI/Telegram/API
address a route as `work-laptop/goose`; MCP names its tools
`work-laptop__goose_ask`, `work-laptop__goose_sessions`, etc. Conflicting tool
names are refused at configuration time. `/health` distinguishes enrolled
computer connectivity from successful ACP initialization, and
`/health/agents/work-laptop/goose` reports the agent's readiness.

After the computer connects, submit a test prompt through the existing owner CLI:

```bash
acpgw --config /path/to/dispatcher.yaml ask --agent work-laptop/goose "say pong"
```

Keep `acpgw ... approvals watch` connected for actions requiring human permission,
or use the configured Telegram approver. No available approver means deny.

For registration diagnostics alone, a standalone listener remains available:

Enroll the computer with the stage 1 commands. For current IP/port tests:

```bash
acpgw --config /path/to/dispatcher.yaml dispatcher \
  --host 0.0.0.0 --port 8766 --connect-path /
```

Default binding is `127.0.0.1`; a public bind requires the explicit `--host`.
TLS is off unless both `--tls-cert` and `--tls-key` are supplied. The listener
accepts exactly `--connect-path` (default `/connect`, `/` in the example); the owner API and MCP
remain on their existing loopback listener. It uses a separate dispatcher lock
in `data_dir`. Its SQLite registry can be administered by `computers` commands
while it runs. `computers list` shows enrolled/enabled identities, not online
connection status. The standalone mode has no owner API or core task routing;
do not run it on the same ingress port as `serve`.

Securely copy the enrollment credential to the computer and keep it in a file
owned by its local Linux user with mode `0600`. Configure local agent profiles
in the computer's YAML, then run using the dispatcher's IP and port:

```yaml
# computer.yaml (computer; .env holds LOCAL_GOOSE_SECRET)
agents:
  - alias: goose
    kind: goose
    url: ws://127.0.0.1:3000/acp
    secret_env: LOCAL_GOOSE_SECRET
    default_cwd: /work
```

```bash
acpgw --config /path/to/computer.yaml connector \
  --dispatcher-url ws://192.0.2.10:8766 \
  --computer-id work-laptop --token-file /private/path/work-laptop.key
```

For explicit WSS, supply both listener TLS files and use a `wss://` URL. Without
`--tls-fingerprint`, the connector verifies the certificate's CA and host name.
For self-signed certificates, pass the SHA-256 pin obtained through a trusted
route, e.g. `openssl x509 -in dispatcher.crt -noout -fingerprint -sha256`.
A pin with a plain `ws://` URL is refused; TLS never falls back to plaintext.
Credential files must be regular, small, private and locally owned; symlinks
are refused. Dispatcher URLs cannot contain credentials, queries or fragments.
HTTP redirects are refused. The credential is sent in the authorization header
after the selected transport connects (and validates TLS when WSS is selected).
Wire DEBUG traces are disabled
to keep authorization headers out of logs. No local agent URL/credential is sent
in the manifest. Registration advertises configured aliases; each relay stream
opens its own local ACP socket on demand. Local agent secrets are checked for
presence before registration. The computer accepts only direct agent profiles.

Authorization occurs before WebSocket upgrade and is checked again when hello
arrives. Hello must match the authenticated computer ID. Each accepted
connection gets a new UUID epoch. A new authenticated hello for an active computer
replaces its old connection. Invalid credentials or hello cannot displace it.
The old handler's cleanup cannot remove the new registration. Native WebSocket
ping/pong defaults to 20 seconds. A single application ping remains for v1
compatibility; subsequent liveness does not depend on application heartbeat
traffic. In-process rotation/revocation fences streams immediately; external
CLI changes close an active connection on the next access check, within one second.
Failure to read the access registry also closes it. One computer's revocation
leaves other registrations running. The computer retries transient network/5xx
failures with 1/2/5/10/30-second backoff. Access, TLS, protocol and replacement
errors stop it for correction. Interrupted prompts are never replayed automatically;
a later explicit request can reconnect/load a stored session if the agent supports it.
Use Ctrl+C for foreground shutdown; dedicated service modes are stage 5 work.

Rebuild locally to include these modes in a standalone binary. The opt-in smoke
`scripts/smoke_connector.py /path/to/acpgw` runs both modes outside the checkout
with no Python on the child PATH, checks connection liveness, live revocation and
interrupt shutdown. It uses only temporary certificates, config, DB and keys.

### Relay wire boundary

Control frames remain limited to 64 KiB; data envelopes are limited to 1 MiB of
UTF-8 JSON. `open`, `opened`, `data` and `close` carry alias, stream UUID and
epoch UUID. The computer opens a local transport before acknowledging a stream;
ACP initialize determines readiness afterwards. Unknown aliases, mismatched
epochs/aliases, malformed ACP IDs and reused stream IDs cannot reach another
stream. Retired-stream traffic is discarded. Each epoch has at most 32 active
streams and 4096 lifetime stream IDs.

One reader demultiplexes traffic. Local streams have independent workers and
2-MiB/128-message inbox limits. The shared FIFO writer has a 2-MiB data limit,
64 control slots and a five-second send deadline. Overflow or policy violations
close the affected stream; a broken computer socket fences all its streams.
Closing a stream does not close other streams. Local open errors distinguish
retryable unavailability from access/TLS failures. User decisions remain bound
to their live stream; AgentClient also discards updates/permissions from a
retired transport. Durable deduplication and recovery of uncertain results
remain future work.

## Domain and nginx path layout

The dispatcher URL accepts IP addresses, DNS names, `/` or a configured subpath.
For example, use `--connect-path /gateway/connect` and connect to
`ws://dispatcher.example/gateway/connect`. The nginx upstream can stay on
`127.0.0.1:8766`; the owner API/MCP are not proxied by this location.
For `serve`, set `connector.connect_path: /gateway/connect` in the VPS YAML;
the CLI flag above applies to the standalone diagnostic listener.

Example inside an existing nginx `server` block:

```nginx
location = /gateway/connect {
    proxy_pass http://127.0.0.1:8766;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-ACP-Computer $http_x_acp_computer;
    proxy_read_timeout 90s;
}
```

The Upgrade/Connection headers are explicit as required for
[nginx WebSocket proxying](https://nginx.org/en/docs/http/websocket.html).
`proxy_pass` has no URI suffix so it preserves the configured path; see
[nginx proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass).
For a domain root endpoint use `--connect-path /` and `location = /`.
Later, adding TLS to the nginx server changes the connector URL to `wss://`
while its upstream remains plain WS on loopback. Do not rely on HTTP redirects
to select the endpoint; the connector refuses them. These snippets describe
the supported layout; no nginx host or public VPS is deployed by these commands.
