import json

import sanitize_fixture


def record(msg):
    return json.dumps({"t": 0, "conn": "c1", "dir": "in", "msg": msg})


def test_skills_replaced_builtins_kept():
    line = record(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [
                        {"name": "compact", "_meta": {"commandType": "Builtin"}},
                        {
                            "name": "private-skill",
                            "description": "personal",
                            "_meta": {"commandType": "Skill"},
                        },
                    ],
                }
            },
        }
    )
    commands = json.loads(sanitize_fixture.sanitize_line(line))["msg"]["params"]["update"][
        "availableCommands"
    ]
    assert [c["name"] for c in commands] == ["compact", "example-skill"]
    assert "personal" not in json.dumps(commands)


def test_foreign_session_titles_and_home_paths():
    line = record(
        {
            "result": {
                "sessions": [
                    {"sessionId": "1", "cwd": "/home/alice/work", "title": "Quarterly numbers"},
                    {"sessionId": "2", "cwd": "/home/alice", "title": "ACP spike 1a2b3c"},
                ]
            }
        }
    )
    sessions = json.loads(sanitize_fixture.sanitize_line(line))["msg"]["result"]["sessions"]
    assert [s["title"] for s in sessions] == ["user session", "ACP spike 1a2b3c"]
    assert [s["cwd"] for s in sessions] == ["/home/user/work", "/home/user"]


def test_main_rewrites_in_place(tmp_path):
    path = tmp_path / "traffic.jsonl"
    path.write_text(record({"params": {"cwd": "/home/bob"}}) + "\n")
    assert sanitize_fixture.main([str(path)]) == 0
    assert "/home/user" in path.read_text()
