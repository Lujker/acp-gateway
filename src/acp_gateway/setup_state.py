"""Saved operational role and connector recipe, shared by CLI entry points."""

import json
from pathlib import Path

STATE = "setup.json"


def parse(contents):
    try:
        value = json.loads(contents or b"{}")
        if not isinstance(value, dict):
            raise ValueError
        if value and (
            type(value.get("version")) is not int
            or value["version"] != 1
            or value.get("role") not in {"local", "server", "connector"}
        ):
            raise ValueError
        if "env_file" in value and (
            not isinstance(value["env_file"], str) or not Path(value["env_file"]).is_absolute()
        ):
            raise ValueError
        if value.get("role") == "connector" and "connection" in value:
            connection = value["connection"]
            if not isinstance(connection, dict) or not all(
                isinstance(connection.get(key), str) and connection[key]
                for key in ("dispatcher_url", "computer_id", "token_file")
            ):
                raise ValueError
            if set(connection) - {"dispatcher_url", "computer_id", "token_file", "tls_fingerprint"}:
                raise ValueError
            if connection.get("tls_fingerprint") is not None and not isinstance(
                connection["tls_fingerprint"], str
            ):
                raise ValueError
        return value
    except (ValueError, KeyError, TypeError, RecursionError):
        raise ValueError("Invalid setup.json; rerun setup after repairing the saved role") from None


def read(config_path):
    try:
        with (config_path.parent / STATE).open("rb") as stream:
            contents = stream.read(65537)
    except FileNotFoundError:
        return {}
    if len(contents) > 65536:
        raise ValueError("Invalid setup.json; saved role exceeds its size limit")
    return parse(contents)
