# Connector-first pairing

Pairing is for the current owner's computers, not public registration or multi-user
isolation. The existing owner HTTP API remains loopback-only. No password, copied
key, YAML edit or certificate fingerprint is needed in the ordinary connector path.

## Dispatcher owner bootstrap

After configuring the dispatcher listener, run in the trusted local OS account:

```console
acpgw --config /absolute/config.yaml --env-file /absolute/private.env owner bootstrap --telegram-id 123456
acpgw --config /absolute/config.yaml --env-file /absolute/private.env owner show
```

Repeat `--telegram-id` only for explicitly chosen approvers. An empty list means
local CLI approval only. An existing Telegram allowlist is never automatically
promoted into owner authority. Telegram approvers must also pass the channel's
configured allowlist when it is enabled. Repeating bootstrap preserves the stable
owner ID and refuses a different approver set. Server setup can also offer this
explicit bootstrap. `/owner` displays the profile to its authorized approvers.
The public owner ID is an address, not a password or authorization credential.

## New connector

Run `acpgw connect` on the applicant computer. A fresh installation opens the full
connector setup; an existing installation edits only the connection. Keep the
selected `--config` and `--env-file` when they are non-default.

1. Choose **owner approval** (the default), enter dispatcher address and owner ID.
2. Enter a display name. Leave the previous computer ID empty for a new computer.
3. Compare the request ID, comparison value, name and exact new/rebind target with
   the owner. Do not approve an unsolicited request or compare just a hostname.
4. In the owner's Telegram use `/pairing`, then type the displayed
   `/pair_approve REQUEST_ID COMPARISON` or `/pair_deny REQUEST_ID COMPARISON` after
   comparing with the applicant screen. The comparison value is public, not the
   applicant bearer. There is no blind one-button approval.
5. Alternatively, on the dispatcher:

   ```console
   acpgw --config /absolute/config.yaml pairing list
   acpgw --config /absolute/config.yaml pairing approve REQUEST_ID --verification COMPARISON
   acpgw --config /absolute/config.yaml pairing deny REQUEST_ID --verification COMPARISON
   ```

6. Continue on the applicant. The application privately saves the issued credential
   and connection metadata, then acknowledges activation. Until that ACK the new
   credential cannot authenticate a relay or remote update.
7. Check the agent separately. `acpgw agents configure` / `acpgw agents probe` help
   identify an existing backend. Finish the agent's own authorization and backend
   startup if it is missing or offline. No agent/provider is silently installed,
   configured or contacted with a model task.
8. Choose autostart explicitly. A connection edit refreshes an existing owned
   service/task using the existing service inspection, maintenance reservation,
   systemd/Task Scheduler adapters and guarded Windows setup snapshot. Busy or
   uncertain activity is refused. Changes are previewed; running/enabled state is
   restored and selected config/env/data paths and agent profiles are preserved.
   No second connector service is created. Failed settings/service writes attempt
   rollback and report incomplete recovery rather than pretending to be ready.
9. Start the connector, select its `/agent` route in Telegram, and explicitly use
   `/new` before sending a task. Binding is not ready-agent or service acceptance.
   Pairing does not switch an active dialogue or promise live attachment recovery.

The advanced credential-file option remains for explicit administration, not as a
fallback when pairing is denied, expired or inaccessible. After the legacy boundary
is activated, legacy credentials do not regain relay/update access through it.

## Transport and endpoint discovery

An address without a scheme means **WSS**, never automatic plaintext. WSS uses the
normal OS CA chain and certificate name/date validation. No application encryption,
self-signed certificate bypass, TOFU, or automatic certificate provisioning is
introduced. A DNS name needs a trusted certificate for that name; an IP needs a
trusted certificate containing the appropriate IP subjectAltName. Configure that
certificate/proxy outside the wizard, or explicitly choose the accepted WS risk.

For an address with no path, discovery tries only `/connect`, `/acpgw/connect`, then
`/` on the same scheme/host/port. Only an HTTP 404 permits the next candidate. For a
custom reverse-proxy subpath, enter the full connector URL. Pairing uses that
connector path with `/pair` appended (root becomes `/pair`). Forward that path to
the **same connector listener**. All redirects, including same-origin redirects,
are refused before any applicant payload is sent. TLS errors never cause a WSS to
WS downgrade. No bearer appears in a URL, query, argv, Telegram message or log.

WS is supported for self-hosting only after the explicit warning and consent.
It exposes applicant secrets, permanent credentials and conversations to network
observers; approving pairing does not make WS safe. This is a deliberate deviation
from the TLS requirement in RFC 8628, not a claim of OAuth conformance.

## Recovery and limits

Private applicant state is under `.<config-filename>.pairing/` alongside the selected
configuration. It is outside the replaceable package environment. The 256-bit
applicant bearer is saved **before** the first request, so a lost request reply can
resume the same request. File locks refuse concurrent local wizards. Credential
files and applicant metadata are atomically replaced, file-fsynced, and their
parent directory fsynced on POSIX; native Windows uses the existing private ACL
checks and atomic file replacement. Power-loss durability of Windows directory
metadata still requires native acceptance testing.

The wizard resumes pending or completed state. It can explicitly cancel a pending
request, or archive a completed/expired request before starting another. Archiving
never deletes an old credential or revokes an old computer: revoke it separately
if it is no longer yours. If a claim/ACK was interrupted, rerun `acpgw connect`:
claims recover the same exact credential and generation until request expiry;
ACK retries return the same activation. A revoke, rotation, target revision change,
foreign owner, or boundary mismatch prevents old issuance from being resurrected.
After the request expires, claim/ACK are refused even if approval happened earlier.
Use an explicit new rebind request for an incomplete issued computer, not automatic
replacement by alias. Local pending state records the issued computer ID before ACK.

