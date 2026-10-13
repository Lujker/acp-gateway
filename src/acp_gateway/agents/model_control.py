"""Narrow ACP extension contract shared by dispatcher and host connector."""

import re

MODEL_OPTIONS = "_acpgw/model_options"
MODEL_SET = "_acpgw/model_set"
MODEL_RECOVER = "_acpgw/model_recover"
MODEL_METHODS = frozenset({MODEL_OPTIONS, MODEL_SET})
MODEL_REFUSED = -32010


def identifier(value) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 256
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+~-]*", value) is None
    ):
        raise ValueError("invalid provider or model identifier")
    return value


def model_params(method: str, params: dict) -> dict:
    if method == MODEL_RECOVER:
        from acp_gateway.agents.bindings import session_binding

        if set(params) != {"sessionId", "operationId", "binding"}:
            raise ValueError("invalid model recovery parameters")
        sid, operation = params["sessionId"], params["operationId"]
        binding = session_binding(params["binding"])
        if (
            not isinstance(sid, str)
            or re.fullmatch(r"[0-9a-f]{32}", sid) is None
            or not isinstance(operation, str)
            or re.fullmatch(r"[0-9a-f]{32}", operation) is None
            or binding is None
            or binding["restore"] != "connector-live"
        ):
            raise ValueError("invalid model recovery identity")
        return {"sessionId": sid, "operationId": operation, "binding": binding}
    allowed = {"sessionId"}
    if method == MODEL_SET:
        allowed |= {"provider", "model", "confirmationToken", "operationId"}
    if method not in MODEL_METHODS or set(params) - allowed:
        raise ValueError("invalid model operation parameters")
    sid = params.get("sessionId")
    if not isinstance(sid, str) or not sid or len(sid) > 4096 or "\x00" in sid:
        raise ValueError("invalid model session identifier")
    clean = {"sessionId": sid}
    if method == MODEL_SET:
        clean |= {
            "provider": identifier(params.get("provider")),
            "model": identifier(params.get("model")),
        }
        operation = params.get("operationId")
        if operation is not None:
            if not isinstance(operation, str) or re.fullmatch(r"[0-9a-f]{32}", operation) is None:
                raise ValueError("invalid model operation identity")
            clean["operationId"] = operation
        token = params.get("confirmationToken")
        if token is not None:
            if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
                raise ValueError("invalid model confirmation token")
            clean["confirmationToken"] = token
    return clean


def model_result(method: str, payload: dict) -> dict:
    """Validate and project extension replies before exposing them to channels."""
    if not isinstance(payload, dict):
        raise ValueError("invalid model operation result")
    if method == MODEL_RECOVER:
        status = payload.get("status")
        if status not in {
            "accepted",
            "deferred",
            "not_applied",
            "pending",
            "unknown",
            "unavailable",
        }:
            raise ValueError("invalid model recovery result")
        result = {"status": status}
        resolved = payload.get("resolved_request")
        if resolved is not None and status in {"accepted", "deferred"}:
            if not isinstance(resolved, dict):
                raise ValueError("invalid recovered model request")
            result["resolved_request"] = {
                "provider": identifier(resolved.get("provider")),
                "model": identifier(resolved.get("model")),
            }
        return result
    if method == MODEL_SET:
        status = payload.get("status")
        if status not in {"accepted", "deferred", "confirmation_required"}:
            raise ValueError("invalid model operation status")
        result = {"status": status}
        resolved = payload.get("resolved_request")
        if resolved is not None and status in {"accepted", "deferred"}:
            if not isinstance(resolved, dict):
                raise ValueError("invalid resolved model request")
            result["resolved_request"] = {
                "provider": identifier(resolved.get("provider")),
                "model": identifier(resolved.get("model")),
            }
        if status == "confirmation_required":
            token, message = payload.get("confirmation_token"), payload.get("message")
            if (
                not isinstance(token, str)
                or re.fullmatch(r"[0-9a-f]{32}", token) is None
                or not isinstance(message, str)
                or not message.strip()
                or len(message) > 4096
                or any(ord(c) < 32 and c not in "\n\t" for c in message)
            ):
                raise ValueError("invalid model confirmation")
            result |= {"confirmation_token": token, "message": message}
        return result
    rows = payload.get("providers")
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError("invalid model catalogue")
    providers = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("models"), list):
            raise ValueError("invalid model catalogue")
        if len(row["models"]) > 4096:
            raise ValueError("model catalogue exceeds its limit")
        providers.append(
            {
                "provider": identifier(row.get("provider")),
                "models": [identifier(m) for m in row["models"]],
            }
        )
    pending = payload.get("pending")
    if pending is not None:
        if not isinstance(pending, dict):
            raise ValueError("invalid pending model selection")
        pending = {
            "provider": identifier(pending.get("provider")),
            "model": identifier(pending.get("model")),
        }
    if payload.get("observation_source") != "model.options":
        raise ValueError("invalid model observation source")
    return {
        "provider": identifier(payload.get("provider")),
        "model": identifier(payload.get("model")),
        "providers": providers,
        "pending": pending,
        "observation_source": "model.options",
    }
