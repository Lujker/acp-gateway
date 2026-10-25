# Experimental managed Claude Code CLI

[Documentation](../README.md) · [P4.13 scope and evidence](../items/connector-harness-adapters.ru.md)

This is a source-checkout feature, **not part of published ACP Gateway 0.2.2**.
Safe/work profiles, cancellation and scoped persisted resume are implemented in
source. Current scoped checks and independent review are recorded in the
[evidence journal](../items/connector-harness-adapters.ru.md). Live authenticated
Windows/WSL and Telegram acceptance remains a separate next step; synthetic peers
do not establish a production release or Desktop support.

The adapter launches a dedicated native Claude Code `stream-json` child per
conversation and uses the existing AgentClient, connector relay and human approval
path. It does not control Claude Desktop or import existing CLI/Desktop chats.
Running it inside an explicit WSL distribution from a Windows connector is
described in [managed Codex/Claude inside WSL](windows-wsl-managed.md).

## Prerequisites

- Source dispatcher and connector must both understand `kind: claude_code`.
- Install Claude Code separately in the connector's OS. The real-binary
  initialize-only check used **Claude Code 2.1.306 in WSL**. Other versions are
  not established compatible by this check.
- Authenticate the CLI locally through its normal workflow before live testing.
  Gateway setup does not install it, sign in, switch accounts or copy credentials
  to the dispatcher. Initialize success alone does not prove login/model access.
