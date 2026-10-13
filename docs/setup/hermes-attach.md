# Existing Hermes backend: discovery and attach spike

Development preview, P4.8.1–P4.8.4. Discovery, the dedicated dispatcher adapter
and setup integration are implemented. Live acceptance limits are recorded below.
It is separate from [Hermes calling Gateway through MCP](hermes.md).

## Read-only discovery

Run Gateway in the same OS environment and under the same user as Hermes:

```bash
acpgw agents probe hermes
acpgw agents probe hermes --role desktop-serve --json
acpgw agents probe hermes --lock-dir /absolute/path/to/gateway-locks
```

In a checkout use `uv run acpgw`. No Gateway configuration is required. Exit
code 0 means at least one backend passed the probe; 1 means none did. The JSON
report lists each role independently as `ready`, `missing` or `unavailable`.
`ready` confirms identity, authenticated protocol access and discovery only;
it does not claim that a selected conversation is idle or can be controlled safely.
The `access` object reports `mode=observe`, with shared prompt/cancel, approval
decisions and cold resume disabled. A backend's `exclusive_submit` capability
alone does not enable these operations.

Discovery uses `HERMES_GATEWAY_LOCK_DIR`, otherwise `$XDG_STATE_HOME/hermes/gateway-locks`
when absolute, otherwise `~/.local/state/hermes/gateway-locks`. On native Windows
the default is `%USERPROFILE%\.local\state\hermes\gateway-locks`.
Roles `desktop-serve` and `serve` have separate records and tokens.

The probe validates loopback addresses, token fingerprint, protocol version,
PID and role through `/api/host/identity` before connecting to `/api/ws`.
It then calls `ping`, `gateway.capabilities` and `session.active_list`.
It does not print tokens, session titles, history or provider credentials;
it creates no conversations, submits no prompts and accepts no approvals.
Proxy use and HTTP/WebSocket redirects are disabled. POSIX discovery files
must be private to the current user. On native Windows, discovery now checks
record/token ACLs and the rendezvous directory before reading either file.
Only the current user, SYSTEM and Administrators may have file Allow entries;
other identities must not have write/delete/ACL-changing access to the directory.
Windows OWNER RIGHTS entries are accepted only after verifying that the object
owner is one of those trusted identities; CPython uses them for private directories.
Reparse points and null DACLs are refused. This uses read-only Windows PowerShell
with a ten-second bound; unavailable/failed verification stops discovery with
`auth-required`. Permissions are never changed automatically. Native acceptance
of this discovery preflight passed on 2026-10-13, including private/public paths
and the existing Hermes Desktop; see the Windows handoff for evidence.

Do not point a WSL probe at a Windows token directory: Windows and WSL have
different user identities, file permissions and loopback environments. Run a
native Windows client for native Windows Hermes. Cross-environment discovery
is not implemented.

## Dedicated conversation spike

Gateway persists each dedicated dialogue's Hermes profile, runtime ID, stored ID
and connector handle alongside its own session row. These are provenance records;
they never authorize attaching to another Desktop dialogue. `/sessions` reports
`attached`, `busy`, `uncertain`, `restore-required` or `restore-unavailable`.
The computer connector now owns live Hermes attachments independently of WSS.
New connector dialogues carry `restore=connector-live`: after an idle VPS-link
drop or dispatcher restart, ACP `session/load` rebinds the same live local handle.
No Hermes activate/resume RPC is sent. The connector checks local creation,
cwd, idle state and exclusive stream ownership; the dispatcher checks the returned
profile/runtime/stored provenance against SQLite before sending a prompt.

The connector and its Hermes attachment must still be alive. Losing a remote
stream during a pending turn permanently quarantines that dialogue in this
manager; no prompt/approval/result is replayed. An unknown handle, another live
owner, a lost Hermes attachment or restarted connector is refused. The active
selection remains recorded. Direct local adapters and older `restore=disabled`
rows keep their former refusal rule; `/new` explicitly creates a fresh dialogue.
The manager is scoped to the configured local alias, for the current single-owner
deployment; account/workspace isolation is still a separate P4.10 implementation.

From the checkout, with Gateway dependencies available in the agent's OS:

