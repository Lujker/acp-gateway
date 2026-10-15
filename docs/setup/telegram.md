# Telegram channel

Published 0.2.0 includes durable result delivery, status edits, inline controls,
Markdown rendering, dialogue execution modes, **Computers** and **Jobs**.
See [recovery and status configuration](telegram-recovery.md).
Current working-branch recovery, button-driven renaming and actionable error
cards are not published; see the [P2.5 journal](../items/telegram-recovery.ru.md).

Navigation buttons edit their panel; agent answers and approval cards remain
separate messages. Job history and saved results belong to the requesting chat
across its agents, including agents removed from configuration. Read-only panels
use saved metadata and connector snapshots without submitting prompts or probing
agents. See the [development journal and acceptance checklist](../items/telegram-ui.ru.md).

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
| `/start`, `/menu` | Open the inline control panel (rc6) |
| `/help` | Show commands |
| `/context` | Working branch: enable or explicitly refresh one durable context card for this chat |
| `/recent` | Working branch: preview the 10 most recently used dialogues across this chat’s agents |
| `/favorites [PAGE]` | Working branch: saved route shortcuts with current availability |
| `/agent`, `/agent ALIAS` | List available routes or select one, e.g. `home/goose` |
| `/new [ALIAS]` | Create and activate a new session |
| `/sessions [PAGE]` | Dialogue cards for the selected agent, 10 per page; ▶ marks the active one |
| `/dialogue ID [PAGE]` | Preview an owned dialogue without changing selection; return to the supplied list page |
| `/cancel` | Working branch: cancel name input begun by the dialogue rename button; other commands cancel it too |
| `/rename ID TITLE` | Persistent dispatcher display name; does not rename Hermes |
| `/approval REQUEST_ID [PAGE]` | Complete pending request input by page |
| `/switch ID [PAGE]` | Activate a session owned by this chat and selected agent |
| `/computers [PAGE]`, `/computers id ID` | Enrolled PCs, link/ACP readiness and agent selection |
| `/jobs [active] [before ID]` | This chat's task history, 10 per page |
| `/job JOB_ID` | Exact task, refresh, saved result and delivery |
| `/status` | PC link, agent readiness and this chat's running jobs |
| `/result JOB_ID` | Retrieve a saved answer or status owned by this chat |
| `/delivery JOB_ID` | Inspect saved automatic delivery state |
| `/stop` | Cancel this chat's selected agent jobs |
| `/approvals` | Redisplay pending human approval buttons |
| `/model`, `/models [PAGE]` | Observe the model and choose from paginated model buttons |

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
approval fields are redacted. Tool progress/reasoning is not forwarded. Version 0.2.0 edits the working
message with a bounded partial answer and final job status.
Human approval requests include the tool title/input needed for a decision.

The adapter limits commands to five messages per user per ten seconds,
uses bounded exponential backoff for polling, and retries outbound messages
only after an explicit Telegram `429` rejection (three attempts, maximum
30-second delay). Ambiguous network timeouts are not replayed automatically;
use `/result` or `/approvals` after recovery. Polling failure removes approval
eligibility until the connection recovers. An unreachable human is never
replaced by an automatic allow decision.

Saved `/result` replies use a separate bounded in-memory queue (32 replies).
Version 0.2.0 persists automatic
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


Dialogue cards show the saved title (or “Без названия”), last activity in UTC,
connection state when known, and the working folder on a separate line. `/new`
and `/switch` confirm the dialogue ID, agent and folder. The list is scoped to
this private chat and selected agent; `/sessions 2` opens the next page.

In the development UI, a dialogue list button opens a preview without changing
the selected dialogue or contacting the agent. The preview shows the saved title,
agent, folder, last activity and whether messages will go to that dialogue.
Use **Выбрать диалог** to select it explicitly. **Переименовать** explains the
explicit `/rename ID TITLE` command; plain text continues to go to the agent. **← Диалоги** returns to the
original page, including after selection. Text commands `/dialogue ID [PAGE]`
and `/switch ID [PAGE]` offer the same actions; the default return page is 1.
Invalid pages and dialogues outside this chat/agent are rejected before selection.


## Inline controls and formatted answers (rc6)

`/start` or `/menu` opens buttons under the message: Agents, Dialogues, New
dialogue, Status, Model, Approvals and Help. Agent and dialogue lists offer
dialogue previews, explicit selection buttons and dialogue pages. Text commands
remain available. No reply keyboard is installed and button labels are never submitted as prompts.

