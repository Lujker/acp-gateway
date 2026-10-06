"""Contract checks against real goose traffic in tests/fixtures/acp (P1.2).

If goose changes its messages, record a new fixture set with
scripts/spike_acp.py + scripts/sanitize_fixture.py and these tests show what
the gateway no longer understands.
"""

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from acp_gateway.agents.events import OtherUpdate, normalize
from fakes import recordings as rec

INBOUND_UPDATES = [
    (scenario, record)
    for scenario in rec.scenarios()
    for record in rec.load(scenario)
    if record.direction == "in" and record.method == "session/update"
]


def test_fixtures_present():
    assert {"init", "modes", "ping", "permission-reject", "load-ws"} <= set(rec.scenarios())
    assert len(INBOUND_UPDATES) > 50


@pytest.mark.parametrize(
    ("scenario", "record"),
    INBOUND_UPDATES,
    ids=[f"{s}-{i}-{r.update_kind}" for i, (s, r) in enumerate(INBOUND_UPDATES)],
)
def test_every_recorded_update_is_understood(scenario, record):
    params = record.msg["params"]
    update = rec.parse_update(params["sessionId"], params["update"])
    event = normalize(params["sessionId"], update)
    assert not isinstance(event, OtherUpdate), f"unmodelled update kind {record.update_kind}"


def test_recorded_permission_request_parses():
    params = rec.request("permission-reject", "session/request_permission")
    tool_call = ToolCallUpdate.model_validate(params["toolCall"])
    options = [PermissionOption.model_validate(o) for o in params["options"]]
    assert tool_call.title.startswith("shell · ")
    assert set(tool_call.raw_input) == {"command", "timeout_secs"}
    assert [o.kind for o in options] == [
        "allow_always",
        "allow_once",
        "reject_once",
        "reject_always",
    ]


def test_recorded_session_defaults():
    new = rec.result_of("modes", "session/new")
    assert new["modes"]["currentModeId"] == "auto"
    assert {m["id"] for m in new["modes"]["availableModes"]} == {
        "auto",
        "approve",
        "smart_approve",
        "chat",
    }
    init = rec.result_of("init", "initialize")
    assert init["agentCapabilities"]["loadSession"] is True
    assert init["agentInfo"] == {"name": "goose", "version": "1.53.0"}


def test_recorded_load_replays_history_before_the_response():
    records = [r for r in rec.load("load-ws") if r.conn == "c2"]
    load_request = next(r for r in records if r.method == "session/load")
    response_index = next(
        i
        for i, r in enumerate(records)
        if r.direction == "in"
        and r.msg.get("id") == load_request.msg["id"]
        and "method" not in r.msg
    )
    replayed = {r.update_kind for r in records[:response_index] if r.update_kind}
    assert {"user_message_chunk", "agent_message_chunk"} <= replayed
