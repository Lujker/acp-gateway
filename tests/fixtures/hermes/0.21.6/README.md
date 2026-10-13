# Hermes 0.21.6 semantics fixture

`models.json` was generated on 2026-10-13 by `scripts/spike_hermes_models.py`.
It extracts selected installed upstream parser/setter definitions via AST and
runs synthetic idle/busy/compute-host/confirmation/sessionless/scope cases.
Provider imports are blocked, model resolution/application hooks are synthetic.
This is handler control-flow evidence, not a live model switch or catalogue.
Run the opt-in test with `HERMES_MODELS_REPO=/path/to/hermes-agent`.

`semantics.json` was generated on 2026-10-12 by
`scripts/spike_hermes_semantics.py` from the installed Windows Hermes source,
using Gateway's Linux test Python. It runs actual upstream busy/interrupt and
server-request code with synthetic in-memory sessions and fake agents/persistence.
No server, provider, model, executable tool or personal conversation is used.

Frames and cancellation notifications in the approval section are emitted by
the upstream request broker; random request IDs are replaced by stable aliases.
The two client response frames are synthetic input. This is a compatibility
fixture, not a recording of live Desktop approvals or a full session transcript.
Source hashes identify the exact helper files exercised. Windows runtime
verification and GUI acceptance must be recorded separately.

Regenerate explicitly, with Gateway dependencies available:

```bash
python scripts/spike_hermes_semantics.py --hermes-repo /path/to/hermes-agent \
  --output tests/fixtures/hermes/0.21.6/semantics.json
```

The fixture records three boundaries: busy submits queue/steer/redirect instead
of refusing; a session-scoped late interrupt reaches the next owner's turn;
the first approval response wins and expired/cancelled responses are ignored.
`expected_hosted_task_id` only guards hosted-room tasks and does not provide an
ordinary-chat turn token. Even its refusal invokes global TTS stop first.
