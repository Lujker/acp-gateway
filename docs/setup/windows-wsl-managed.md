# Managed Codex / Claude Code inside WSL from a Windows connector

[Documentation](../README.md) · [P4.13 scope and evidence](../items/connector-harness-adapters.ru.md)

This is a **source-checkout** capability, not part of published ACP Gateway 0.2.2.

**Independent source review accepted (2026-10-22):** no reproducible remaining
S1/L1/L2/L3 defect was found in the revised BEGIN/CLEAN and final-drain mechanism.
This does not establish a sandbox, authenticated model/channel acceptance, or an
overall green Windows suite. See the item journal for evidence and limits.

It lets one Windows connector start managed Codex CLI or Claude Code CLI
conversations in an explicitly selected WSL distribution and Linux user, instead
of requiring a second gateway/connector installed inside WSL.

Safe/work profiles and resume of this connector profile's own persisted chats are
implemented in source; see the backend guides. Native history and gateway binding
must both remain available. Unknown work or unconfirmed WSL cleanup fences resume.

It does **not** provide native chat catalogue, import of arbitrary native IDs, attachment to
an already open CLI/Desktop conversation, or Desktop shared control. Those remain
separate, unproven scopes.

## What "explicit environment" means

The execution identity is: physical computer → execution environment
(distribution + Linux user + profile/home) → agent installation/executable →
native runtime/chat. The UI location does not decide where the agent runs: a
Windows Desktop app can drive a WSL runtime, and a Windows connector can launch a
WSL agent — but neither is proven by the other.

Select it per profile. Manual YAML, replacing the example paths with real ones:

```yaml
agents:
  - alias: codex-wsl
    title: Codex CLI in WSL
    kind: codex
    backend: codex
    default_cwd: /home/user/projects/test-project
    codex:
      executable: /home/user/.local/bin/codex
      sandbox: read-only
      wsl:
        distribution: Ubuntu-22.04
        user: user                 # non-root
        python: /usr/bin/python3   # target-side supervisor interpreter
        home: /home/user           # optional Linux HOME override
        agent_home: /home/user/.codex-test   # optional CODEX_HOME
        path: [/home/user/.local/bin]
```

For Claude Code the block is the same but nested under `claude_code` (`agent_home`
becomes `CLAUDE_CONFIG_DIR`). Omitting `wsl` keeps the native behaviour unchanged.

Field semantics:

- `distribution` and `user` are literal; the values are never trimmed or
  normalized into a different distro/user. Root and option-like values are
  rejected.
- `python` is the Linux interpreter that runs the gateway's embedded supervisor.
  It must exist in the target; the gateway never installs packages there. The
  supervisor targets the distro's own Python (3.10 on the verified Ubuntu-22.04),
  which is separate from the gateway's own interpreter.
- `home` overrides Linux `HOME` for the launched CLI; when omitted, the selected
  user's home from the target is used. `agent_home` only sets `CODEX_HOME` /
  `CLAUDE_CONFIG_DIR`, so a disposable profile can be used without touching an
  existing login.
- `path` is the Linux `PATH` for the child. Keep it short and explicit; the target
  home's `.local/bin` is included, nothing from the Windows environment is
  forwarded.
- `executable` and `default_cwd` must be **Linux** paths when `wsl` is set. They
  are validated for syntax at configuration time and used as-is at runtime; the
  Windows host does not translate, stat or `which` them.

`acpgw setup` / `acpgw agents configure` offer `codex-wsl` and `claude_code-wsl`
choices that ask for exactly these fields. Switching between native and WSL keeps
the backend's model/limit/advanced settings, and removes fields that are not
compatible with the newly selected environment.

## Launch and isolation

The Windows side invokes the absolute `%SystemRoot%\System32\wsl.exe` with an
explicit `--distribution`, `--user` and `--cd`, and then the target interpreter;
no login shell and no shell string is involved, and adapter argv is fixed. The
Linux supervisor runs with a minimal Linux-only environment (`/usr/bin/env -i`);
Windows `HOME`, `PATH`, `WSLENV`, gateway tokens, Telegram tokens and proxy
variables are not forwarded.

Stdio is the existing JSONL protocol; the Windows parent sees the `wsl.exe`
launcher process, and the target-side supervisor owns exactly one fresh process
group for the CLI. Cleanup signals only that group after confirming the retained
PID identity, never arbitrary PID trees and never the distribution as a whole.
`spawn_wsl` fails closed on a non-Windows host — there is no silent fallback to a
native executable.

## Diagnostics

`acpgw doctor` reports the selected environment (`WSL <distro>/<user>`) before
host-level checks and marks target-side executable/cwd as unverified, because the
static check does not enter the distribution. It fails closed when the host is not
Windows or the launcher is missing. `doctor --live` runs the existing initialize-only
probe through the same WSL path; it sends no prompt and creates no thread.

```sh
uv run --frozen acpgw --config /path/to/test-config.yaml --env-file /path/to/test.env doctor
uv run --frozen acpgw --config /path/to/test-config.yaml --env-file /path/to/test.env doctor --live
```

Login/authentication stays on the target side. Initialize success does not prove
that the Linux CLI is signed in, nor that its subscription/model access works.

## Evidence and remaining acceptance

Verified on 2026-10-22 on Windows 10 (build 19045) and WSL2 Ubuntu-22.04, user
`user`:

- real installed Codex **0.163.0** and Claude Code in WSL initialized through the
  Windows `AgentClient` over this launcher, with isolated Linux `HOME`/agent home,
  zero user prompts and zero model calls;
- a Windows-side synthetic-peer suite crossed the real boundary and covered two
  sequential turns, allow/deny decisions and loss of the owned launcher.

Not verified, and not claimed: authenticated model turns, Telegram/other channel
round trip, native history/list/resume, Desktop attachment, other distributions,
WSL1, native Linux, macOS, or another computer. An abrupt kill of the supervisor
protects its immediate child via `PDEATHSIG` but cannot guarantee every descendant;
a descendant that deliberately escapes the owned process group is outside the
cleanup guarantee. When no cleanup receipt and normal wrapper exit are observed,
the adapter keeps the outcome uncertain rather than reporting a completed cleanup.

A protocol-output stall or an incomplete final drain (including after child exit
or requested EOF) causes nonzero exit **without** a cleanup receipt. The adapter
then retains uncertainty rather than reporting a clean close of a truncated stream.

The Linux supervisor generates its receipt capability internally; no value is
passed in Windows/Linux argv, launch payload or environment. Before creating the
native child it requires `PR_SET_DUMPABLE=0` and emits an initial BEGIN frame on its
own stderr. The parent validates ordering, bounded framing and matching CLEAN,
requires stderr EOF and normal wrapper exit, and strips reserved control frames.
Protection failure prevents native spawn. This mechanism is not a sandbox against
root or hostile same-UID signals. Synthetic tests and initialize-only smoke are
not overall Windows or live-model acceptance; independent source review of the
revised boundary was accepted on 2026-10-22 with the stated threat-model limits.

