"""Protocol boundary rejects unsafe frames without reflecting input."""

import json
from uuid import uuid4

import pytest

from acp_gateway.connectors.protocol import (
    MAX_DATA_BYTES,
    MAX_FRAME_BYTES,
    AgentManifest,
    Close,
    Data,
    Error,
    Hello,
    Open,
    Opened,
    Ping,
    Pong,
    ProtocolError,
    Welcome,
    decode_frame,
    encode_frame,
)


@pytest.mark.parametrize(
    "frame",
    [
        Hello(
            computer_id="work-laptop", agents=[AgentManifest(alias="work", display_name="Goose")]
        ),
        Welcome(connection_id=uuid4()),
        Ping(sequence=0),
        Pong(sequence=19),
        Error(code="unavailable"),
        Open(epoch=uuid4(), stream=uuid4(), alias="goose"),
        Opened(epoch=uuid4(), stream=uuid4(), alias="goose"),
        Data(
            epoch=uuid4(),
            stream=uuid4(),
            alias="goose",
            message={"jsonrpc": "2.0", "id": 1, "result": {}},
        ),
        Close(epoch=uuid4(), stream=uuid4(), alias="goose", code="policy_denied"),
    ],
)
def test_round_trip(frame):
    assert decode_frame(encode_frame(frame)) == frame


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        "[]",
        "{",
        b"\xff",
        '{"version":true,"type":"ping","sequence":1}',
        '{"version":1,"type":"ping","sequence":true}',
        '{"version":1,"type":"ping","sequence":"1"}',
        '{"version":1,"type":"ping","sequence":-1}',
        '{"version":1,"type":"ping","sequence":9007199254740992}',
        '{"version":1,"type":"unknown"}',
        '{"version":1,"version":1,"type":"ping","sequence":0}',
        '{"version":1,"type":"welcome","connection_id":"bad"}',
        '{"version":1,"type":"hello","computer_id":"../work","agents":[]}',
        '{"version":1,"type":"ping","sequence":NaN}',
    ],
)
def test_reject_invalid_frames(payload):
    with pytest.raises(ProtocolError, match="invalid control frame"):
        decode_frame(payload)


def test_unknown_version_and_size_limits():
    with pytest.raises(ProtocolError, match="unsupported protocol version"):
        decode_frame('{"version":2,"type":"ping","sequence":0}')
    with pytest.raises(ProtocolError, match="too large"):
        decode_frame("ж" * MAX_DATA_BYTES)
    with pytest.raises(ProtocolError, match="too large"):
        decode_frame(
            json.dumps(dict(version=1, type="ping", sequence=0, extra="x" * MAX_FRAME_BYTES))
        )


def test_manifest_unique_aliases_no_urls_or_secrets():
    payload = {
        "version": 1,
        "type": "hello",
        "computer_id": "work",
        "agents": [
            {"alias": "work", "display_name": "Goose"},
            {"alias": "work", "display_name": "Other"},
        ],
    }
    with pytest.raises(ProtocolError):
        decode_frame(json.dumps(payload))
    payload["agents"] = [{"alias": "work", "display_name": "Goose", "url": "sensitive-input"}]
    with pytest.raises(ProtocolError) as exc:
        decode_frame(json.dumps(payload))
    assert "sensitive-input" not in str(exc.value)
    assert exc.value.__cause__ is None


def test_manifest_bounds_and_control_fields():
    for agents in ([], [{"alias": "work", "display_name": "x"}] * 101):
        with pytest.raises(ProtocolError):
            decode_frame(
                json.dumps(dict(version=1, type="hello", computer_id="work", agents=agents))
            )
    with pytest.raises(ProtocolError):
        decode_frame(
            '{"version":1,"type":"welcome","connection_id":"00000000-0000-0000-0000-000000000000","heartbeat_seconds":0}'
        )


def test_relay_has_separate_control_and_data_limits_and_rejects_nonfinite_json():
    message = {"jsonrpc": "2.0", "method": "notice", "params": {"text": "ж" * 40_000}}
    frame = Data(epoch=uuid4(), stream=uuid4(), alias="goose", message=message)
    encoded = encode_frame(frame)
    assert len(encoded.encode()) > MAX_FRAME_BYTES
    assert decode_frame(encoded) == frame
    frame.message["params"]["text"] = "x" * MAX_DATA_BYTES
    with pytest.raises(ProtocolError, match="too large"):
        encode_frame(frame)
    payload = json.loads(encoded)
    payload["message"]["params"]["number"] = float("nan")
    with pytest.raises(ProtocolError):
        decode_frame(json.dumps(payload))
