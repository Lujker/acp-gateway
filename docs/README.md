# Documentation

[ACP Gateway](../README.md) · [Project status](project-status.md) · [Roadmap](../road-map.md)

Start with the installation guide for your deployment. Setup guides explain
how to operate the product; item journals record decisions and verification.
Historical documents are not current installation instructions.

## Install and connect

| Task | Guide |
| --- | --- |
| Install via uv/PyPI: Ubuntu dispatcher + Windows/Hermes | [Step-by-step guide (Russian)](setup/self-install-pypi.ru.md) |
| Use the interactive setup sections | [Setup and CLI menu](setup/interactive.md) |
| Connect a Windows PC | [Windows connector](setup/windows-connector.md) |
| Deploy a native VPS dispatcher with Hermes PCs | [Native deployment (Russian)](setup/vps-native-hermes.ru.md) |
| Use Docker for a dispatcher and Linux/WSL connectors | [VPS runbook](setup/vps-acceptance.md), [Russian](setup/vps-acceptance.ru.md) |
| Run the dispatcher in Docker behind the host's reverse proxy, or move a native dispatcher into Docker | [Docker dispatcher (Russian)](setup/vps-docker.ru.md) |
| Configure autostart | [Services](setup/service.md) |
| Upgrade dispatcher and connectors to 0.2.8 | [Upgrade instructions (Russian)](setup/update-0.2.8.ru.md) |
| Current release 0.2.8 | [Release notes](releases/0.2.8.md), [publication and deployment](releases/0.2.8-publication.md) |
| Connector fixes in 0.2.6 | [Release notes](releases/0.2.6.md), [publication](releases/0.2.6-publication.md) |
| Historical upgrade to 0.2.3 / 0.2.2 | [0.2.3](setup/update-0.2.3.ru.md), [0.2.2](setup/update-0.2.2.ru.md) |
| Updates out of the box (since 0.2.2) | [Normal setup and first start](setup/updates-out-of-box.md) |
| Telegram updates in the 0.2.1 manual opt-in path | [Independent updater setup](setup/remote-updates.md) |
| Update, inspect or recover an installation | [Maintenance](setup/maintenance.md) |

## Use and troubleshoot

| Task | Guide |
| --- | --- |
| Telegram bot, commands and approval controls | [Telegram](setup/telegram.md) |
| Telegram delivery and status recovery | [Delivery and recovery](setup/telegram-recovery.md) |
| CLI and local HTTP/SSE API | [Gateway](setup/gateway.md) |
| Expose agents to Hermes through MCP | [Hermes MCP](setup/hermes.md) |
| Connect existing Hermes Desktop | [Hermes attach](setup/hermes-attach.md) |
| Test the experimental managed Codex CLI adapter | [Codex managed](setup/codex-managed.md) |
| Test the experimental managed Claude Code CLI adapter | [Claude Code managed](setup/claude-code-managed.md) |
| Run a managed Codex/Claude CLI inside explicit WSL from Windows | [Managed agents in WSL](setup/windows-wsl-managed.md) |
| Configure a direct Goose connection | [Work Goose](setup/work-goose.md), [Goose attach](setup/goose-attach.md) |
| Discover agents and manage profiles from Telegram | [Agent onboarding and profiles](setup/telegram-agent-profiles.md) |
| Manage agent tools and host setup | [Host CLI](setup/host-cli.md) |
| Inspect health and logs | [Observability](setup/observability.md) |
| Configure request and history limits | [Rate limits](setup/rate-limits.md), [Retention](setup/retention.md) |

## Develop and verify

- [Development, tests and source builds](development.md).
- [Architecture](architecture.md): design and topology; historical target sections
  should be read together with [current project status](project-status.md).
- [Distribution formats](setup/distribution.md) and [standalone binaries](setup/binary.md).
- [Recorded-mock CLI laboratory](setup/cli-lab.md).
- [Acceptance checklist](setup/p48-acceptance.md),
  [Hermes VPS acceptance (Russian)](setup/vps-hermes-acceptance.ru.md).

## Plan and evidence

- [ACP in the unified AI business (Russian)](items/ai-business-strategy.ru.md):
  practicum, universal AI Assistant + agent and future remote-control reuse; not
  a delivered integration or a replacement for the current engineering queue.

- [Roadmap](../road-map.md): the single status board; the final section is the current queue.
- [Codex/Claude resumption checkpoint (Russian)](plans/p4.13-resumption.ru.md):
  shipped implementation, historical evidence and future native chat list/attach.
- [Work mode for remote development P4.13.1 (Russian)](plans/p4.13-work-mode.ru.md):
  implemented safe/work profiles, native auto mode and cancel/resume; acceptance provisionally closed.
- [Decision journal](../road-notes.md): dated findings and owner decisions.
- [Future AI-agent practicum and business brief (Russian)](items/business-practicum.ru.md):
  approved educational concept, pilot gates and open economics; not a launched
  service, a sales commitment or a change to the implementation queue.
- [Connector-first pairing P4.17 (Russian)](items/connector-pairing.ru.md) and its
  [technical plan (Russian)](plans/p4.17-connector-pairing.ru.md): implemented in 0.2.2; acceptance provisionally closed on 2026-10-27.
- [Updates out of the box P4.15.1 (Russian)](items/updates-out-of-box.ru.md): delivered in 0.2.2; shipped updater acceptance provisionally closed, remaining features tracked in P4.19.
- [Agent onboarding P4.18 (Russian)](items/agent-onboarding.ru.md): shipped discovery; acceptance provisionally closed.
- [Operational improvements P4.19 (Russian)](items/ops-improvements.ru.md): completed ACL/CI fixes and open work.
- [VK channel](items/vk-bot.ru.md) and [Goose alternative](items/goose-alternative.ru.md):
  future requirements, not current runtime support.
- [Telegram implementation and checks (Russian)](items/telegram-ui.ru.md).
- [Planned Telegram usability improvements: P2.4 (Russian)](items/telegram-usability.ru.md).
- [Project review and verified fixes (Russian)](items/project-review-2026-10-14.ru.md).
- [Delivery and update evidence (Russian)](items/p48-delivery-plan.ru.md).
- [Dispatcher/connector updates from Telegram: development stages (Russian)](items/remote-connector-management.ru.md).
- [User/workspace ownership](items/dispatcher-accounts-and-dialogues.ru.md),
  [scenarios](items/scenarios-and-schedules.ru.md),
  [client API/mobile](items/client-api-and-mobile.ru.md),
  [harness adapters](items/connector-harness-adapters.ru.md): requirements and evidence
  in Russian; managed Codex/Claude are shipped, additional integrations remain proposals.

## Historical references

- [Frozen 0.2.0rc1 installation guide (Russian)](setup/self-install-0.2.0rc1.ru.md):
  applies to that artifact only.
- [Original project plan (Russian)](archive/2026-10-06-initial-plan.md).
- [README before the 2026-10-14 cleanup](archive/2026-10-14-readme.md):
  retained for provenance, not for installation.

Keep the root README short. Put operational instructions in `setup/`, current
implementation evidence and future items in `items/`, and superseded material
in `archive/`. Keep roadmap and journal entry points at their established root
paths so existing links and workflows continue to work.
