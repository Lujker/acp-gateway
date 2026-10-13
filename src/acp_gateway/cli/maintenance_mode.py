"""Owner-only admission reservation; never cancels a job or mutates a service."""

import json
import os
import re
import stat

from acp_gateway.cli.client import request, show_json
from acp_gateway.connectors.lifecycle import _read


def _check_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ValueError("Maintenance reservation must be a private regular file")
    if os.name == "nt":
        from acp_gateway.windows_acl import check_private_paths

        check_private_paths([path], directories=[path.parent], purpose="Maintenance reservation")
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Maintenance reservation file must be private to the current user")


def run(client, config, args):
    action = args.maintenance_command
    if action == "status":
        show_json(request(client, "GET", "/maintenance"))
        return
    path = args.reservation_file or config.settings.resolved_data_dir() / "maintenance.json"
    path = path.expanduser().absolute()
    if action == "prepare":
        if os.name == "nt":
            from acp_gateway.windows_acl import ensure_private_directory

            ensure_private_directory(path.parent)
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Reserve the local file before the HTTP request; another CLI cannot
        # overwrite a reservation when it loses the race for the server.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        token = None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                _check_file(path)
                data = request(client, "POST", "/maintenance/prepare")
                token = data.get("token")
                if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
                    token = None
                    raise ValueError("Invalid maintenance reservation response")
                json.dump({"version": 1, "token": token}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            if token is not None:
                try:
                    request(client, "POST", "/maintenance/resume", json={"token": token})
                except Exception as recovery:
                    raise ValueError(
                        "Reservation save/recovery failed; inspect maintenance status and use "
                        "maintenance resume --force if the token was lost."
                    ) from recovery
            raise
        print(f"Dispatcher reserved; private reservation file: {path}")
        return
    exists = path.exists() or path.is_symlink()
    if exists:
        _check_file(path)
    elif not args.force:
        raise ValueError(
            "Reservation file is missing; inspect maintenance status and use resume --force "
            "only for owner recovery."
        )
    if args.force:
        body = {"force": True}
    else:
        data = _read(path)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "token"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["token"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", data["token"])
        ):
            raise ValueError("Invalid maintenance reservation file")
        body = {"token": data["token"]}
    show_json(request(client, "POST", "/maintenance/resume", json=body))
    if exists:
        path.unlink()
