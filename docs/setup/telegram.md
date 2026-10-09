# Telegram channel

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

| Command | Action |
|---|---|
| Plain text | Send a prompt; receive a working status and final answer |
| `/start`, `/help` | Show commands |
| `/agent`, `/agent ALIAS` | List available routes or select one, e.g. `home/goose` |
| `/new [ALIAS]` | Create and activate a new session |
| `/sessions` | List this chat's selected agent sessions; `*` marks the active one |
| `/switch ID` | Activate a session owned by this chat and selected agent |
| `/status` | Agent/channel connections and this chat's running jobs |
| `/result JOB_ID` | Retrieve a saved answer or status owned by this chat |
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
this version sends working status and the final answer, without streaming
edits. Human approval requests include the tool title/input needed for a
decision.

The adapter limits commands to five messages per user per ten seconds,
uses bounded exponential backoff for polling, and retries outbound messages
only after an explicit Telegram `429` rejection (three attempts, maximum
30-second delay). Ambiguous network timeouts are not replayed automatically;
use `/result` or `/approvals` after recovery. Polling failure removes approval
eligibility until the connection recovers. An unreachable human is never
replaced by an automatic allow decision.

Final answers and saved `/result` replies use a separate bounded delivery queue
(32 replies). Long multi-part answers and their explicit-429 retry waits do
not block processing approval events or polling for button clicks. An exhausted
queue is logged; results remain in SQLite and can be requested again. This is
not durable automatic outbound delivery.

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