```bash
python scripts/spike_hermes.py                         # read-only
python scripts/spike_hermes.py --sessions              # creates one conversation
python scripts/spike_hermes.py --sessions --prompt     # one fixed model request
python scripts/spike_hermes.py --observe-session LIVE_ID  # attach to a live test chat
python scripts/spike_hermes.py --resume-session STORED_ID --allow-auto-continue
```

`--sessions` creates a conversation titled **ACP Gateway attach probe** and
attaches a second independent client in observation mode. `--prompt` submits a fixed text request,
consumes each client's events through `message.complete`, and verifies successful
completion. This may use provider credits. Only the first diagnostic client can
deny requests, and only inside its explicitly bound test session/profile.
Requests for other sessions are left to their owner, without even an error
response: an error would also settle Hermes' shared pending request.
The script never retries a prompt after timeout or disconnect.

`--observe-session` activates an already live test conversation with
`omit_messages=true`, without advertising approval handling. It never falls
back to resume if the live ID disappeared. Attach changes subscription and
session liveness; it does not submit or interrupt a turn. The snapshot can
still include in-flight state; the report does not print its content.

`--resume-session` attaches a saved test conversation with `omit_messages=true`.
**Cold resume is not a read-only operation:** Hermes can automatically continue
a crash-interrupted turn, even without `prompt.submit`. The flag
`--allow-auto-continue` explicitly acknowledges that behavior and is mandatory.
`omit_messages` skips transcript transfer; it does not suppress recovery.
Use the saved `stored_id` from an earlier prompt run. A newly created conversation
without a prompt may not have a persisted row yet. Live runtime IDs can change
between attachments; the saved ID must stay stable. Hermes 0.21.6 snapshots can
use either `stored_session_id` or `session_key` for that saved identity.

The script checks both peers' live and saved IDs, disconnects the first, then
pings the remaining peer. It leaves test conversations for the owner to inspect
and remove in Desktop; it never closes or deletes a shared session.

For the installed Windows Hermes runtime, this session used its existing Python
and bootstrap without installing packages or editing Hermes. From PowerShell,
replace the checkout path and runtime directory with those on the machine:

```powershell
$probePython = "$env:LOCALAPPDATA\hermes\tools\python-3.14.7+20260901-win32-x64\python.exe"
$probeCode = @'
import os, sys, runpy
sys.path.insert(0, os.path.join(os.environ['LOCALAPPDATA'], 'hermes', 'hermes-agent'))
import hermes_bootstrap
sys.argv = ['spike_hermes.py', '--sessions']
runpy.run_path(r'C:\Projects\acp-gateway\scripts\spike_hermes.py', run_name='__main__')
'@
& $probePython -I -c $probeCode
```

This is a version-specific diagnostic invocation, not an installer or supported
Windows service command. An existing Gateway Python environment can run the
script directly instead.

## Evidence and remaining boundaries

On 2026-10-12, native Windows Hermes Desktop **0.21.6**, rendezvous v1:

| Check | Result |
|---|---|
| Discovery, authenticated identity, status, RPC | Passed on installed Desktop backend |
| Create and second-client activate | Same live and stored conversation |
| Diagnostic creator + observer peer | Passed on native Windows; no new model call |
| One fixed model prompt | Both clients received streaming events and successful completion |
| Disconnect one peer | Remaining peer responded to ping |
| Resume saved test conversation | Same saved ID, new live ID, second peer attached successfully |
| GUI displays the same conversation/events | Still requires visual acceptance in Desktop |
| Real approvals, busy race and safe cancel | Still require dedicated acceptance and ownership design |
| Goose, second Windows PC, Linux/WSL/macOS agents, VPS/Telegram | Not exercised by this spike |

The machine-readable [evidence summary](../items/hermes-attach-spike-2026-10-12.json)
contains no tokens, personal history or real session IDs. It is a summary of
observations, not a complete captured wire transcript. Model-free integration
tests exercise HTTP/WS authentication, interleaved notifications and server
requests, default denial, identity mismatch, timeout without replay, protocol
failure fencing and the two-peer scenario.

The default RPC client now refuses mutation and cold resume. Explicit diagnostic
access permits prompt/interrupt only for one bound session/profile and rejects
extra mutation fields such as history truncation. A diagnostic grant is not an
upstream ownership lease and must never be used to control a production shared chat.
Observer clients ignore shared server requests rather than denying Desktop's
approvals. Tests also cover port/PID/token replacement, torn record/token updates
and refusal of HTTP and WebSocket redirects.

