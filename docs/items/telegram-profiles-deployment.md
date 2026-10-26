# Owner deployment: Telegram profiles and background WSL

Deployed on **2026-10-26**, with explicit owner approval, without model calls.
Source: **44ca89e4bd86b376077d0d9585fa28e1bda6e247**.
This is a development deployment after public 0.2.3rc1; the package version still
reports `0.2.3rc1`. No public artifacts, release tags, stable or main were changed.

**Current state (2026-10-26, after stable 0.2.3):** dispatcher `acpgw:dev-b37e91a`,
home Windows connector on the Git checkout with the 0.2.3 busy-cursor fix, work PC
on 0.2.3 — see the [0.2.3 publication record](../releases/0.2.3-publication.md).
The sections below record the earlier `44ca89e` and `0158dea` deployments.

## Dispatcher

- Only `acpgw-gateway-1` was recreated with local image `acpgw:dev-44ca89e`.
- Running image ID:
  `sha256:c7f2aba5f71d9c7e76357d45404207d33d2328d8f31a7d353a533d3df194dac6`.
- OCI revision label matches the source above. The build used the repository
  Dockerfile and frozen lock. Transferred git archive SHA-256 matched at both ends:
  `621be89931e4752b54450e7221393bfdd028647822123bed8cfdfd50acd31924`.
- Existing config/data mounts, user, network and hardening were retained.
- Health: **healthy**; Telegram and MCP connected; both `home` and `work` reconnected.
- Database integrity: **ok**, schema **10 unchanged**; **9 dialogues, 2 jobs,
  2 computers** preserved. No new job or dialogue was created by the probes.
- SQLite backup: `state/update-snapshots/deploy-44ca89e/gateway.db`, private.
- Previous image override: `compose.release.yaml.before-44ca89e`; the public rc1
  image remains available locally. Rollback was not exercised.

## Windows connector

- Checkout `C:\Users\vladz\source\repos\acp-gateway` fast-forwarded to the same
  commit while stopped. Task still runs its `.venv\Scripts\pythonw.exe` with an
  ordinary Python 3.12 base, independently of uv.
- `connector.remote_profiles: true` enabled with owner approval. All five aliases
  remain: `hermes`, `codex_safe`, `codex_work`, `claude_safe`, `claude_work`.
- Private config/env/key/setup, stopped data and task definition backups:
  `C:\Users\vladz\AppData\Local\acpgw-connector\setup-backups\deploy-44ca89e`.
- `.env`, `home.key`, `setup.json` verified byte-identical after config update.
- Shutdown was graceful, without force; the owned task is enabled/running.
- Pre-upgrade activity file was unavailable; dispatcher had zero jobs/approvals/
  model operations and zero streams. Three durable `attached` bindings were
  preserved without resetting their safety fences. They are not certified idle
  sessions and are not automatically resumed by deployment.
- The new activity file is fresh, running/idle, with zero pending requests,
  approvals, running/uncertain sessions and reservations. Existing stored fences
  remain independent of these current-runtime counters.

## Model-free checks

The actual dispatcher→Windows→WSL routes initialized both **codex_safe** and
**claude_safe**, reporting `connected: true`, `error: null`. No session/new, prompt,
work hooks or model requests were sent. Telegram Bot API confirmed `profiles`
among the 25 registered Russian private-chat commands, without sending messages
or competing with the bot's update polling.

Dispatcher maintenance reservations were released; own obsolete pre-restart file
was removed. Profile CRUD and command forms have automated Windows/WSL evidence in
the [implementation journal](connector-harness-adapters.ru.md); actual owner bot
interaction remains a manual acceptance step. Open `/profiles home`, or
**Computers → home → Configure profiles**. See the
[profile guide](../setup/telegram-agent-profiles.md).

The WSL window suppression is installed. Model-free native transport/cleanup
checks passed before deployment; a visual check on the owner's desktop and live
model/tool/cancel/resume acceptance remain separate checks.

## Profile deadline hotfix — 2026-10-26

The owner reported permanent `stale` on opening profiles. Disk bytes matched the
deployed config and disk/runtime profiles were equal. Measured Windows clock lag
was about 6.4 seconds behind the dispatcher. A freshly created 120-second sender
deadline therefore exceeded the connector's strict `now + 120` upper bound.

The sender now uses `min(form expiry, now + 30)`, retaining the two-minute form
expiry and all receiver/owner/epoch/revision/operation checks. Read-only probes
using the actual host clocks and installed Windows config returned **stale** with
the old deadline, **ok / 4 managed profiles** with the new one. No model was used.
Scoped WSL checks: **34 passed**; native Windows: **30 passed**. One independent
review found no blockers.

Only the dispatcher was recreated with `acpgw:dev-0158dea`; source label matches
the full commit above. Image ID:
`sha256:a35e7027cfc16e9ec40c6bc3948c64e5951b6221d0ed24bc4c322efc83ce8181`.
The Windows service/config was not changed. Private SQLite backup:
`state/update-snapshots/deploy-0158dea/gateway.db`; previous override:
`compose.release.yaml.before-0158dea`. Schema 10 and the existing records were
preserved. Open a fresh `/profiles home` after the bot restart.

## Owner-reported acceptance — 2026-10-26

The owner confirmed Codex and Claude usage in WSL, deleting one agent profile,
and changing the other profiles' working directories. Native stop/resume and
safe allow/deny were not separately confirmed. Follow-up UI observations: a long
Claude answer was split across two Telegram messages (supported behavior), and
Codex intermediate text appeared merged into its answer. Separating phases and
improving multipart presentation remain proposed changes, not deployed fixes.
