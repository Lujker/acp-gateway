# Project status

[Documentation](README.md) · [Roadmap and current queue](../road-map.md)

This is a short orientation for readers, not a replacement for the roadmap
or acceptance journals. Planning snapshot: **2026-10-21**; stable release and source are **0.2.2**.

## Versions

| Track | State |
| --- | --- |
| Stable `0.2.2` | Connector-first pairing, out-of-box updater, CLI artifact gate and recovery fixes; published GitHub/PyPI/GHCR; automated suite 1734 passed/38 skipped; [publication](releases/0.2.2-publication.md), [release notes](releases/0.2.2.md), [upgrade](setup/update-0.2.2.ru.md) |
| Previous stable `0.2.1` | Telegram updates, independent updater lifecycle, watcher/env fixes, context/recovery controls; [publication](releases/0.2.1-publication.md), [upgrade](setup/update-0.2.1.ru.md) |
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

**Owner decision, 2026-10-23: paused; documentation only.** P4.13 remains PARTIAL.
The next implementation substage, only after an explicit request to resume, is
P4.13.1 safe/work profiles with cancel and persisted resume (OPEN, not started).
Targeted main comparison is accepted as sufficient to proceed; incomplete full-main
baseline and remaining Windows/interop issues stay documented debt, not a gate.
No further baseline runs, implementation, live acceptance or installation changes
are authorized during this pause. See the
[current checkpoint](plans/p4.13-resumption.ru.md#owner-directive-2026-10-23).
The test counts below are historical, not a current all-green suite.

**P4.13 is PARTIAL (source only):** managed Codex CLI/App Server and Claude Code
CLI adapters, configuration/setup/doctor and connector relay wiring are implemented.
They are not in published 0.2.2. The final post-review-fix WSL suite passed
**1985 tests, 42 skipped**; native Windows Claude config/setup/bridge checks passed
**128 tests** with a synthetic subprocess peer. Independent re-review accepted
both Claude fixes; this is not live model/Telegram/Desktop acceptance.
Both paths use dedicated new dialogues and once/deny approvals. Claude has one
native child per conversation, explicit ask rules, no persisted native history,
no cold resume and no safe native-turn cancellation; manual permissions are not
an OS sandbox. Windows subprocess peer tests and real WSL Claude initialize-only
smoke do not establish live authenticated Windows/model/Telegram support.
Per the owner's decision, joint live Codex/Claude acceptance on the Windows host
and WSL is deferred until after this implementation. The known short-lived Codex
startup `git.exe` directory lock remains open. No Desktop shared attach, native
chat catalogue/import or Telegram native attach-by-ID is implemented.
See [Codex configuration](setup/codex-managed.md),
[Claude Code configuration](setup/claude-code-managed.md) and
[implementation evidence](items/connector-harness-adapters.ru.md).
For resuming the committed but unreleased source work, use the
[handoff and acceptance plan](plans/p4.13-resumption.ru.md); documentation recording
does not start live acceptance or change production.

**Windows-host connector → explicit WSL is implemented in unreleased source for
new managed chats.** Configuration/setup/doctor and owned Linux supervision are
wired; independent review of S1/L1/L2/L3 fixes was accepted on 2026-10-22.
Parent real installed-CLI initialize succeeded for both backends (exit 0, no
cleanup uncertainty, no prompts); Windows→WSL focused tests passed 135/34 skipped.
Final post-format full WSL suite passed **2148 tests,48 skipped**; this
source-checkout managed-launch substage is complete. The broader native
Windows unit run is **not green**. On 2026-10-23 the owner authorized local
full Windows runs without the strict isolation runner. The later main/branch
comparison is partial; its accepted targeted results and unresolved debt are
recorded in the current checkpoint. More baseline work is not the next step.
Stop and report if a test actually mutates
services or Task Scheduler tasks. See the
[current owner directive](plans/p4.13-resumption.ru.md#owner-directive-2026-10-23). No native catalogue/ID/live Desktop attachment, authenticated
model or Telegram acceptance follows from these checks. See the
[WSL guide](setup/windows-wsl-managed.md),
[current checkpoint](plans/p4.13-resumption.ru.md#windows-wsl-current-checkpoint)
and [evidence journal](items/connector-harness-adapters.ru.md).


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
These features are included in stable 0.2.1, released with the owner's authorization
on 2026-10-16. Historical development snapshots do not supersede that release.
Production installation state was not rechecked or changed by the planning interview.

[P4.17 connector-first pairing](items/connector-pairing.ru.md) and
[P4.15.1 updates out of the box](items/updates-out-of-box.ru.md) are implemented
in the 0.2.2 source and pass the automated suite. Live native service update,
Docker replacement, Telegram round trips and owner smoke remain acceptance checks.
Existing 0.2.1 computers keep their enrollment; the one-time legacy re-pairing
transition is explicit and is not activated by installing 0.2.2.

- Hermes managed conversations still have cold-resume limitations. Shared Desktop
  observation does not grant shared write/cancel/approval authority.
- Actions depend on reported agent capabilities and gateway policy.
- Telegram update MVP is implemented in source for one target and newer stable
  major.minor.patch releases. Native updates require a registry uv installation;
  Docker requires a prepared immutable image allowed by host policy. Live Docker
  engine/native Windows update evidence is not claimed. macOS, batch, Git/file
  installs and RC versions are outside this MVP. Version 0.2.0 lacks the feature.
- Built-in basic OS/agent actions plus user scripts/modules, and connector-owned
  tasks/schedules, remain deferred. No shutdown/restart actions were added.
- Current order (owner decision 2026-10-23): Windows fixes and main comparison,
  safe/work profiles with cancel/resume, live acceptance and release, then native
  chat listing/stored resume (4a), then research into live shared attachment (4b).
  4a can ship without 4b. The later choice is minimal [VK](items/vk-bot.ru.md)
  versus multi-user; [Goose parity](items/goose-alternative.ru.md) is CONDITIONAL.
  See the [canonical queue](../road-map.md#9-очередь-дальнейшей-разработки).
  CLI/Desktop/platform coverage remains a target, not verified support.
- Public client API/mobile and additional adapters remain [planned work](../road-map.md).

## Control and data boundaries

Agents execute on their own computers. The dispatcher routes messages, retains
conversation/job metadata and results, and records approval decisions. Registered
credentials are redacted. Human decisions are offered only through authorized
human channels; an explicit per-dialogue automatic mode is separately auditable.
The owner API is authenticated and loopback by default.

See [architecture](architecture.md) for the design and the linked item journals
for evidence. Some architecture sections retain earlier target descriptions;
use the current roadmap and acceptance records for implementation status.
