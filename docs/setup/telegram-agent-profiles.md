# Manage connector agent profiles from Telegram

Available since **0.2.3**. Both dispatcher and connector must run 0.2.3 or later.
Connector-owned discovery (P4.18) is included in **0.2.6**. Use dispatcher
0.2.7 and connectors 0.2.6 or later for the current fixes. A 0.2.3 connector
keeps template-based profile management; discovery requires a newer connector.

An owner can add, edit and remove **Codex and Claude Code profiles** on a connected
computer. **Добавить агента** discovers a locally installed CLI and can create
the first managed profile without a template. **Добавить профиль** copies an
existing managed profile's local executable, Windows/WSL environment and account
configuration. Installing CLI tools, signing
in, changing executable paths, and managing Hermes/remote ACP profiles remain
local operations.

## Local permission

New `acpgw connect` setup asks whether to allow remote profile management
(default yes). Existing installations keep their previous permission. To enable
it locally without editing YAML, use the saved connector config/env paths:

```sh
acpgw --config /absolute/config.yaml --env-file /absolute/private.env agents allow-remote-profiles
```

The command refreshes an installed owned service; restart a foreground connector
manually. Alternatively, while the connector is stopped, enable this in its config:

```yaml
connector:
  remote_profiles: true
```

Restart after a manual config edit. An absent `remote_profiles` setting means
remote administration is disabled. The setting
trusts the authenticated dispatcher to change project directories and safe/work
modes, separately from ordinary ACP execution permissions. Setting it back to
false locally refuses further profile requests; restart after local config edits
before using remote administration again.

## Use the bot

Open **Computers → computer → Configure profiles**, or `/profiles COMPUTER_ID`.
The user must be an authorized pairing owner and allowed in the bot's private
chat configuration. Management actions are not model tools.

- Select an existing profile to edit its title, directory or mode, or remove it.
- Choose **Добавить агента**, select a discovered Codex/Claude Code and enter an
  existing project directory, then safe/work mode and confirmation. The suggested
  directory is the agent's home; safe is the default. An alias such as `codex` or
  `claude_code` is generated with a numeric suffix if needed.
- Or choose **Добавить профиль**, select an existing managed template and enter
  a unique alias; then set the project directory and mode.
- Directories must already exist. Use Linux paths for WSL and Windows paths for
  native Windows agents. The connector checks the directory without starting the
  agent, hooks or model.
- Review and confirm. `/cancel` cancels pending text entry. Forms expire after
  two minutes; reopen the list if the form expires or configuration changes.

Service requests use a shorter 30-second deadline, capped by the form expiry.
This leaves headroom inside the connector's strict 120-second request bound when
the two computers' clocks differ slightly; it does not extend the form lifetime.

Active turns, pending requests/approvals, unknown outcomes, unsupported local
backends or another maintenance reservation block changes. Tasks are not
force-stopped. Successful changes update YAML, local policies and the dispatcher
catalog without restarting the whole connector service.

The previous config is backed up privately under
`setup-backups/profiles/OPERATION_ID.yaml`. Enrollment, `.env`, CLI credentials,
native history and project files are preserved. Removing the last agent is not
supported by the current registration protocol.

## Discovery and local troubleshooting

Discovery checks PATH and known installation directories on Windows, Linux and
macOS; Windows also probes eligible WSL2 distributions. It checks CLI `--version`,
not authentication. Candidate executable/account details remain connector-owned;
the dispatcher selects an opaque candidate ID valid for five minutes. Forms have
the shorter two-minute lifetime above; repeat discovery after expiry.

Candidates and hints name their OS or WSL distribution/user. WSL login banners
before the final JSON probe line are tolerated. An npm-only Claude Code on Windows
gets a local `claude install` hint because its shell wrapper cannot be used as a
native managed executable. If nothing is found, install/check the CLI locally and
repeat discovery; the bot does not install tools or log into accounts.

After saving a discovered profile, the connector runs initialize only: no new
conversation or model prompt. Failed initialize leaves the profile saved and the
bot asks for local inspection. Successful initialize does not prove model access.
The remaining real-machine acceptance matrix is recorded in
[P4.18](../items/agent-onboarding.ru.md#остаток-приёмки).

## Dialogues and recovery

Changing a profile's directory or mode changes its saved-session scope. Existing
dialogues are not moved and may become unavailable for resume. Prefer a **new
profile for another project**, keeping the old profile for its dialogues.
Deleting a profile removes its route, not stored history or files.

Mutations check the reviewed configuration revision and operation identity.
Repeated delivery cannot silently create another profile or overwrite newer
settings. The local wizard shares the same writer locks. If a reply is lost,
refresh the list before deciding whether another change is needed.

Unconfirmed process cleanup prevents saving and keeps admission fenced. Inspect
the connector locally before recovery. No prompts are replayed and no forced
process-tree termination is performed. Storage failures can also require local
inspection/restart; backups retain the previous configuration.

## Windows background execution

The development WSL launcher uses `CREATE_NO_WINDOW` with its existing piped
transport and cleanup supervisor. Dialogues still own their required processes
without opening a visible WSL console. This is separate from attaching to an
existing Terminal or Desktop conversation.