Defaults and hard bounds:

- 600-second request lifetime (configuration accepts 60–1800 seconds).
- 256 retained requests including unknown-owner dummy requests (1–1024 configurable).
- 120 pre-auth handshakes per minute per listener, with bounded rate-limit memory.
- Existing listener connection cap (64 by default), five-second handshake and
  single-request receive deadlines, one 4096-byte JSON request per pairing connection.
- Duplicate JSON fields, extra operation arguments, binary/oversized payloads and
  browser-origin WebSocket handshakes are rejected. No owner operation is exposed.
- Expired rows are deleted on new requests/owner listing and every 30 seconds while
  the daemon ingress runs. Audit retains at most 4096 decision/issuance/activation
  records without bearer values.

Unknown owner IDs enter the same bounded request path with the same public response
shape/status and no owner-existence branch. Only the local authorized owner can
list/resolve requests addressed to its stable ID. The short-lived private SQLite
issuance row retains the raw permanent credential to recover lost claim/ACK replies;
long-lived computer records retain its digest. Private DB/WAL files and backups
must therefore be treated as credentials. Cleanup is not forensic erasure of old
backups or WAL pages. No permanent credential is returned by owner listing or audit.

## One-time mandatory legacy rebind

**Do not activate this against a real installation until the owner has completed
its local upgrade and backup checks. Loading/importing new software does not
activate migration.**

1. Inventory and back up the stopped dispatcher's consistent database, configuration,
   data paths, service/task selection, existing computer IDs and agent profiles.
2. Upgrade the dispatcher and connectors through local installation/update paths
   that do not require a legacy relay credential. Prepare owner CLI access first.
   Offline old computers can be upgraded locally later; do not enable a legacy
   remote-update bypass just to enroll them.
3. Make the irreversible boundary explicit:

   ```console
   acpgw --config /absolute/config.yaml pairing activate-legacy-rebind --confirm
   ```

4. The external `data_dir/pairing-boundary.json` is fsynced **before** the DB boundary
   commit. Every legacy computer is disabled and its generation advanced. Existing
   connections are fenced immediately in-process and by the normal generation
   watcher across processes. A holder of a valid retired legacy key receives
   `migration-required` and guidance to upgrade locally and run `acpgw connect`;
   random credentials still receive a generic unauthorized response.
5. On each computer choose its previous stable computer ID as the explicit rebind
   target. The owner compares that target and request; matching aliases/hostnames
   never confer replacement authority. Claim replaces authority transactionally,
   ACK activates it, and IDs/history/results/settings remain. Later updates keep
   this new binding without another migration.

The boundary is outside the DB snapshot and must never be rolled back or removed
by an updater. Missing/mismatched marker/DB epochs fail closed. A crash after marker
creation but before the DB commit requires forward recovery from a matching
post-boundary DB; deleting the marker is **not** recovery. Restoring an old DB while
keeping the boundary cannot revive old credentials in this version. Running an old
binary with an old snapshot is prohibited after activation; this software cannot
make an administrator's deliberate whole-volume rollback or marker deletion
cryptographically tamper-proof. Do not promise arbitrary downgrade support.

### Managed updater integration contract

Preserve the pairing directory, token files, computer records, owner identity,
profiles/config/env/data selections and **external boundary marker** across package
replacement. Never include that marker in a rollback-to-old-authority restore.
The independent local updater and its IPC must remain usable without a valid remote
connector credential; migration does not authorize remote maintenance with an old
key. Before accepting a restored database, opening `Store` and constructing
`PairingService(store, boundary=data_dir / "pairing-boundary.json")` validates the
boundary without bootstrapping an owner or activating migration. Refuse an epoch
mismatch and recover forward. This contract does not install or restart an updater.

## Standards and security review

Primary references reviewed: [RFC 8628](https://www.rfc-editor.org/rfc/rfc8628),
sections 3 and 5 (TLS, distinct high-entropy device bearer and public comparison
code, expiry, brute force/phishing), and
[RFC 9700](https://www.rfc-editor.org/rfc/rfc9700), particularly credential leakage,
redirect/issuer boundaries and TLS-terminating proxies. This is a private enrollment
protocol **inspired by** the device authorization pattern, not OAuth and not an
implementation of the RFC's HTTP endpoints, access-token scopes or refresh tokens.

Explicit differences: the applicant generates and persists its high-entropy
`device_code` before request submission for retry deduplication; the server stores
its SHA-256 digest. JSON is transported through one bounded WebSocket request on
the connector listener. A public 96-bit truncated SHA-256 comparison binds the
immutable request ID/owner/name/target/revision; it is not an authentication secret.
Owner CLI/Telegram approval is out-of-band and applicant claim still requires the
private device bearer. Two-phase disabled issuance/save/ACK is added for durable
activation, and raw issuance is recoverable only during the request lifetime.
There is no homemade encryption, signing scheme, password recovery or proof-of-
identity-by-hostname. WS is explicitly insecure and outside RFC TLS compliance.

Local automated evidence exercises real SQLite, real WS/WSS sockets and mock-agent
relay → route → explicit new dialogue → result, including lost ACK, concurrent
claims, expiry/deny/revoke, owner/target binding, migration and rejected redirects.
It does **not** prove live Telegram delivery, publicly issued IP/DNS TLS, real
Windows Task Scheduler ACL/power-loss behavior, real systemd changes, a production
mixed-version upgrade/rollback, real provider readiness, or the owner's own-PC
smoke acceptance. Those are residual release gates, not completed live acceptance.
