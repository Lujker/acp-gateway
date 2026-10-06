"""Strip personal data from recorded ACP traffic before it becomes a test fixture.

    uv run python scripts/sanitize_fixture.py spike-runs/<run>/traffic.jsonl -o tests/fixtures/...

What is replaced:
- the home directory of the recording user -> ``/home/user``;
- installed skills in ``available_commands_update`` -> one placeholder entry
  (built-in goose commands are kept);
- titles of sessions listed by ``session/list`` that the spike did not create.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SKILL_PLACEHOLDER = {
    "name": "example-skill",
    "description": "An installed skill (redacted from the recording).",
    "_meta": {"commandType": "Skill"},
}
SPIKE_TITLE = re.compile(r"(?i)acp spike|pong|code word")
HOME = re.compile(r"/home/[^/\"\s]+")


def sanitize_message(msg: dict[str, Any]) -> dict[str, Any]:
    update = (msg.get("params") or {}).get("update") or {}
    if update.get("sessionUpdate") == "available_commands_update":
        commands = update.get("availableCommands") or []
        builtins = [c for c in commands if (c.get("_meta") or {}).get("commandType") != "Skill"]
        has_skills = len(builtins) != len(commands)
        update["availableCommands"] = builtins + ([SKILL_PLACEHOLDER] if has_skills else [])

    result = msg.get("result")
    if isinstance(result, dict) and isinstance(result.get("sessions"), list):
        for session in result["sessions"]:
            if not SPIKE_TITLE.search(session.get("title") or ""):
                session["title"] = "user session"
    return msg


def sanitize_line(line: str) -> str:
    record = json.loads(line)
    record["msg"] = sanitize_message(record["msg"])
    return HOME.sub("/home/user", json.dumps(record, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="defaults to rewriting the source")
    args = parser.parse_args(argv)

    lines = args.source.read_text(encoding="utf-8").splitlines()
    cleaned = "\n".join(sanitize_line(line) for line in lines if line.strip()) + "\n"
    (args.output or args.source).write_text(cleaned, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