Each control is bound to the private user/chat and exact message. Agent/dialogue
controls also capture the selected route and revision. Changing selection makes
old controls stale. New/switch/model changes and cancellation are one-shot;
a repeated click cannot repeat a host operation. Controls expire after one hour,
a dispatcher restart, or bounded cache eviction; reopen `/menu`. Callback data
contains an opaque random reference, not routes or confirmation credentials.
Navigation shares the message rate limit. Human tool approvals keep their
separate existing binding and do not consume the navigation limit.

Job controls show the originating agent and dialogue ID. Saved results and
job details remain accessible through their own private button even after
selecting another agent. Cancel targets the exact owned job, never a newer job
on another route; it is omitted when safe cancellation is explicitly unsupported.
Finished jobs cannot be cancelled through an old control. Model choice buttons
carry the observed dialogue/revision; upstream-required confirmation uses a
separate one-shot button with the token retained only on the dispatcher.

Agent answers, explicit results and bounded status previews support Markdown
headings, emphasis, strikeout, inline code, fenced code with language, lists,
quotes and HTTP(S) links. This compatible `sendMessage` HTML path renders tables
as monospaced rows. Raw HTML is literal; images remain descriptive links and
are not downloaded. Link previews are disabled. Unsupported math/media are
not silently promoted into executable HTML or remote file uploads.

Every long-answer fragment is independently valid HTML with at most 2000
codepoints/4000 UTF-16 units after entity parsing. Formatting closes and reopens
at boundaries, including code blocks. Redaction precedes parsing; redaction masks
remain visible. Only an explicit Telegram entity-parse rejection falls back to
plain text; ambiguous network outcomes never trigger an automatic resend.

Automatic delivery hashes a versioned, deterministic rendered payload before
sending and ACKs one fragment at a time. Already-bound rc5 deliveries finish
using their original plain-text chunks; changed content/unknown outcomes retain
the existing fences. Manual result buttons use the bounded explicit delivery
queue and do not replay a model prompt. Formatting does not change stored answers.


## Dialogue execution mode (0.2.0rc7)

`/menu` → **Выполнение**, or `/mode`, controls the selected dialogue:

- **Как Hermes** (default): Hermes decides which actions need approval; the
  dispatcher sends those requests to a human. Smart mode can still ask about
  untrusted MCP servers and tools without a read-only annotation.
- **Без подтверждений**: explicitly authorize every subsequent permission
  request in this dialogue. The dispatcher returns `allow_once` and records each
  decision in the approval audit. This includes writes and command execution.

Choose the mode between jobs. The panel shows the agent, dialogue and current
mode. It persists for that dialogue across dispatcher restart; a new dialogue
always starts with the default. Other chats, computers and Hermes profile settings
are unaffected. Changing back restores human handling of future requests.
The gateway's configured approval policy still applies, and audit failure never
returns an allow decision. Unsupported host operations remain unsupported.

New Hermes dialogues no longer receive the pinned title `Dispatcher`: Hermes can
name them normally. The gateway dialogue list uses a short first-message summary.
Existing Hermes titles are retained; rename them in Desktop if desired.


## Dialogue names, approvals and work status (0.2.0)

Dispatcher dialogue names persist separately from agent titles. An explicit
`/rename ID TITLE` accepts 1–120 printable characters, checks this chat and
selected agent, and leaves dialogue selection unchanged. Later agent title
updates remain stored as the host title; the dispatcher display name wins in
its own views. Hermes Desktop titles are not synchronized.

Approval cards show the originating channel, agent/PC, job, dialogue and UTC
deadline. A shortened input is clearly marked; **Полный ввод** opens the complete
redacted JSON in numbered pages. Decisions remain once-only and bound to the
private user, chat, message and request, independent of the currently selected
agent. Every displayed copy gets the recorded outcome and loses its controls
after resolution or expiry. If an edit fails, the durable decision remains
authoritative; callbacks refuse already settled requests. Job links are offered
only for approvals originating in the same Telegram chat.

The advisory working card shows the actual job state, route, dialogue, elapsed
time and approval wait. It contains only an outward answer preview, no internal
reasoning or raw tool stream. Each task has its own controls. Status edits use
the existing interval and retry-after backoff in a separate task; durable result
delivery and approval processing stay independent. Cancellation is offered only
when supported, and the UI shows the actual state returned by the core.

History → job → delivery → job → history preserves the active filter and cursor.
**Выбрать диалог AGENT · #ID** explicitly selects the owned job's exact route and
dialogue without an ACP prompt. Retired agents keep their saved results but do
not offer a dialogue selection action.

