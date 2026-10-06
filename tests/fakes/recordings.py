"""Access to recorded real-agent traffic in tests/fixtures/acp/<agent-version>/.

Each fixture line is ``{"t", "conn", "dir", "msg"}`` as written by
``scripts/spike_acp.py`` and cleaned by ``scripts/sanitize_fixture.py``.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from acp.schema import SessionNotification

FIXTURES = Path(__file__).parents[1] / "fixtures" / "acp"
GOOSE = "goose-1.53.0"


@dataclass(frozen=True)
class Record:
    t: float
    conn: str
    direction: str  # "in" (agent -> client) | "out" (client -> agent)
    msg: dict[str, Any]

    @property
    def method(self) -> str | None:
        return self.msg.get("method")

    @property
    def update_kind(self) -> str | None:
        update = (self.msg.get("params") or {}).get("update") or {}
        return update.get("sessionUpdate")


@cache
def load(scenario: str, agent: str = GOOSE) -> tuple[Record, ...]:
    path = FIXTURES / agent / f"{scenario}.jsonl"
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = json.loads(line)
        records.append(Record(raw["t"], raw["conn"], raw["dir"], raw["msg"]))
    return tuple(records)


def scenarios(agent: str = GOOSE) -> list[str]:
    return sorted(p.stem for p in (FIXTURES / agent).glob("*.jsonl"))


def result_of(scenario: str, method: str, agent: str = GOOSE) -> dict[str, Any]:
    """The response to the first ``method`` request sent on the first connection."""
    records = load(scenario, agent)
    request = next(r for r in records if r.direction == "out" and r.method == method)
    response = next(
        r
        for r in records
        if r.direction == "in"
        and r.conn == request.conn
        and r.msg.get("id") == request.msg["id"]
        and "method" not in r.msg
    )
    return copy.deepcopy(response.msg["result"])


def update(scenario: str, kind: str, index: int = 0, agent: str = GOOSE) -> dict[str, Any]:
    """The ``index``-th recorded session update of ``kind`` (a fresh copy)."""
    matches = [r for r in load(scenario, agent) if r.update_kind == kind]
    return copy.deepcopy(matches[index].msg["params"]["update"])


def request(scenario: str, method: str, agent: str = GOOSE) -> dict[str, Any]:
    """Params of the first agent -> client request ``method``."""
    record = next(r for r in load(scenario, agent) if r.direction == "in" and r.method == method)
    return copy.deepcopy(record.msg["params"])


def parse_update(session_id: str, raw_update: dict[str, Any]) -> Any:
    """Turn a recorded update dict into the SDK model for ``session_update``."""
    return SessionNotification.model_validate(
        {"sessionId": session_id, "update": raw_update}
    ).update
