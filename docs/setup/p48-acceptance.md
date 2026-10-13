# P4.8 candidate: VPS, Telegram and native Windows Hermes

[Russian step-by-step self-install guide](self-install-0.2.0rc1.ru.md).

The development candidate is 0.2.0rc1. Public 0.1.0 does not contain this workflow.
Use the same verified candidate on the VPS and connector. Installation and live
acceptance are separate from source tests. Start with the current Windows
computer; repeat isolation checks on the second computer.

## Deployment inputs

Prepare the VPS SSH endpoint/account, an IP:port ingress URL, and the reverse
proxy domain/path URL. Both paths must reach the same dispatcher, for example
ws://IP:8766/connect for explicitly opted-in temporary plaintext testing and
wss://example.test/acpgw/connect through the existing TLS proxy. Keep the owner
API on loopback; use the CLI over SSH on the VPS. Do not expose owner credentials
on a public HTTP endpoint.

Keep the Telegram bot token in the VPS private environment file. Record the
owner's numeric Telegram user ID in the allowlist. Copy the connector enrollment
key privately to the Windows computer; do not paste credentials into chat.
Provider keys remain in the existing native Hermes installation.

Follow [Windows connector](windows-connector.md), [VPS acceptance](vps-acceptance.md)
and [native Hermes deployment](vps-native-hermes.ru.md) for setup and enrollment.

## CLI checks before live prompts

On the VPS, use the actual config/env paths before each command:

    acpgw version --json
    acpgw config check
    acpgw doctor --live
    acpgw computers status
    acpgw new --agent home/hermes
    acpgw sessions --agent home/hermes
    acpgw model show --agent home/hermes --session ID
    acpgw model list --agent home/hermes --session ID
    acpgw model state --agent home/hermes --session ID
    acpgw model set PROVIDER MODEL --agent home/hermes --session ID --revision REVISION
    acpgw model reconcile --agent home/hermes --session ID --revision REVISION
    acpgw model default set PROVIDER MODEL --agent home/hermes
    acpgw model default show --agent home/hermes
    acpgw model default clear --agent home/hermes

Read ID and active_revision from sessions. A confirmation_required response
requires an explicit repeat of the same set command with its
--confirmation-token TOKEN. A selection change invalidates that action. Do not
put a token into logs or scripts. Show/list report a model.options observation,
not proof of which provider actually served a model request.

Defaults are shared by CLI and Telegram for the current single owner/workspace,
route and Hermes profile. A connector profile must first be identified by
creating a dialogue. Setting a default performs no host RPC and changes no
existing dialogue or Desktop profile. It is explicitly applied to later new
dispatcher dialogues. If application fails or needs confirmation, the created
dialogue remains recorded and prompting stays blocked until explicit selection.

On Windows, run CLI in native PowerShell, with the saved connector config:

    acpgw config check
    acpgw doctor --live
    acpgw service --role connector status
    acpgw maintenance --role connector status
    acpgw connector-sessions list --agent hermes
    acpgw connector-sessions journal HANDLE --agent hermes

Local aliases use hermes; dispatcher routes use home/hermes. Local commands
need the running connector and its private data directory, not the VPS owner
token. A journal contains up to 128 events per retained handle, with up to 512
characters per answer chunk. It is a diagnostic tail, not a complete transcript.
Tool arguments, prompts, approval tokens and raw host errors are not retained.

connector-sessions retire HANDLE --agent hermes explicitly forgets only an
idle, detached handle with no pending model activity or permissions. It does not
delete the upstream Hermes chat. There is no automatic eviction. If capacity
must be freed, first select the dialogue to keep, finish work, disconnect the
dispatcher safely, and explicitly retire only old unused handles. A retired
logical dialogue stays recorded; it cannot be silently restored or replaced.

## Telegram checks

From the allowlisted private account:

    /agent
    /agent home/hermes
    /new
    /sessions
    /model
    /models
    /model set PROVIDER MODEL ID REVISION
    /model reconcile ID REVISION
    /model state
    /model default PROVIDER MODEL
    /model default
    /model default clear

Model responses include dialogue_id and selection_revision. If confirmation is
required, explicitly repeat /model set PROVIDER MODEL ID REVISION TOKEN.
Model commands never become prompts. A stale target, revision, confirmation or
agent-route change is rejected. /models truncates large catalogues; use CLI for
the complete list. Use ordinary /result and /approvals for saved jobs/buttons.

## Reconnect and reconciliation

Test idle reconnect on both IP:port and domain/path. Keep the logical dialogue
selected; neither transport errors nor reconnect may create a different chat.

For a lost model reply, explicit reconcile asks the same live connector manager
for the exact durable operation ID and binding. Only a known terminal ACK with
an idle driver can release that model-loss fence, followed by fresh observation.
It never repeats config.set. Lost confirmation returns not_applied and grants
no reusable confirmation authority.

Unknown host failures, missing/mismatched outcomes, connector/Hermes restart and
prompt-loss uncertainty remain fenced. Read the local journal and saved model
state; do not infer success from a matching catalogue. Prompt results received
after stream loss are recorded for inspection, but are not replayed or used to
grant writer authority. Cold resume/cancel is outside this Hermes MVP. Maintenance
refusal while work is uncertain requires explicit operator recovery, not forced
automatic updater retries.

## Windows logon and real acceptance

Install/enable the connector Task Scheduler service under the same interactive
Windows user as Hermes. Inspect the installed Hermes Desktop's own startup
setting first. If it has no supported setting, verify its installed launch entry
point and configure a separate per-user logon task during acceptance. Do not
guess an executable, launch Desktop as SYSTEM, or replace the existing backend.

Check logon/reboot with Hermes and connector starting in either order, then
sleep/wake and network loss. Confirm one connector per data directory, CLI service
status/logs, actual IP and domain/path ingress, selected-chat preservation,
Telegram answers and approvals. A real provider/model/tool request is a separate
explicit live smoke; synthetic tests do not establish its success.

Finally repeat with the second connector: different keys, route aliases and
catalogues, no cross-dialogue/approval access. Package/service update and rollback
must preserve config, environment, enrollment and SQLite. Record live evidence
separately from source and installed-wheel mock tests.
