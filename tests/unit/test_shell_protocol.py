"""Shell control is negotiated, bounded and cannot confuse inspection with execution."""

import json
import time
from uuid import uuid4

import pytest

from acp_gateway.connectors.protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    ShellRequest,
    ShellResponse,
    ShellResult,
    decode_frame,
    encode_frame,
)


def request(**updates):
    return ShellRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=uuid4(),
        actor="telegram:123",
        expires=time.time() + 30,
        action="start",
        alias="code",
        session_id="owned",
        cwd="/work",
        command="printf hello",
        **updates,
    )


def test_shell_roundtrip_and_worst_case_output_window_fit_control_frame():
    frame = request()
    assert decode_frame(encode_frame(frame)) == frame
    result = ShellResult(
        operation=frame.operation,
        state="completed",
        cleanup_verified=True,
        exit_code=0,
        output="\x01" * 4000,
        total=4000,
    )
    response = ShellResponse(epoch=frame.epoch, request=frame.request, result=result)
    encoded = encode_frame(response)
    assert len(encoded.encode()) < MAX_FRAME_BYTES
    assert decode_frame(encoded) == response


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "status"},
        {"action": "cancel"},
        {"alias": None},
        {"cwd": None},
        {"offset": 1},
        {"timeout": 0},
        {"timeout": 601},
        {"timeout": float("nan")},
        {"offset": True},
        {"command": "x" * 4097},
        {"unexpected": "private-value"},
    ],
)
def test_bad_request_fields_are_rejected_without_reflecting_payload(changes):
    payload = request().model_dump(mode="json")
    payload.update(changes)
    with pytest.raises(ProtocolError) as error:
        decode_frame(json.dumps(payload))
    assert str(error.value) == "invalid control frame"
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "completed", "exit_code": 0},
        {"state": "completed", "exit_code": 3, "cleanup_verified": True},
        {"state": "running", "output": "unexpected", "total": 10},
        {"state": "unknown", "output": "unverified", "total": 10},
        {"state": "failed", "cleanup_verified": True, "output": "x" * 4001, "total": 4001},
        {"state": "failed", "cleanup_verified": True, "output": "xx", "offset": 1, "total": 2},
    ],
)
def test_reply_cannot_claim_success_or_publish_unverified_unbounded_output(changes):
    payload = ShellResponse(
        epoch=uuid4(),
        request=uuid4(),
        result=ShellResult(
            operation=uuid4(),
            state="unknown",
        ),
    ).model_dump(mode="json")
    payload["result"].update(changes)
    with pytest.raises(ProtocolError):
        decode_frame(json.dumps(payload))
