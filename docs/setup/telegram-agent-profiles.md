# Manage connector agent profiles from Telegram

Available in the development branch after 0.2.3rc1. Both dispatcher and connector
must be updated; published 0.2.3rc1 does not contain this feature.

An owner can add, edit and remove **Codex and Claude Code profiles** on a connected
computer. New profiles copy an existing managed profile's local executable,
Windows/WSL environment and account configuration. Installing CLI tools, signing
in, changing executable paths, and managing Hermes/remote ACP profiles remain
local operations.

## Local permission

While the connector is stopped, explicitly enable this in its existing config:

```yaml
connector:
  remote_profiles: true
```

Restart the connector. Remote administration is disabled by default. The setting
trusts the authenticated dispatcher to change project directories and safe/work
modes, separately from ordinary ACP execution permissions. Setting it back to
false locally refuses further profile requests; restart after local config edits
before using remote administration again.

## Use the bot

Open **Computers → computer → Configure profiles**, or `/profiles COMPUTER_ID`.
The user must be an authorized pairing owner and allowed in the bot's private
chat configuration. Management actions are not model tools.

- Select an existing profile to edit its title, directory or mode, or remove it.
- Add a profile by choosing an existing managed profile as a template and entering
  a unique alias; then set the project directory and mode.
- Directories must already exist. Use Linux paths for WSL and Windows paths for
  native Windows agents. The connector checks the directory without starting the
  agent, hooks or model.
- Review and confirm. `/cancel` cancels pending text entry. Forms expire after
  two minutes; reopen the list if the form expires or configuration changes.

Active turns, pending requests/approvals, unknown outcomes, unsupported local
backends or another maintenance reservation block changes. Tasks are not
force-stopped. Successful changes update YAML, local policies and the dispatcher
catalog without restarting the whole connector service.

The previous config is backed up privately under
`setup-backups/profiles/OPERATION_ID.yaml`. Enrollment, `.env`, CLI credentials,
native history and project files are preserved. Removing the last agent is not
supported by the current registration protocol.

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
