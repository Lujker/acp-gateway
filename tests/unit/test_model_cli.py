"""Explicit CLI model targets stay attached to their revision and scope."""

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from acp_gateway.cli import client as cli
from acp_gateway.cli.main import _build_parser


@pytest.mark.parametrize(
    "arguments, method, path, body",
    [
        (["show", "--session", "7"], "GET", "/sessions/7/models", None),
        (["list", "--session", "7"], "GET", "/sessions/7/models", None),
        (["state", "--session", "7"], "GET", "/sessions/7/model-state", None),
        (
            ["reconcile", "--session", "7", "--revision", "9"],
            "POST",
            "/sessions/7/model-reconcile",
            {"expected_revision": 9},
        ),
        (
            [
                "set",
                "p",
                "m",
                "--session",
                "7",
                "--revision",
                "9",
                "--confirmation-token",
                "a" * 32,
            ],
            "POST",
            "/sessions/7/model",
            {"provider": "p", "model": "m", "expected_revision": 9, "confirmation_token": "a" * 32},
        ),
        (["default", "show"], "GET", "/model-default", None),
        (["default", "clear"], "DELETE", "/model-default", None),
        (["default", "set", "p", "m"], "PUT", "/model-default", {"provider": "p", "model": "m"}),
    ],
)
def test_model_commands_keep_explicit_target_and_do_not_send_prompts(
    monkeypatch, arguments, method, path, body
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "synthetic"})

    actual_client = httpx.Client
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(cli, "owner_token", lambda config: SecretStr("owner-only"))
    monkeypatch.setattr(cli, "api_url", lambda config: "http://127.0.0.1")
    args = _build_parser().parse_args(
        ["model", *arguments, "--agent", "home/hermes", "--thread", "work"]
    )
    assert cli.run(SimpleNamespace(), args) == 0
    assert len(requests) == 1
    request = requests[0]
    assert request.method == method
    assert request.url.path == path
    assert request.headers["Authorization"] == "Bearer owner-only"
    if body is not None:
        assert json.loads(request.content) == body | {"agent": "home/hermes", "thread": "work"}
    else:
        assert dict(request.url.params) == {"agent": "home/hermes", "thread": "work"}


def test_set_requires_explicit_dialogue_and_revision():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["model", "set", "p", "m"])