- Use explicit configuration, env and data paths and a disposable project. Live
  acceptance and installation updates follow the owner-approved
  [acceptance plan](../plans/p4.13-work-mode.ru.md#6-живая-приёмка--следующий-пункт-не-этот).
- On Windows, choose the native executable, not the Desktop UI launcher or a
  PowerShell/npm shell wrapper. An absolute `claude.exe` path can be used if PATH
  does not resolve the native executable. Native Windows tests currently use a
  synthetic Python protocol peer, not an installed authenticated Claude binary.

A child receives an allowlisted environment with the local HOME/USERPROFILE and
Claude config location. Arbitrary gateway, Telegram, provider-key and proxy env
variables are not forwarded. In safe, user/project settings sources, hooks, slash commands,
Chrome integration and additional MCP configuration are disabled by launch flags;
this is deliberately narrower than an ordinary interactive CLI configuration.
Managed host policy can still restrict the native CLI. This is **not an OS sandbox**.

## Configure

Run `acpgw setup` or `acpgw agents configure` with explicit test config/env paths.
Choose **claude_code**, an alias (default `claude`), the real project directory,
CLI executable, optional model (`none` keeps the CLI default) and safe/work mode. Existing custom
request/turn timeouts and limits are preserved on edit. Switching agent kinds
removes incompatible transport settings. Routed profiles ask for computer/cwd,
not a local executable or account secret on the dispatcher.

Manual YAML, under `agents` in a complete test config:

```yaml
agents:
  - alias: claude
    title: Claude Code CLI (experimental)
    kind: claude_code
    backend: claude_code
    default_cwd: /home/user/projects/disposable-project
    claude_code:
      executable: /home/user/.local/bin/claude
      mode: safe  # work selects native automatic permissions and user settings
      request_timeout: 30
      turn_timeout: 600
      max_turns: 16
      max_sessions: 8
      # model: an-explicit-model-id
```

Omitting `claude_code` uses `claude` from PATH and the shown defaults. Only a local
executable name or absolute path is accepted, not a shell command, arbitrary flags
or an endpoint. No `session_mode`, bypass mode, persistent permission grants,
remote secret or sandbox option is accepted for this backend. Windows and WSL
paths are different; use the paths of the actual connector OS.

`max_turns` bounds the native agent loop, not the number of user messages in a
conversation. The native CLI's own limit semantics apply. `max_sessions` bounds
owned CLI processes, including a prewarmed initialize-only child; it is not a
limit on discovered disk history. Request timeout: >0 and <=300 seconds; turn
timeout: >0 and <=3600; max_turns: 1..100; max_sessions: 1..32.

```sh
uv run --frozen acpgw --config /path/to/test-config.yaml --env-file /path/to/test.env agents configure
uv run --frozen acpgw --config /path/to/test-config.yaml --env-file /path/to/test.env config check
uv run --frozen acpgw --config /path/to/test-config.yaml --env-file /path/to/test.env doctor
uv run --frozen acpgw --config /path/to/test-config.yaml --env-file /path/to/test.env doctor --live
```

Offline doctor starts no child. The Claude Code part of live doctor performs
initialize only and closes its child: no prompt/model request. Other configured
components have their usual probes; use a Claude-only test config when that is
the intended scope. `agents install`, `agents serve` and `agents probe` do not
become Claude installers/launchers through this feature.

On a connector use the normal enrollment/foreground configuration workflow.
The dispatcher discovers the published `computer/claude` route. An explicit
legacy routed profile uses `backend: connector`, `kind: claude_code`, its
`computer_id` and remote `default_cwd`, **without** a `claude_code` settings block.
Executable/login settings remain on the connector computer.

## Conversations and permissions

Use the existing new-dialogue and prompt workflow in [CLI](gateway.md) or
[Telegram](telegram.md). Each new owned conversation has its own CLI process/native
UUID. Completed turns can continue that same live conversation; another dialogue
does not share its history. Merely listing owned handles does not run a model.

In safe, the native process uses manual permission mode plus explicit ask rules for the
fixed tool surface: Read, Glob, Grep, Edit, Write and Bash. Approved requests use
the exact original input once, or are denied. Native cancellation/revocation,
expired requests, incomplete/foreign data and unsupported background actions do
not authorize a tool. There is no session-wide/always grant. Keep an authorized
human CLI/Telegram approver available; MCP is not a human approval channel.

Manual mode alone normally auto-approves some reads; the explicit ask rules are
intentional. Native inherent termination behavior is not a promise that every
internal action becomes a gateway permission prompt. Gateway cwd policy and
these approvals **do not restrict all filesystem/network access like an OS sandbox**.
Once you approve Bash, that command has the user's host authority; review the full
command and input before approving it.

## Work profile, restart and cancellation

Safe remains the default. `mode: work` selects native `--permission-mode auto`,
default tools (including web tools and subagents), user/project/local settings,
CLAUDE.md, skills, plugins, hooks and MCP. It uses the user's real agent profile;
setup does not create a separate agent home. Hooks and MCP therefore run with
the user's authority when tasks arrive through Telegram. Native auto review is
model judgment, not a sandbox. Remaining permission questions still go through
once-only allow/deny, including additional native tools.

Work defaults to `max_turns: 100` and `turn_timeout: 3600`; safe defaults remain
16 and 600. Explicit values are preserved. Setup can create both profiles with
separate aliases; choose the profile in Telegram. `/mode` controls dispatcher
approvals independently and does not switch the native permission profile.

Both profiles now save native sessions. After a confirmed clean connector restart,
ACP `session/load` launches `--resume` with the original UUID and cwd. Native
history lives in Claude's profile; local gateway bindings live under the connector
data directory's `pins/managed-sessions`. Preserve both. Bindings check profile,
mode, cwd, executable and native home or WSL distribution/user/profile. An OS
lease excludes another gateway owner.

ACP `session/cancel` sends native interrupt. Cancellation requires a successful
interrupt acknowledgement and a terminal result; an acknowledgement alone does
not prove the turn stopped. Already completed actions are not rolled back.

## Loss, lifecycle and missing features

- **Unknown work stays fenced:** attached and busy state is persisted before
  native resume/prompt. Only confirmed cleanup makes an idle chat resumable;
  crash, timeout or unconfirmed cleanup blocks automatic resume after restart.
  No uncertain prompt is resent and no substitute conversation is created.
  Inspect actual work before explicitly starting a different dialogue.
- Transport cleanup closes owned processes and pipes, not proof that every
  descendant stopped or an action was undone.
  Uncertain work blocks safe maintenance even after connection loss. Other
  people's CLI/Desktop/Git processes are not terminated as a tree.
- **No native chat catalogue or attach-by-ID yet:** Telegram `/sessions` and
  `/switch` operate on dispatcher dialogues. The adapter's owned-session list
  is also not a list of existing native chats. The planned next scope is Telegram
  computer -> connector agent -> native chat list -> selected ID, with access and
  ownership checks and backend-specific resume/shared-control contracts.
- **No Desktop shared writer or native model catalogue** is provided. Work mode
  enables the user's normal tools/settings; simultaneous CLI/Desktop and Telegram
  writers to one native chat remain outside this substage.
- Synthetic native subprocess tests and initialize-only smoke do not establish
  live model, approval, Telegram or complete upstream-helper lifecycle acceptance.
  The separate known Codex Windows Git-helper lock is not fixed by adding Claude.

## Model-free tests

```sh
uv run --frozen pytest tests/unit/test_claude_code_config.py tests/unit/test_claude_code_cli.py tests/unit/test_claude_code_wiring.py tests/integration/test_claude_code_managed.py tests/integration/test_claude_code_frontends.py
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
```

The subprocess fake uses real JSONL pipes and a real Python child on both POSIX
and Windows. Tests include separate histories, sequential turns, once/deny,
permission revocation, malformed/oversized frames, foreign/subagent messages,
closed/uncertain session fencing, maintenance capacity and owned-pipe cleanup.
They deliberately make no model calls. See the item journal for executed counts,
review findings and later acceptance evidence rather than inferring readiness
from this test command.
