"""The CLI reconciles snapshots and displays message chunks only."""

import json

import httpx
import pytest

from acp_gateway.api.app import _sse
from acp_gateway.cli.client import approval_connection, events, stream_job


def frame(kind, data):
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"


def test_stream_reconciles_missing_chunks_and_ignores_thought(capsys):
    job = {"id": "job1", "status": "running", "answer": ""}
    final = job | {"status": "completed", "answer": "hello world", "error": None}
    text = (
        frame("snapshot", job)
        + frame("JobProgress", {"event_type": "ThoughtChunk", "event": {"text": "private"}})
        + frame("JobProgress", {"event_type": "MessageChunk", "event": {"text": "hello"}})
        + frame("resync", job | {"answer": "hello wor"})
        + frame("JobFinished", {"job": final})
    )
    with httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=text)),
    ) as c:
        assert stream_job(c, job) == 0
    captured = capsys.readouterr()
    assert captured.out == "hello world\n"
    assert "completed" in captured.err


def test_lost_stream_reports_recoverable_job_id():
    job = {"id": "job-recover", "status": "running", "answer": ""}
    with httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="")),
    ) as c:
        with pytest.raises(ValueError, match="acpgw result job-recover"):
            stream_job(c, job)
        with pytest.raises(ValueError, match="before initialization"), approval_connection(c):
            pytest.fail("empty stream initialized")


def test_json_stream_emits_single_final_object(capsys):
    job = {"id": "j", "status": "running", "answer": ""}
    final = job | {"status": "completed", "answer": "pong", "error": None}
    text = frame("JobProgress", {"event_type": "MessageChunk", "event": {"text": "pong"}})
    text += frame("JobFinished", {"job": final})
    with httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=text)),
    ) as c:
        assert stream_job(c, job, as_json=True) == 0
    assert json.loads(capsys.readouterr().out) == final


def test_ctrl_c_requests_cancellation_before_returning():
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            raise KeyboardInterrupt
        return httpx.Response(200, json={"status": "cancelled"})

    with (
        httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handle)) as c,
        pytest.raises(KeyboardInterrupt),
    ):
        stream_job(c, {"id": "j", "status": "running"})
    assert calls == [("GET", "/jobs/j/events"), ("POST", "/jobs/j/cancel")]


def test_sse_preserves_unicode_line_separators():
    payload = {"answer": "Hello 🌍\u2028world\u0085next\nline"}
    response = httpx.Response(200, text=_sse("snapshot", payload))
    assert list(events(response)) == [("snapshot", payload)]
