# Telegram channel

Development 0.2.0rc2.dev0 adds durable result delivery and advisory status
edits. See [recovery and status configuration](telegram-recovery.md).
The frozen 0.2.0rc1 self-install candidate keeps its earlier behavior.

Use a separate development bot created with BotFather, and put its token in
`.env` as `TELEGRAM_BOT_TOKEN`. Never put the token in YAML or a command line.
Enable the channel with an explicit numeric user allowlist:

```yaml
telegram:
  enabled: true
  token_env: TELEGRAM_BOT_TOKEN
  allowed_user_ids: [123456789]  # replace with your own Telegram user ID
policy:
  approver_channels: [telegram, cli]
```

Merge these settings into the existing sections. Run `acpgw config check`,
then restart your foreground daemon or `acpgw service restart`. The Bot API
is used through [aiogram 3](https://docs.aiogram.dev/en/latest/).
Only one daemon may poll a bot token. An existing webhook must be removed
manually before polling; the gateway never changes or deletes webhooks.

Open the bot's private chat and send `/start`. Groups, bots, unknown users
and messages whose sender differs from the private chat ID are ignored.
An enabled channel requires at least one allowlisted user. The bot becomes
an eligible approver after an authorized chat has contacted it and polling
is working. Known authorized chats survive restart; removing a user from
the configuration also removes their access. The owner API, MCP and bot
tokens must be independent.

Create a dialogue explicitly with `/new [ALIAS]` or select one with `/switch ID`
before sending plain text. Plain text never creates a dialogue implicitly.
The selected agent and dialogue survive restart; unavailable agents and failed
requests do not select a replacement. A failed `/new ALIAS` keeps the previous
route. For newly created managed connector dialogues, idle reconnect to the VPS
or dispatcher restart retains the same Hermes chat while connector/Hermes stay
alive. A drop during a pending turn, connector restart or lost Hermes attachment
does not enable restoration. The active choice remains; no new chat or prompt
is created automatically. Older direct/disabled bindings retain their refusal rule.

| Command | Action |
|---|---|
| Plain text | Send to the selected dialogue; refuse if none is selected |
| `/start`, `/help` | Show commands |
| `/agent`, `/agent ALIAS` | List available routes or select one, e.g. `home/goose` |
| `/new [ALIAS]` | Create and activate a new session |
| `/sessions [PAGE]` | Dialogue cards for the selected agent, 10 per page; ▶ marks the active one |
| `/switch ID` | Activate a session owned by this chat and selected agent |
| `/status` | PC link, agent readiness and this chat's running jobs |
| `/result JOB_ID` | Retrieve a saved answer or status owned by this chat |
| `/delivery JOB_ID` | Development: inspect saved automatic delivery state |
| `/stop` | Cancel this chat's selected agent jobs |
| `/approvals` | Redisplay pending human approval buttons |

Human approvals from other originating channels, including MCP and CLI, are
delivered to known authorized private chats. Approvals for a Telegram chat's
own job go only to that chat, like its answers and `/result`; each card names
its origin. Buttons offer only `allow_once` and
`reject_once`; the gateway checks user, chat, message, request and deadline.
Every decision is audited before it reaches the agent. Settled/expired
requests cannot be approved again; their buttons are removed. The MCP
channel still has no approval tools.

Messages are escaped for HTML and split into chunks of at most 2000 Unicode
codepoints (at most 4000 UTF-16 units). Registered credentials and sensitive
approval fields are redacted. Tool progress/reasoning is not forwarded;
0.2.0rc1 sends working status and the final answer. Development 0.2.0rc2.dev0
edits the working message with a bounded partial answer and final job status.
Human approval requests include the tool title/input needed for a decision.

The adapter limits commands to five messages per user per ten seconds,
uses bounded exponential backoff for polling, and retries outbound messages
only after an explicit Telegram `429` rejection (three attempts, maximum
30-second delay). Ambiguous network timeouts are not replayed automatically;
use `/result` or `/approvals` after recovery. Polling failure removes approval
eligibility until the connection recovers. An unreachable human is never
replaced by an automatic allow decision.

In 0.2.0rc1, final answers and saved `/result` replies use a separate bounded
in-memory queue (32 replies). Development 0.2.0rc2.dev0 persists automatic
plain-text submission results with a chunk ACK cursor; /result retains its
explicit bounded queue. Long replies and status edits do not block approval
events or button clicks. See [delivery states and restart/rollback rules](telegram-recovery.md).

The SQLite cursor is committed before command handling. A crash can lose a
reply, but replaying an update cannot submit the same command again after
restart. This is an at-most-once dispatch guarantee, not an exactly-once
delivery guarantee. Cursors are namespaced by bot ID and expire after six
inactive days because Telegram can randomize update IDs after a week.
Sessions and final results remain in the gateway database. See the
[Telegram Bot API update contract](https://core.telegram.org/bots/api#getupdates).

The automated integration tests use real aiogram types/dispatcher with a
fake Telegram session and the recorded Goose ACP server. A live test needs
an authorized person to send messages/click buttons in the bot; a valid
token or successful `getMe` alone does not prove that user journey.


## Hermes provider/model controls (0.2.0rc1 candidate)

Use /model for observation, /models for the catalogue, and /model state for
stored evidence without a host connection. Set with
/model set PROVIDER MODEL ID REVISION [TOKEN], using the target/revision from
the response. /model default [PROVIDER MODEL | clear] affects later new
dispatcher dialogues only. /model reconcile ID REVISION reads the exact retained
operation outcome without repeating the mutation. See the
[P4.8 acceptance guide](p48-acceptance.md) for confirmation and recovery limits.


Since 0.2.0rc5, the dispatcher registers the command menu automatically
at startup (default English and Russian descriptions for private chats). BotFather
configuration is unnecessary. Registration retries independently when the API
is unavailable; it never sends chat messages or changes the allowlist.

`/status` uses local snapshots and does not initialize agents or call a model.
It distinguishes an online PC from an ACP agent that has not been checked yet.
A ready agent is reported only when both the PC connection and ACP client are
ready. Optional inactive CLI approvers are omitted; MCP is described as serving,
since its HTTP interface does not imply a persistent client connection.


Dialogue cards show the saved title (or “Untitled dialogue”), last activity in UTC,
connection state when known, and the working folder on a separate line. `/new`
and `/switch` confirm the dialogue ID, agent and folder. The list is scoped to
this private chat and selected agent; `/sessions 2` opens the next page.
