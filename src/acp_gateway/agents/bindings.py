"""Small allowlisted provenance record, never an authority to resume a session."""

from acp_gateway.log import redact_text


def session_binding(value):
    if value is None:
        return None
    required = {"backend", "mode", "profile", "runtime_id", "stored_id", "restore"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid session binding metadata")
    if (
        value["backend"] != "hermes"
        or value["mode"] != "dedicated"
        or value["restore"] not in ("disabled", "connector-live")
    ):
        raise ValueError("unsupported session binding metadata")
    for field in required:
        text = value[field]
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 256
            or not text.isprintable()
            or redact_text(text) != text
        ):
            raise ValueError("invalid session binding metadata")
    return dict(value)
