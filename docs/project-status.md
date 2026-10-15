# Project status

[Documentation](README.md) · [Roadmap and current queue](../road-map.md)

This is a short orientation for readers, not a replacement for the roadmap
or acceptance journals. Snapshot: **2026-10-15**.

## Versions

| Track | State |
| --- | --- |
| Stable `0.2.0` | Install through uv/PyPI; independent setup sections, Telegram computer/job dashboards, controls and approvals, dialogue execution modes, managed update with service recovery, Docker dispatcher behind a host reverse proxy |
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
  Stable 0.2.0 includes computer cards and scoped task history. Current branch
  improvements to recovery, renaming and error hints remain unpublished; see
  the [Telegram journal](items/telegram-recovery.ru.md).
- **CLI, local HTTP/SSE API and MCP:** additional interfaces to the same core.
  MCP does not receive human approval controls.
- **Direct Goose:** ACP connection, TLS pinning, sessions and approvals;
  deployment instructions and historical checks have their own guides.

## Limits and acceptance still ahead

Acceptance remains incomplete after the 0.2.0 publication. Existing two-PC success
is not proof of every disconnect, concurrent dialogue or reboot scenario.

- Dashboard and current Telegram improvements still need live owner acceptance.
- Managed Hermes conversations do not gain cold resume merely because a
  connector restarted. Shared Desktop observation does not imply shared write,
  cancellation or approval authority; see the [Hermes adapter evidence](items/hermes-shared-control.ru.md).
- Cancellation, model changes and other actions depend on the selected
  agent's reported capabilities and the gateway policy.
- Windows reboot/logon/sleep/network and selected update/rollback cases remain
  in the [acceptance queue](setup/p48-acceptance.md).
- Remote connector updates and host controls, multi-user/workspace ownership,
  schedules, a public client API/mobile and additional harness adapters are
  [planned work](../road-map.md), not current features.

## Control and data boundaries

Agents execute on their own computers. The dispatcher routes messages, retains
conversation/job metadata and results, and records approval decisions. Registered
credentials are redacted. Human decisions are offered only through authorized
human channels; an explicit per-dialogue automatic mode is separately auditable.
The owner API is authenticated and loopback by default.

See [architecture](architecture.md) for the design and the linked item journals
for evidence. Some architecture sections retain earlier target descriptions;
use the current roadmap and acceptance records for implementation status.
