# Telegram cards, answers and work summaries

Included in **0.2.3** and deployed to the owner's dispatcher on 2026-10-26. See the
[visual preview](../previews/telegram-cards.html), rendered with fictitious data
using the same formatting functions as the bot.

## System cards

The menu, current context, computers, profiles, jobs, errors and other system
responses share bold headings, highlighted field labels, meaningful emoji and
spacing between connection, directory, dialogue and task information. The menu
does not repeat its selected agent and dialogue. Help/status have separate
sections. Values remain plain data: HTML is escaped and secrets are redacted
before formatting; user text cannot inject markup or controls.

## Codex output

Codex message phases are taken from the installed CLI's generated schema,
matching the [official App Server item documentation](https://learn.chatgpt.com/docs/app-server#items):

- `commentary` becomes a work update rather than answer text.
- `partial_answer` and `final_answer` become answer text, with paragraph boundaries
  between distinct messages.
- Only the public `reasoning.summary` is available as a work update. Native raw
  reasoning content/text events are not forwarded or saved by this projection.
- Missing phase metadata retains compatibility behavior; text is not classified
  by language or guessed from its wording.

Message deltas are bounded and held until `item/completed`, so a late phase cannot
leak a commentary into the final result. The completion's text is authoritative.
Legacy peers that omit item completion still flush their pending messages when
the turn ends. Native identity, permissions, cancellation and resume fences are
unchanged. This affects new turns; old saved answers are not rewritten.

## Work view

The live task card shows public work updates separately from any answer preview.
Use **🧭 Ход работы** on a task/result, or `/work JOB_ID`, for the separate view.
It is scoped to the job's owning private chat, independently of the currently
selected route. Viewing it never calls a model or resumes a native chat.

The snapshot is advisory rather than a complete tool audit. Up to 64 live entries
are retained in memory; the first 6000 characters of public work are captured,
with a clear truncation notice. The last 20 completed snapshots per private chat
are retained for reading after a restart. Missing/evicted snapshots are reported
as unavailable, not recreated by prompting the agent.

Only a redacted snapshot is displayed or persisted. Bounded original public text
is held in memory to preserve masking context across split credentials; an
unfinished trailing token is withheld while streaming and at the truncation
boundary. Persisting advisory history cannot block delivery of the final answer.

## Long answers and compatibility

New multipart answers have **📄 Ответ · 1/N** labels and prefer whitespace
boundaries. Each part has valid independent HTML, including reopened code/style
entities, with room reserved for the part header. Buttons for continuing or
creating a dialogue remain on the last part.

Rendering is versioned. A delivery that already acknowledged a prefix under the
old Markdown v1 format continues with its original text and boundaries. Older
plain deliveries are also retained. A formatter upgrade never silently replays
acknowledged content; unknown network outcomes retain the existing safeguards.

The implementation was checked with fake native peers and Telegram sessions;
these checks do not spend model tokens. A live visual/model acceptance step is
separate from source validation.
