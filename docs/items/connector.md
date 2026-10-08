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
| 2 | In progress: WSS registration/heartbeats implemented; ACP route, requests/results/human approvals next |
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
only in the WSS handshake authorization header. TLS must be validated before
credentials are sent. Enrollment is local in the first stage; remote pairing
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
WSS transport, authorization and heartbeat scheduling are implemented below.
Task routing is the next increment of stage 2.

## Stage 2: WSS registration channel

Two foreground modes now establish a real control connection. They register a
computer and its configured agent aliases; they do not relay tasks yet.

On the dispatcher host, provision a TLS certificate/key and enroll the computer
with the stage 1 commands. Then start the separate listener:

```bash
acpgw --config /path/to/dispatcher.yaml dispatcher \
  --host 0.0.0.0 --port 8766 \
  --tls-cert /private/path/dispatcher.crt --tls-key /private/path/dispatcher.key
```

Default binding is `127.0.0.1`; a public bind requires the explicit `--host`.
TLS is mandatory. This listener exposes only `/connect`; the owner API and MCP
remain on their existing loopback listener. It uses a separate dispatcher lock
in `data_dir`. Its SQLite registry can be administered by `computers` commands
while it runs. `computers list` shows enrolled/enabled identities, not online
connection status. There is no public registration-status API yet.

Securely copy the enrollment credential to the computer and keep it in a file
owned by its local Linux user with mode `0600`. Obtain the dispatcher certificate
SHA-256 fingerprint through a trusted route, for example the hex value printed by
`openssl x509 -in dispatcher.crt -noout -fingerprint -sha256`. Configure local
agent profiles in the computer's YAML, then run:

```bash
acpgw --config /path/to/computer.yaml connector \
  --dispatcher-url wss://dispatcher.example:8766/connect \
  --computer-id work-laptop --token-file /private/path/work-laptop.key \
  --tls-fingerprint AA:BB:...:FF
```

The pin is mandatory in this first mode; there is no implicit first-use trust.
Credential files must be regular, small, private and locally owned; symlinks
are refused. Dispatcher URLs cannot contain credentials, queries or fragments.
HTTP redirects are refused. The credential is sent in the authorization header
only after TLS validates the pinned certificate. Wire DEBUG traces are disabled
to keep authorization headers out of logs. No local agent URL/credential is sent
in the manifest, and registering does not connect to the ACP agent yet.

Authorization occurs before WebSocket upgrade and is checked again when hello
arrives. Hello must match the authenticated computer ID. Each accepted
connection gets a new UUID epoch. A second connection for an active computer is
refused; it cannot replace that registration. Heartbeats default to 20 seconds;
missing or mismatched replies close the connection. Rotation/revocation closes
an active connection on the next access check, normally within one second.
Failure to read the access registry also closes it. One computer's revocation
leaves other registrations running. No reconnect or prompt replay is automatic.
Use Ctrl+C for foreground shutdown; dedicated service modes are stage 5 work.

Rebuild locally to include these modes in a standalone binary. The opt-in smoke
`scripts/smoke_connector.py /path/to/acpgw` runs both modes outside the checkout
with no Python on the child PATH, checks two heartbeats, live revocation and
interrupt shutdown. It uses only temporary certificates, config, DB and keys.
