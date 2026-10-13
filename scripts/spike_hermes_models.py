"""Run selected installed Hermes model handlers with synthetic state only.

AST extraction avoids importing provider/config modules. Network, persistence,
agent construction and model resolution are replaced by in-memory hooks. This
checks handler control flow, not a live switch or provider authentication.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import contextlib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def extract(path: Path, names: set[str], scope: dict) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
        else:
            continue
        if name in names:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
            selected.append(node)
            found.add(name)
    if found != names:
        raise ValueError(f"upstream definitions changed: {sorted(names - found)}")
    # Explicit compatibility execution of trusted local upstream definitions.
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), scope)  # noqa: S102


def run(repo: Path) -> dict:
    repo = repo.resolve(strict=True)
    files = {
        "parser": repo / "hermes_cli/model_switch.py",
        "setter": repo / "tui_gateway/methods_config_set.py",
        "request": repo / "tui_gateway/model_switch.py",
    }
    parser = {"__name__": __name__, "dataclass": dataclass, "re": re}

    def isolated_import(name, *args, **kwargs):
        if name == "hermes_cli.model_switch":
            return SimpleNamespace(**parser)
        raise RuntimeError(f"lab forbids import: {name}")

    safe_builtins = vars(builtins) | {"__import__": isolated_import}
    parser["__builtins__"] = safe_builtins
    extract(
        files["parser"],
        {
            "ModelFlagParseResult",
            "ModelSwitchRequest",
            "_BOOL_FLAGS",
            "_VALUE_FLAGS",
            "MODEL_SWITCH_ERR_ONCE_WITH_GLOBAL",
            "MODEL_SWITCH_ERR_ONCE_REQUIRES_TARGET",
            "MODEL_SWITCH_ERR_BAD_REASONING",
            "MODEL_SWITCH_ERROR_TEXT",
            "parse_model_flags_detailed",
            "parse_model_switch_args",
            "resolve_persist_behavior",
        },
        parser,
    )
    calls = []

    def apply(sid, session, raw, **kwargs):
        calls.append({"session_id": sid, "raw": raw})
        return {
            "value": "model-b",
            "warning": "",
            "scope": "session",
            "confirm_required": False,
        }

    scope = {
        "__builtins__": safe_builtins,
        "_ok": lambda rid, result: {"result": result},
        "_err": lambda rid, code, message: {"error": {"code": code}},
        "_apply_model_switch": apply,
        "_session_profile_runtime_scope": lambda session: contextlib.nullcontext(),
        "_pending_switch_selection_warning": lambda *args: None,
    }
    extract(
        files["setter"],
        {"_kv", "_cfgset_model_ok", "_stash_pending_model_switch", "_set_model"},
        scope,
    )
    extract(files["request"], {"_switch_request"}, scope)
    raw = "model-b --provider provider-b --session"
    params = {"session_id": "owned-live", "key": "model", "value": raw}
    results = {}
    for label, flags in (
        ("idle", {}),
        ("busy", {"running": True}),
        ("compute_host_idle", {"_compute_host_active": True}),
    ):
        session = {"agent": SimpleNamespace(provider="provider-a"), **flags}
        before = len(calls)
        response = scope["_set_model"]("lab", params, "model", raw, session)
        results[label] = {
            "response": response,
            "applied": len(calls) != before,
            "pending": "pending_model_switch" in session,
        }
    check(results["idle"]["applied"] and not results["idle"]["pending"], "idle switch changed")
    for label in ("busy", "compute_host_idle"):
        check(
            not results[label]["applied"] and results[label]["pending"], f"{label} switch changed"
        )
        check(results[label]["response"]["result"]["deferred"] is True, "deferral not reported")
    scope["_pending_switch_selection_warning"] = lambda *args: "synthetic guard"
    guarded = {"agent": SimpleNamespace(provider="provider-a"), "running": True}
    response = scope["_set_model"]("lab", params, "model", raw, guarded)
    check(response["result"]["confirm_required"] is True, "confirmation missing")
    check("pending_model_switch" not in guarded, "unconfirmed switch queued")
    results["confirmation"] = {"required": True, "pending": False}
    response = scope["_set_model"]("lab", params, "model", raw, None)
    check(response["error"]["code"] == 4001, "sessionless switch accepted")
    results["sessionless"] = response
    # Even explicit --global cannot override an explicit session-scoped request.
    normalized = scope["_switch_request"](raw + " --global", None, None)
    check(normalized == ("model-b", "provider-b", False, False, ""), "session scope did not win")
    results["session_scope_prevents_global_persistence"] = True
    return {
        "kind": "installed_upstream_ast_handlers_with_synthetic_hooks",
        "source_sha256": {
            path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files.values()
        },
        "results": results,
        "live_desktop_test": False,
        "provider_resolution_tested": False,
        "model_or_tools_called": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(run(args.hermes_repo), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
