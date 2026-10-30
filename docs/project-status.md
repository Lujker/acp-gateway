# Project status

[Documentation](README.md) · [Roadmap and current queue](../road-map.md)

Snapshot: **2026-10-27**. Stable was **0.2.8**, published to GitHub, PyPI and GHCR. The architecture direction changed on **2026-10-29**; deployments were not checked during that discussion. [Owner decisions](plans/p4.20-paperclip-direction.ru.md).

## Versions and deployment

| Component | State and evidence |
| --- | --- |
| Dispatcher | Docker 0.2.8 by immutable GHCR digest; backup and health checks recorded in the [publication journal](releases/0.2.8-publication.md) |
| home connector | 0.2.8, editable Git/Python venv; reconnected after update, per the publication journal |
| work connector | Owner reports a successful update to 0.2.8 on 2026-10-27; method and live connection were not independently checked |

Both ends need 0.2.8 for stream recovery and interrupted Claude dialogue resume.
Ordinary upgrades preserve pairing and stored data. [Upgrade guide](setup/update-0.2.8.ru.md).

## Available paths

- Dispatcher and outgoing PC connectors; stored dialogues/jobs, results and approvals.
- Hermes Desktop dedicated conversations; basic Telegram use on both Windows PCs confirmed by the owner.
- Experimental managed Codex and Claude Code: safe/work profiles, Windows→WSL,
  cancellation and scoped persisted resume. The owner confirmed WSL use and Telegram profile edits.
- Telegram computer/job cards, agent/dialogue/model controls, approvals,
  recent/favorite dialogues, automatic titles, work updates and multipart answers.
- Connector-owned CLI discovery and agent profile creation from Telegram.
  Executable paths and accounts remain local. [Guide](setup/telegram-agent-profiles.md).
- CLI, local HTTP/SSE and MCP; MCP has no human approval controls.

0.2.8 keeps streams across a short computer-link drop (up to five minutes, within
bounded replay buffers). Claude-reported API errors fail only the turn; interrupted
Claude dialogues can be explicitly resumed after confirmed process cleanup, without
replaying the request. Codex retains its strict interruption fence. Large native
output lines no longer hit the old 256 KiB reader limit. [Release notes](releases/0.2.8.md).

## Acceptance decision and verification

On **2026-10-27** the owner provisionally closed outstanding acceptance of shipped
managed-agent, onboarding, pairing, updater and recovery behaviour. These checks
are removed from the active queue; reopen only on concrete problems. This decision
adds no live test evidence and does not mark unimplemented features as delivered.
Earlier checklists remain historical references, not instructions to restart testing.

The release records **2319 passed / 229 skipped** locally and successful
Linux/Windows/macOS/ARM/Docker CI; one maintenance-IPC race required a rerun.
During the status review, 128 targeted tests passed and Ruff check/format were clean.
The review's full local run was stopped before completion. Historical native Windows
baseline failures remain recorded; revisit when they cause a concrete problem.
No new deployed model/drop/resume scenarios were run during this documentation update.

## Next development

1. **Finish and verify P4.19 first.** Consult the actual Git state and [checkpoint](plans/p4.19-progress.ru.md); release and deployment require separate authorization. Previously closed acceptance is not automatically reopened.
2. After P4.19, research **P4.20**: ACP Gateway remains independent, with optional Paperclip integration and its SSH execution path. The first proposed vertical slice observes Home-wsl from Telegram without changing SSH. See the [owner decisions and boundaries](plans/p4.20-paperclip-direction.ru.md).
3. Direct ACP sessions and prior planned improvements remain; their later priority is not yet decided. Channels become connectors and PC programs become nodes, with compatibility and migration to be designed.

The [roadmap queue](../road-map.md#9-очередь-дальнейшей-разработки) is canonical.

## Process lifetime and limits

Switching agent or dialogue changes where subsequent messages go. It does not
cancel the previous job or close its native process. Claude uses a dedicated
child per dialogue; Codex uses an app-server process per transport, which can
host multiple dialogues. Idle processes can remain alive, including inside WSL;
there is no idle timeout today. `running_sessions: 0` counts active turns, not
resident processes. [User-facing explanation](setup/telegram.md#switching-and-background-processes).

Registry updates require a supported uv install. Docker replacement needs a host
companion and an allowed immutable image; editable Git updates remain manual.
Native chat import/shared Desktop attachment, VK and multi-user are not implemented.
OS actions/modules and schedules remain deferred. Hermes cold resume has limits.

Agents execute on their own computers. The dispatcher stores dialogue/job data and
routes messages, results and approvals. Credentials are redacted. Native agent
permissions are not an OS sandbox. [Architecture](architecture.md).
