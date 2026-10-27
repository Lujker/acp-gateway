"""Bounded, read-only native model observations; never setter confirmation."""

from acp_gateway.agents.model_control import identifier
from acp_gateway.log import redact_text

ACP_CONFIG = "acp.config_options"
SOURCES = {"codex.thread.start", "codex.thread.resume", "claude.system.init", ACP_CONFIG}
EFFORTS = {"none", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
# Kinds whose model is a native observation; Hermes reports through model.options.
NATIVE_KINDS = frozenset({"codex", "claude_code", "goose", "generic"})


def _identifier(value):
    try:
        clean = identifier(value)
    except ValueError:
        return None
    return clean if redact_text(clean) == clean else None


def observation(value):
    """Ignore absent/invalid fields without turning config/catalog into evidence."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("source"), str)
        or value["source"] not in SOURCES
    ):
        return None
    effort = value.get("effort")
    claude = value["source"] == "claude.system.init"
    return {
        "provider": None if claude else _identifier(value.get("provider")),
        "model": _identifier(value.get("model")),
        "effort": (
            effort
            if not claude
            and isinstance(effort, str)
            and effort in EFFORTS
            and redact_text(effort) == effort
            else None
        ),
        "source": value["source"],
    }


def codex_observation(response, *, loading):
    return observation(
        {
            "provider": response.get("modelProvider"),
            "model": response.get("model"),
            "effort": response.get("reasoningEffort"),
            "source": "codex.thread.resume" if loading else "codex.thread.start",
        }
    )


def claude_observation(frame):
    return observation({"model": frame.get("model"), "source": "claude.system.init"})


def config_observation(options):
    """Current values of standard ACP session config options (Goose and others).

    Only ``select`` options count: category ``model`` and ``thought_level``, plus
    an uncategorised option with id ``provider`` (Goose reports its provider so).
    Returns None when the agent reported no model.
    """
    if not isinstance(options, list | tuple):
        return None
    found = {}
    for option in options[:64]:
        if hasattr(option, "model_dump"):
            option = option.model_dump(by_alias=True)
        if not isinstance(option, dict) or option.get("type") != "select":
            continue
        value, category = option.get("currentValue"), option.get("category")
        if category == "model":
            found.setdefault("model", value)
        elif category == "thought_level":
            found.setdefault("effort", value)
        elif category is None and option.get("id") == "provider":
            found.setdefault("provider", value)
    if "model" not in found:
        return None
    clean = observation({**found, "source": ACP_CONFIG})
    return clean if clean and clean["model"] else None
