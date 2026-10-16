# Project status

[Documentation](README.md) · [Roadmap and current queue](../road-map.md)

This is a short orientation for readers, not a replacement for the roadmap
or acceptance journals. Snapshot: **2026-10-16**.

## Versions

| Track | State |
| --- | --- |
| `0.2.1` | Telegram update operations, independent updater lifecycle, watcher/env fixes, context and recovery controls; [release notes](releases/0.2.1.md), [upgrade](setup/update-0.2.1.ru.md) |
| Previous stable `0.2.0` | Install through uv/PyPI; independent setup sections, Telegram computer/job dashboards, controls and approvals, dialogue execution modes, managed update with service recovery, Docker dispatcher behind a host reverse proxy |
| Previous candidates `0.2.0rc1`–`0.2.0rc8` | Published prereleases; upgrade with `acpgw update --stable --with-services` |

A production dispatcher runs in Docker behind a host nginx with two Windows/Hermes
connectors (owner acceptance 2026-10-15). See
[installation](setup/self-install-pypi.ru.md),
[delivery evidence](items/p48-delivery-plan.ru.md) and
[review findings/fixes](items/project-review-2026-10-14.ru.md).

## Available paths

- **Dispatcher + connectors:** one dispatcher, enrolled outgoing connections
  from PCs, discovered agent routes and per-chat dialogues/jobs.
- **Hermes Desktop:** dedicated managed conversations through a local connector.
  The owner has confirmed basic Telegram prompts/results on two Windows PCs.
- **Telegram:** private user allowlist, command menu, agent/dialogue/model controls,
  human approval buttons, formatted answers and restart-aware delivery.
  Stable 0.2.0 includes computer cards and scoped task history. Recovery, renaming,
  context cards, recent dialogues and favorites are published in main `d42c430`.
  The owner reports a successful Docker VPS update and connected agents; development
  is closed; remaining live acceptance was provisionally closed by the owner
  on 2026-10-16, to reopen on reported problems. See
  the [Telegram journal](items/telegram-recovery.ru.md).
- **CLI, local HTTP/SSE API and MCP:** additional interfaces to the same core.
  MCP does not receive human approval controls.
- **Direct Goose:** ACP connection, TLS pinning, sessions and approvals;
  deployment instructions and historical checks have their own guides.

## Current work and limits

The owner provisionally closed outstanding Telegram and operational acceptance
on 2026-10-16. Reopen those checks only when the owner or another user reports a
problem. This decision does not claim that all previously listed scenarios passed.
The macOS connector will be checked by the owner later and does not block current work.

Connector activity watcher recovery and explicit service reinstallation guidance
with saved env selection are implemented in source. Telegram updates now include
single-owner exact-version confirmation, durable operations, status/history and
restart-aware notifications. An independent, locally enabled companion executes
uv managed service updates or a Docker dispatcher image replacement with database
backup and known-failure recovery. See [companion setup](setup/remote-updates.md).
The companion now has separate Linux user-service/Windows logon-task installation,
autostart and cooperative stop controls, available from setup. Local diagnosis,
audited resolution of retained unknown executor records and immutable Docker-image
registration are implemented in source; image download/build remains external.
A local development snapshot `0.2.1.dev20261016` was built offline with source/artifact
hashes and checked wheel contents. It is unpublished and does not change stable 0.2.0.
These features are included in 0.2.1; the owner authorized its release on 2026-10-16.
Production updates remain the owner's next action. User modules remain deferred.

- Hermes managed conversations still have cold-resume limitations. Shared Desktop
  observation does not grant shared write/cancel/approval authority.
- Actions depend on reported agent capabilities and gateway policy.
- Telegram update MVP is implemented in source for one target and newer stable
  major.minor.patch releases. Native updates require a registry uv installation;
  Docker requires a prepared immutable image allowed by host policy. Live Docker
  engine/native Windows update evidence is not claimed. macOS, batch, Git/file
  installs and RC versions are outside this MVP. Version 0.2.0 lacks the feature.
- User modules and connector-owned tasks/schedules remain deferred proposals.
- Multi-user/workspace ownership, public client API/mobile and additional adapters
  remain [planned work](../road-map.md).

## Control and data boundaries

Agents execute on their own computers. The dispatcher routes messages, retains
conversation/job metadata and results, and records approval decisions. Registered
credentials are redacted. Human decisions are offered only through authorized
human channels; an explicit per-dialogue automatic mode is separately auditable.
The owner API is authenticated and loopback by default.

See [architecture](architecture.md) for the design and the linked item journals
for evidence. Some architecture sections retain earlier target descriptions;
use the current roadmap and acceptance records for implementation status.