## Installed-source semantics lab

```bash
python scripts/spike_hermes_semantics.py --hermes-repo /path/to/hermes-agent
HERMES_SEMANTICS_REPO=/path/to/hermes-agent pytest tests/integration/test_hermes_semantics.py -q
```

This separately executes the installed upstream busy, interrupt and request-broker
helpers with synthetic in-memory sessions and fake agents/persistence. It does
not start a server, call a provider or execute tools. It confirmed queue/steer/
redirect behavior, legacy queue+interrupt, late session cancellation affecting
the next owner, first-response-wins approvals and rejection of expired/cancelled
answers. Source hashes and normalized broker frames are recorded in the
[compatibility fixture](../../tests/fixtures/hermes/0.21.6/README.md).
This is stronger than a reimplementation mock, but remains separate from live
Desktop tool approval and cancellation acceptance.

**Busy:** `per_session_exclusive_submit=true` does not guarantee refusal of a
second prompt inside the same backend. The installed Hermes busy handler can
queue, steer or redirect it. A read-before-submit busy check alone would race.
The production adapter needs an atomic upstream guard or a clearly restricted
conversation ownership policy before allowing shared Desktop/Telegram submits.

**Cancel:** `session.interrupt` targets a session, not necessarily the exact turn
started by Gateway. Blind cancellation could interrupt a later Desktop turn.
Turn ownership and late cancel handling remain explicit design work.

**Approvals:** the diagnostic deny behavior does not establish arbitration between
Desktop and Telegram approvers. Real pending-request ownership, expiration and
duplicate/late responses need tests before production translation into ACP.
The upstream broker's first response wins, including an error response. A second
surface must not automatically deny a request merely because it has no approver.
Sending a response does not itself prove that it won; settlement confirmation
and stale-card cleanup still need an end-to-end check.

The current decision and proposed upstream requirements are recorded in
[shared-session control](../items/hermes-shared-control.ru.md).

No local Hermes token is sent to the dispatcher. No production relay path,
automatic reconnection, service lifecycle or full platform support is claimed
by these diagnostic commands. Follow [P4.8 in the roadmap](../../road-map.md).

## Dedicated dispatcher conversations (P4.8.2 foundation)

The chosen MVP creates separate dispatcher conversations in the same running
Hermes. `adapters/hermes.py` contains the connection-local driver, and
`adapters/hermes_acp.py` translates it into an ACP stream. Setup can now select
this attachment. Run this driver smoke test in the agent's OS:

```powershell
python scripts/spike_hermes_dedicated.py --cwd C:\Users\your-user\work
```

It creates one conversation, checks live/stored identity through an observer,
then detaches without stopping Hermes or deleting the conversation. No inference
is requested. Optional `--prompt` sends one fixed text-only model prompt and
waits for completion. Own permission requests are denied until the human approval
bridge exists; foreign requests receive no response.

Keep these conversations reserved for the dispatcher. Reading them in Desktop
is fine; simultaneous Desktop submits, steering and cancellation are unsupported.
The local ownership registry does not provide an upstream exclusive writer lease.
Prompt timeout, nonisolated acceptance or detected interference blocks further
writes to that conversation. Lost connections invalidate every handle: there is
no automatic replay, adoption, cold resume or session interrupt. Cancelling a
local wait or closing the driver does not cancel an already running Hermes turn.

## Interactive setup (development)

Use a build containing P4.8.3 and run `acpgw setup` in the agent's own OS,
under the same user. Select the computer connector role (or local gateway),
then choose **hermes** as the agent type. For an existing configuration use
`acpgw agents configure` and add/edit a profile.

The wizard shows the current OS/WSL environment and asks for the rendezvous
directory, defaulting to the standard per-user location. It checks the
desktop-serve/serve roles, shows version, profiles and active-session count,
then lets you choose an instance and profile. This probe creates no sessions,
sends no prompt and never responds to Desktop permission requests.

Choose an alias and a native working directory. Add another profile to attach
another role or directory. Saving stores the discovery directory, role and
profile, without copying the temporary port/PID or token. Repeated setup keeps
existing keys and offers keep/edit/add/remove/replace. If nothing is verified,
the wizard stops unless you explicitly choose to save an offline attachment.

