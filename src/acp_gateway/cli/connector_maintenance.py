"""Local connector maintenance IPC; no VPS or owner API credential is needed."""

import json
import secrets
import time
from types import SimpleNamespace

import httpx
from filelock import FileLock

from acp_gateway.cli.maintenance_mode import run as maintenance_mode
from acp_gateway.cli.wizard import _atomic
from acp_gateway.connectors.lifecycle import _read
from acp_gateway.connectors.maintenance import REQUEST, RESPONSE, read_status


class LocalClient:
    def __init__(self, directory, *, timeout=10):
        self.directory, self.timeout = directory, timeout

    def request(self, method, path, **kwargs):
        state = read_status(self.directory)
        if method == "GET":
            return self.response(200, state)
        if state["state"] != "running":
            return self.response(409, {"detail": "Live connector activity is unavailable"})
        identity = secrets.token_hex(16)
        deadline = time.time() + self.timeout
        body = {
            "version": 1,
            "instance": state["instance"],
            "request": identity,
            "expires": deadline,
            "operation": path.rsplit("/", 1)[-1],
            **kwargs.get("json", {}),
        }
        with FileLock(self.directory / "connector-maintenance-client.lock", timeout=0):
            _atomic(self.directory / REQUEST, json.dumps(body).encode())
            while time.time() < deadline:
                try:
                    result = _read(self.directory / RESPONSE, limit=1024 * 1024)
                    if (
                        result.get("instance") == state["instance"]
                        and result.get("request") == identity
                    ):
                        return self.response(result["status"], result["result"])
                except (OSError, ValueError, KeyError, AttributeError):
                    pass
                time.sleep(0.05)
        raise ValueError("Connector maintenance timed out; inspect status before retrying")

    @staticmethod
    def response(code, data):
        return httpx.Response(
            code, json=data, request=httpx.Request("POST", "http://127.0.0.1/maintenance")
        )


def run(config, args):
    directory = config.settings.resolved_data_dir()
    if getattr(args, "reservation_file", None) is None:
        args = SimpleNamespace(
            **(vars(args) | {"reservation_file": directory / "connector-maintenance.json"})
        )
    maintenance_mode(LocalClient(directory), config, args)
    return 0


def sessions(config, args):
    from acp_gateway.cli.client import check_response, show_json

    client = LocalClient(config.settings.resolved_data_dir())
    operation = "sessions" if args.session_command == "list" else args.session_command
    response = client.request(
        "POST",
        f"/maintenance/{operation}",
        json={"agent": args.agent, "handle": getattr(args, "handle", None)},
    )
    check_response(response)
    show_json(response.json())
    return 0
