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
| 2 | In progress: WS/WSS registration, heartbeats and newest-wins implemented; ACP route, requests/results/human approvals next |
| 3 | Multiple computers/agents, selection in channels, isolated session mappings |
| 4 | Heartbeats, reconnect/connection epochs, bounded queues, deduplication and uncertain task outcomes |
| 5 | VPS deployment, connector services/upgrades and local stdio bridging |

Existing core, policy, approval audit, sessions, Telegram/MCP, ACP client,
binary build and Linux user services are reused. Native Windows and macOS
are separate platform work; they do not block the Linux/WSL route.
Enrollment and frame validation alone do not constitute an operational
connector. The relay and deployment are subsequent stages.

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
Task routing is the next increment of stage 2.

## Relay preparation: transport injection

`AgentClient` accepts an optional asynchronous `transport_factory`. Each call
must open a fresh ACP stream implementing `AgentTransport`: `send`, `receive`,
idempotent `close`, and a `closed` event. EOF returns `None`; delivery failures
raise `ConnectionError`. Factories own routing, authentication, TLS policy,
connection timeouts and cleanup of partially opened resources; normalize open
failures to the existing agent error types for retry/fatal-error handling.
Once a stream is returned, the client owns its lifecycle, including closing it
on failed or cancelled initialization. Closing a future relay stream must not
close the shared computer connection.

Without a factory the existing direct WebSocket connection, authentication and
certificate pinning are used. With a factory the client does not probe the local
agent endpoint or transmit its configured secret; `tls_pin` is `None`, and
connection logs mark TLS as transport-managed. ACP initialization, session modes,
load/replay suppression, updates, permissions, cancellation and reconnection
remain in the same client. Integration tests exercise these operations against
the recorded mock agent over an in-memory ACP stream, including a disconnected
turn that fails without replay.

This is the implemented seam for a future `RelayTransport`; it does not add
task routing to the registration channel. The local policy described below is
also implemented and tested. The owner accepted the architecture below on
2026-10-08; those decisions no longer block relay implementation.

## Accepted relay architecture

- Connector ingress becomes a second listener in `serve`, sharing the runtime
  with core/channels/approvals; the current standalone registration listener is
  an interim diagnostic mode, not the future routing process.
- Relay carries ACP JSON-RPC in a multiplexed envelope with agent alias, stream
  ID and connection epoch. The VPS reuses AgentClient with RelayTransport; the
  computer applies PolicyTransport before forwarding traffic to its local agent.
- The newest fully authenticated registration replaces the old connection and
  receives a fresh epoch. Old stream results and approvals must be rejected.
  Registration replacement is implemented; relay epoch fencing is still pending.
- One reader demultiplexes traffic, with WebSocket keepalive, separate control/data
  limits and byte-bounded stream queues. Overflow fails the affected stream.
- Core addresses remote agents as `computer/agent`, distinguishing computer
  connectivity from agent readiness. Network failures retry with backoff;
  access/TLS failures require correction, and uncertain prompts are not replayed.
- In-process access revocation notifies connections immediately. Public ingress
  gets handshake/connection limits as deployment work.
- TLS is disabled for current IP/port tests. Domains, root paths and subpaths
  remain supported for a later nginx front end. TLS can be enabled explicitly on
  the listener or terminated at nginx; plaintext upstream stays on loopback in
  that deployment. The direct gateway-to-agent transport keeps its own policy.

## Local ACP policy for the first relay route

`LocalAgentPolicy.from_profile(profile)` selects the local profile's
`default_cwd` and `session_mode`. `PolicyTransport(local_transport, policy)`
guards one computer-side ACP stream before it is exposed to the dispatcher.
It implements the same transport interface as the direct WebSocket transport.
The registration-only CLI does not forward ACP traffic yet; this boundary will
be wired into its relay handler, not into the existing direct gateway mode.

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
Wire frame limits, stream/epoch demultiplexing and reconnect scheduling are the
next relay layer, not implemented by this policy wrapper.

## Stage 2: WS/WSS registration channel

Two foreground modes now establish a real control connection. They register a
computer and its configured agent aliases; they do not relay tasks yet.

On the dispatcher host, enroll the computer with the stage 1 commands. For
current IP/port tests, start the listener without TLS:

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
connection status. There is no public registration-status API yet.

Securely copy the enrollment credential to the computer and keep it in a file
owned by its local Linux user with mode `0600`. Configure local agent profiles
in the computer's YAML, then run using the dispatcher's IP and port:

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
in the manifest, and registering does not connect to the ACP agent yet.

Authorization occurs before WebSocket upgrade and is checked again when hello
arrives. Hello must match the authenticated computer ID. Each accepted
connection gets a new UUID epoch. A new authenticated hello for an active computer
replaces its old connection. Invalid credentials or hello cannot displace it.
The old handler's cleanup cannot remove the new registration. Heartbeats default to 20 seconds;
missing or mismatched replies close the connection. Rotation/revocation closes
an active connection on the next access check, normally within one second.
Failure to read the access registry also closes it. One computer's revocation
leaves other registrations running. No reconnect or prompt replay is automatic.
Use Ctrl+C for foreground shutdown; dedicated service modes are stage 5 work.

Rebuild locally to include these modes in a standalone binary. The opt-in smoke
`scripts/smoke_connector.py /path/to/acpgw` runs both modes outside the checkout
with no Python on the child PATH, checks two heartbeats, live revocation and
interrupt shutdown. It uses only temporary certificates, config, DB and keys.

## Domain and nginx path layout

The dispatcher URL accepts IP addresses, DNS names, `/` or a configured subpath.
For example, use `--connect-path /gateway/connect` and connect to
`ws://dispatcher.example/gateway/connect`. The nginx upstream can stay on
`127.0.0.1:8766`; the owner API/MCP are not proxied by this location.

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