On the server choose **hermes** with the server role and the computer ID;
the wizard creates only a routed profile and does not discover an agent there.
Connecting Hermes as an executor is separate from the optional MCP integration
that exposes Gateway tools inside Hermes; that MCP integration modifies its
configuration only when explicitly selected.

Use `acpgw doctor --live` for a fresh read-only check. It reports missing records,
authentication refusal, incompatible data, offline endpoints or a missing
configured profile. `acpgw doctor` without `--live` makes no backend request.
Windows and WSL stay separate: setup does not search the other OS or start Hermes.
The wizard flow passed local HTTP/WS mocks on Linux and native Windows. Native
observer selection of the actual Desktop also passed without saving settings.
Full user installation on the second PC and two-PC acceptance remain open.

## Manual connector configuration (development)

Use a build containing P4.8.2. Run the connector in the same OS and under the
same user as Hermes. On the Windows computer, add this local agent profile:

```yaml
agents:
  - alias: hermes
    kind: hermes
    backend: hermes
    default_cwd: 'C:\Users\your-user\work'
    hermes:
      role: desktop-serve
      profile: default
```

Choose an existing native working directory. For Linux/WSL/macOS use a native
absolute POSIX directory instead; live support for those platforms still needs
acceptance. Optional `hermes.lock_dir` is an absolute native path to the host
record directory. Otherwise discovery uses the standard per-user directory.
Do not configure a Hermes URL, port or bearer token: each new connection reads
the record again and verifies the current backend identity.

The VPS config uses a routed profile with the same alias and cwd:

```yaml
connector:
  enabled: true
agents:
  - alias: hermes
    kind: hermes
    backend: connector
    computer_id: home
    default_cwd: 'C:\Users\your-user\work'
```

Start the computer connector using its existing enrollment credential and WSS
dispatcher endpoint:

```powershell
acpgw --config C:\path\computer.yaml config check
acpgw --config C:\path\computer.yaml connector --dispatcher-url wss://dispatcher.example/acpgw/connect --computer-id home --token-file C:\path\computer.key
```

Only one connector may use a given `data_dir` at a time. A second foreground
or autostart process exits with code 78 before connecting. The OS releases the
lock when its owner exits or crashes; do not delete `connector.lock` to bypass a
running process. Separate connector installations need separate data directories.
Native Windows Task Scheduler/ACL/locking acceptance passed on 2026-10-12;
reboot/logon, sleep/wake/network changes, Hermes ACLs, graceful drain and packaged
installation remain open. See the [Windows connector runbook](windows-connector.md).

Repeat for `work` using its own credential. Enrollment/TLS deployment is described
in [VPS acceptance](vps-acceptance.md); these examples do not deploy a VPS or
establish that live two-host acceptance has passed.

The adapter exposes initialize/new/list/text prompt and once-only human approvals.
Managed streams additionally expose local live load. List includes only the
current stream's attached conversations. It forwards only
permission requests actually raised by Hermes; it does not change the agent's
own approval configuration or add a prompt before every tool. Verify the
Hermes profile's tool/approval policy before live acceptance.

Expired/withdrawn requests are removed from Gateway approvals, and late replies
are ignored. A sent decision is not an authoritative settlement ACK from Hermes.
Keep Desktop as a reader for dispatcher-owned chats, including approval decisions.
Cancellation and cold restoration remain unavailable. Managed connector streams
advertise `connectorLiveResume=true` and `loadSession=true` only for idle handles
still retained by their own live manager. Plain local adapter streams continue
to advertise `loadSession=false`. List reveals only the current stream's attached
sessions, not every retained handle. Connector shutdown closes its own attachments
without stopping Hermes; those handles cannot be loaded by a new manager.
Retention is currently bounded to 32 conversations per local alias/driver,
with 32 managed streams and 32 pending requests. Each stream buffers at most
128 frames, 1 MiB total and 256 KiB per frame; overflow revokes that stream.
There is no automatic eviction or silent replacement of a selected dialogue.
Local connector-sessions list/journal/retire provides explicit retention
management and a bounded diagnostic journal. Retirement accepts only detached
idle handles with no unresolved model work; it never deletes the upstream chat.
See the [P4.8 acceptance guide](p48-acceptance.md).
The ACP/relay integration has been tested with a real ACP SDK and local HTTP/WS
mock backend; native Windows approval and public VPS/Telegram acceptance remain open.
