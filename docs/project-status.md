# Project status

[Documentation](README.md) · [Roadmap and current queue](../road-map.md)

Snapshot: **2026-10-26**, through commit `aa691b7`. Stable is **0.2.7**.
Deployment state below is from publication records, not a new inspection of
running installations.

## Versions and deployment

| Track | State |
| --- | --- |
| Stable `0.2.7` | Published GitHub/PyPI/GHCR. Prompt-based dialogue names and maintenance test cancellation/retry fixes. [Notes](releases/0.2.7.md), [publication](releases/0.2.7-publication.md), [upgrade](setup/update-0.2.7.ru.md) |
| Connector baseline `0.2.6` | Claude Code text after thinking and final answers retained; turn limits complete the turn instead of breaking the connection; clearer discovery hints and tolerant WSL output parsing. [Notes](releases/0.2.6.md), [publication](releases/0.2.6-publication.md) |
| Earlier stable `0.2.3` | Managed Codex/Claude safe/work, cancellation, scoped persisted resume, Telegram profile management and presentation. [Notes](releases/0.2.3.md), [publication](releases/0.2.3-publication.md) |
| Earlier stable `0.2.2` | Connector-first pairing and out-of-box updater. [Notes](releases/0.2.2.md), [publication](releases/0.2.2-publication.md) |

The owner's Docker dispatcher was switched to the official **0.2.7 GHCR image by
digest**, with an integrity-checked SQLite snapshot (schema 10). Health,
Telegram/MCP and both PC connections were verified after deployment. Both
connectors remain **0.2.6**; 0.2.7 requires only a dispatcher update. The home
connector uses editable Git/Python venv on `main`; registry updates are disabled
for that installation. See the [latest deployment record](releases/0.2.7-publication.md).

## Available paths

- **Dispatcher + connectors:** outgoing PC connections, discovered agent routes,
  per-chat dialogues/jobs, retained results and approvals.
- **Hermes Desktop:** dedicated managed conversations through local connectors;
  the owner confirmed basic Telegram prompts/results on two Windows PCs.
- **Experimental managed Codex and Claude Code:** safe/work profiles, Windows→WSL,
  native cancellation and scoped persisted resume. The owner confirmed WSL use
  and Telegram profile edits; the full work/cancel/resume/approval matrix remains
  open. [Codex](setup/codex-managed.md), [Claude Code](setup/claude-code-managed.md),
  [WSL](setup/windows-wsl-managed.md), [acceptance plan](plans/p4.13-work-mode.ru.md).
- **Telegram:** computer/job cards, agent/dialogue/model controls, human approvals,
  recovery, renaming, recent/favorite dialogues, work updates and multipart results.
  Untitled dialogues get a title from the first non-empty prompt line (redacted,
  up to 60 characters) on their next message; an agent title can replace it.
- **Agent onboarding (P4.18):** the connector discovers installed Codex/Claude CLI
  candidates on Windows/WSL, Linux and macOS. Telegram can add the first managed
  profile without an existing template. Executable/account settings remain local; the dispatcher
  selects an opaque candidate ID. Connection setup asks for permission (default
  yes); existing PCs can grant it with `acpgw agents allow-remote-profiles`.
  [Guide](setup/telegram-agent-profiles.md), [remaining checks](items/agent-onboarding.ru.md).
- **CLI, local HTTP/SSE and MCP:** interfaces to the same core; MCP receives no
  human approval controls. Direct Goose supports ACP, TLS pinning and approvals;
  parity with Hermes remains conditional future work.

## Recent fixes and verification

- `99cc9ba` adds discovery, onboarding, the local permission command and background
  ACL rechecks outside heartbeat publication. `e732a4b` measures ACL validity from
  verification start; failed or expired verification stops writes.
- `4ec9f83` restores explicitly empty Claude dialogues with their original ID
  without requiring a native transcript. Used/legacy dialogues still require
  native history; unknown outcomes and unconfirmed cleanup still block resume.
- `49aa93c` preserves text across Claude thinking/tool frames and separates work
  updates from final answers. `481a8f3` improves discovery diagnostics.
- `7fbb065` adds dialogue titles; `64425d6` fixes maintenance test hangs/flakes,
  with no connector runtime change in 0.2.7.

The 0.2.7 publication records **2300 passed / 229 skipped** locally in WSL and
clean Ruff checks. Its main CI run passed full pytest, platform/binary/connector
smokes, native Windows, both macOS runners, Linux ARM and Docker image publication.
These are recorded release results, not tests rerun for this documentation update.
Successful release jobs do not close historical full native Windows baseline debt
or establish live model acceptance.

## Remaining work and limits

- **P4.18: PARTIAL.** Implementation is shipped; complete the real-machine
  onboarding matrix, including a real Mac and older connector compatibility.
  Initialize-only success does not prove CLI authentication/model access.
- **P4.19: PARTIAL.** Heartbeat/ACL fix shipped; a successful Telegram updater
  round trip still needs evidence. Visible message target, PC diagnostics,
  sanitized refusal reasons, Docker host companion setup, Git update policy,
  idle-process investigation and release automation remain open. The narrow
  maintenance CI hang is fixed; broader native Windows test debt remains.
- **P4.13: PARTIAL.** Complete work/tool/cancel/resume and safe allow/deny acceptance
  with an agreed model budget. Native chat catalogue/import (4a) and live shared
  CLI/Desktop attachment (4b, research then decision) are separate future stages.
- **P4.17/P4.15.1: PARTIAL.** Pairing/updater source is delivered. Keep outstanding
  native/Telegram/reboot/logon scenarios open. Manual Docker replacement is proven;
  replacement from Telegram with a host companion is not established.
- Hermes cold resume has limits. Shared Desktop observation gives no write,
  cancel or approval authority. No automatic prompt replay after unknown outcomes.
- Registry updates require a supported uv installation; Docker replacement needs
  a host companion and an allowed immutable image. Git installs and RC updates
  are outside the remote update MVP. See [updater setup](setup/remote-updates.md).
- Basic OS actions/modules and schedules remain deferred. VK versus multi-user
  is an owner decision after P4.13; public client API/mobile remains future work.

The owner provisionally closed older Telegram/operational acceptance on
2026-10-16; reopen those scenarios on reported problems. This does not close the
new managed-agent/onboarding/updater checks above. The
[roadmap queue](../road-map.md#9-очередь-дальнейшей-разработки) is canonical.

## Control and data boundaries

Agents execute on their own computers. The dispatcher retains dialogue/job data,
routes messages and records approval decisions. Credentials are redacted. Human
approvals use authorized human channels; per-dialogue automatic mode is auditable.
The owner API is authenticated and loopback by default. Native agent permissions
are not an OS sandbox. See [architecture](architecture.md) and item journals for
the design and evidence.