These capabilities are included in 0.2.0. Live acceptance must still check
concurrent dialogues on both PCs, allow/deny/expiry, names after reconnect,
and status/result delivery independently.

## Current working branch (unpublished)

P2.5 adds Russian delivery explanations and a saved-result button, actions for
a dialogue that cannot be restored, name input from the rename button with a
five-minute deadline, and actionable Russian error cards. While name input is
active, the next plain message sets the dispatcher name instead of submitting
a prompt. Use the Cancel button or `/cancel`; another command cancels input too.
Input is checked against chat, route, dialogue selection and record identity,
and survives a dispatcher restart without converting a name into a prompt.

Errors offer explicit navigation, never an automatic retry of agent work.
Connection loss directs the user to task history to inspect the outcome before
resubmitting. Diagnostics remain redacted. These branch changes have not been
released, installed or checked against live Telegram; see the
[implementation evidence and remaining UX](../items/telegram-recovery.ru.md).


### Context card (current working branch, unpublished)

Use `/menu` → **Текущий контекст**, or `/context`, to enable a separate message
showing the computer/route, agent, selected dialogue, dispatcher name, cwd and
availability. The main menu also shows the full snapshot. Direct agents do not
advertise a computer name; the card says so instead of guessing the host.
Without a selected dialogue, cwd is explicitly the default for new dialogues.
Active name input is marked because the next text will set a name instead of
submitting a prompt. Another command, including `/context`, cancels name input.

Once enabled, the card edits the same message after choices change and checks
saved state/connector snapshots every five seconds without probing an agent.
You can pin that message manually in Telegram. The dispatcher keeps the message
reference across restart and rebinds controls without sending another card.
This is independent of answer delivery and approval processing.

An explicit missing-message response permits replacement. An ambiguous send/edit
is not replayed automatically; an independent warning says the snapshot may be
stale. Use `/context` to request recovery explicitly. After an unknown send, this
can create a new card while an earlier copy may already exist. Explicit 429 edit
rejections back off; unknown delivery of results retains its separate fences.
Live Telegram acceptance and release remain outstanding.


### Dialogue actions on results (current working branch, unpublished)

The last result fragment offers **Продолжить диалог** and **Новый диалог**.
Continue selects that task's exact owned route and dialogue without sending a
prompt. New creates a dialogue on the task's route and in its original cwd;
conversation history is not replayed. Send the next message explicitly.
These controls also appear on a finished work card. Removed routes retain their
saved-result controls but offer no dialogue actions. A dialogue known to be
unrestorable offers only New.

Actions are bound to user/chat/message and route/selection revisions and are
one-shot. Selection is rechecked after callback acknowledgement as well. A
selection change during creation keeps the new dialogue recorded without
silently replacing a newer choice; there is no automatic new-session retry.
The result message is never edited into a navigation panel or confirmation.


### Recent dialogues and favorite routes (current working branch, unpublished)

Use `/menu` → **Недавние** or `/recent` for the ten most recently used dialogues
across all agents in this private chat. Opening a row previews its title, route,
cwd and last activity without changing selection. **Выбрать диалог** explicitly
selects the exact owned route/dialogue; it does not reconnect or submit a prompt.
Removed routes retain metadata and saved local names but have no select button.
A route known to be offline, disabled or unadvertised is unavailable until refreshed.
Known unrestorable dialogues cannot be selected for continuation here.

In **Агенты**, use **☆ Добавить** to save a favorite route. `/favorites` shows
up to twenty saved routes, ten per page, with select/remove controls. Removing a
favorite does not remove an agent or dialogue. Selecting a route keeps that
route's existing active dialogue; use New explicitly if needed. Direct agents
that have not been connected are marked as unchecked rather than offline.
Favorite routes are stored per bot/private chat and survive dispatcher restart.
Old mutation controls check the favorite list revision; select controls also
check route/selection revisions and cannot replace a newer choice. No fallback
to a different route and no automatic prompt or session creation occurs.


### Final stage/model hints and VPS preview

The initial work card acknowledges admission as **Принято диспетчером**; subsequent
edits show real execution/approval/final states. Model intent, pending/deferred,
refusal and unknown outcome have Russian explanations. Saved evidence is separate
from confirmed application; an explicit reconciliation inspects the retained
operation without replaying the model setter. Queue/other questions are not shown
without real adapter capabilities.

Use the [Docker preview instructions](telegram-preview-docker.ru.md) to try the
unpublished branch on an existing VPS. Publication and live acceptance remain
separate from source verification.
