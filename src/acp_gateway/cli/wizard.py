"""Interactive onboarding. Secrets stay in private files and out of summaries."""

from __future__ import annotations

import asyncio
import getpass
import io
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from dotenv import dotenv_values
from pydantic import ValidationError

from acp_gateway import paths
from acp_gateway.cli.setup_asyncio import run as run_setup
from acp_gateway.cli.setup_sections import SECTIONS
from acp_gateway.config import (
    AgentProfile,
    AppConfig,
    LoggingSettings,
    PolicySettings,
    RateLimitSettings,
    RetentionSettings,
    SecretStore,
    Settings,
    UpdateSettings,
)
from acp_gateway.connectors.control import normalize_pin, read_credential, validate_url
from acp_gateway.connectors.launch import ConnectorLaunch
from acp_gateway.log import register_secret
from acp_gateway.setup_state import STATE
from acp_gateway.setup_state import parse as _parse_state


class Prompts:
    def ask(self, label, default="", validate=None, *, secret=False):
        while True:
            suffix = (
                " [keep existing]" if secret and default else f" [{default}]" if default else ""
            )
            value = (getpass.getpass if secret else input)(label + suffix + ": ").strip()
            value = value or default
            if secret and value:
                register_secret(value)
            try:
                if any(ord(c) < 32 or ord(c) == 127 for c in value):
                    raise ValueError("Control characters are not allowed")
                return validate(value) if validate else value
            except (OSError, ValueError, ValidationError):
                print("Invalid value; please try again.")

    def yes(self, label, default=False):
        return self.ask(label + " (yes/no)", "yes" if default else "no", _yes)

    def choice(self, label, choices, default):
        print(label + ":")
        for index, choice in enumerate(choices, 1):
            print(f"  {index}. {choice}")
        return self.ask("Choose (number or name)", default, lambda v: _choice(v, choices))


def _yes(value):
    if value.lower() not in {"yes", "no", "y", "n"}:
        raise ValueError("Expected yes/no")
    return value.lower() in {"yes", "y"}


def _choice(value, choices):
    if value.isdigit() and 1 <= int(value) <= len(choices):
        return choices[int(value) - 1]
    if value not in choices:
        raise ValueError("Choose a listed value")
    return value


def _required(value):
    if not value:
        raise ValueError("Value is required")
    return value


def _working_directory(value):
    path = Path(_required(value)).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("Choose an existing absolute directory on this agent computer")
    return str(path)


def _identifier(value):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("Invalid computer ID")
    return value


def _alias(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value):
        raise ValueError("Invalid alias")
    return value


def _port(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise ValueError("Invalid port")
    return number


def _ids(value):
    values = [int(part.strip()) for part in value.split(",") if part.strip()]
    if not values or any(v <= 0 for v in values) or len(set(values)) != len(values):
        raise ValueError("Enter unique positive Telegram user IDs")
    return values


def _credential_path(value):
    path = Path(_required(value)).expanduser().absolute()
    if sys.platform == "win32":
        from acp_gateway.windows_acl import check_private_paths

        check_private_paths([path], directories=[path.parent], purpose="Connector credential")
    read_credential(path)
    return str(path)


def _url(value):
    validate_url(value)
    return value


def _snapshot(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Expected a regular file: {path}")
    if sys.platform == "win32" and path.exists():
        from acp_gateway.windows_acl import check_private_paths

        check_private_paths([path], directories=[path.parent], purpose="Gateway setup")
    return path.read_bytes() if path.exists() else None


def _yaml(contents):
    try:
        value = yaml.safe_load(contents or b"{}") or {}
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (yaml.YAMLError, ValueError):
        raise ValueError("Expected a valid YAML mapping; repair the configuration first") from None


def _base_url(settings):
    host = settings.gateway.host
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    return f"http://{host}:{settings.gateway.port}"


def _state(contents):
    return _parse_state(contents)


def _atomic(path, content):
    if sys.platform == "win32":
        from acp_gateway.windows_acl import check_private_paths, ensure_private_directory

        ensure_private_directory(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".acpgw-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if sys.platform == "win32":
                # Verify inherited file access before writing any secret bytes.
                check_private_paths([temporary], purpose="Gateway setup")
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_files(files, originals, directory):
    from filelock import FileLock

    # Share writer locks with remote profile administration.
    with ExitStack() as locks:
        for path in sorted(files, key=str):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            locks.enter_context(FileLock(str(path) + ".profiles.lock", timeout=0, mode=0o600))
        return _save_files(files, originals, directory)


def _save_files(files, originals, directory):
    """Refuse concurrent edits; keep private backups and restore partial writes."""
    if any(_snapshot(path) != originals[path] for path in files):
        raise ValueError("Configuration changed during setup; restart the wizard")
    backup = None
    if any(value is not None for value in originals.values()):
        parent = directory / "setup-backups"
        if sys.platform == "win32":
            from acp_gateway.windows_acl import ensure_private_directory

            ensure_private_directory(parent)
        else:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = Path(tempfile.mkdtemp(prefix="settings-", dir=parent))
        for index, value in enumerate(originals.values()):
            if value is not None:
                _atomic(backup / str(index), value)
        _atomic(backup / "manifest.json", json.dumps([str(p) for p in originals]).encode())
    written = []
    try:
        for path, content in files.items():
            _atomic(path, content)
            written.append(path)
    except BaseException:
        for path in reversed(written):
            value = originals[path]
            if value is None:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, value)
        raise
    return backup


def _env_contents(original, values):
    lines = (original or b"").decode("utf-8").splitlines()
    for key, value in values.items():
        pattern = re.compile(r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=")
        lines = [line for line in lines if not pattern.match(line)]
        lines.append(key + "=" + json.dumps(value, ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode()


def _linux_path(value):
    from acp_gateway.config import _wsl_path

    return _wsl_path(value)


def _managed_environment(prompts, old, backend, environment):
    from acp_gateway.config import WslSettings

    settings = old.copy()
    previous = settings.get("wsl")
    if environment == "native":
        settings.pop("wsl", None)
        if previous is not None:
            settings["executable"] = "codex" if backend == "codex" else "claude"
        return settings
    values = dict(previous or {})

    def field(name, value):
        candidate = {"distribution": "distribution", "user": "user", **values, name: value}
        return WslSettings.model_validate(candidate).model_dump(mode="json")[name]

    for name, label in (
        ("distribution", "WSL distribution (explicit name)"),
        ("user", "WSL Linux user (non-root)"),
        ("python", "WSL Python (absolute Linux path)"),
    ):
        values[name] = prompts.ask(
            label,
            values.get(name, "/usr/bin/python3" if name == "python" else ""),
            lambda value, name=name: field(name, value),
        )
    values["agent_home"] = prompts.ask(
        "WSL agent home (none uses target default)",
        values.get("agent_home") or "none",
        lambda value: field("agent_home", None if value == "none" else value),
    )
    values["path"] = prompts.ask(
        "WSL PATH directories (comma-separated; empty uses explicit executable only)",
        ",".join(values.get("path", [])),
        lambda value: field("path", [v.strip() for v in value.split(",") if v.strip()]),
    )
    settings["wsl"] = WslSettings.model_validate(values).model_dump(mode="json", exclude_none=True)
    if previous is None:
        settings["executable"] = ""
    print(
        f"WSL {values['distribution']}/{values['user']}: syntax only; target-side executable/cwd "
        "validation runs under this explicit identity at runtime. No distribution/account "
        "discovery, install or login is performed."
    )
    return settings


def _managed_mode(prompts, settings, settings_type, explicit):
    previous = settings["mode"]
    mode = prompts.choice("Agent permission profile", ("safe", "work"), previous)
    if mode != previous:
        new_defaults = settings_type(mode=mode).model_dump()
        for name in ("turn_timeout", "max_turns", "sandbox"):
            if name in settings and name not in explicit:
                settings[name] = new_defaults[name]
    settings["mode"] = mode
    if mode == "work":
        print("Work profile: native automatic approvals and your agent settings are enabled.")


def _codex(prompts, old):
    from acp_gateway.config import CodexSettings

    initial = dict(old)
    pending_executable = initial.pop("executable", None)
    settings = CodexSettings.model_validate(
        {k: v for k, v in initial.items() if k != "wsl"}
    ).model_dump(mode="json")
    settings["wsl"] = initial.get("wsl")
    if pending_executable is not None:
        settings["executable"] = pending_executable

    def field(name, value):
        return CodexSettings.model_validate({**settings, name: value}).model_dump(mode="json")[name]

    settings["executable"] = prompts.ask(
        "Codex Linux executable (absolute POSIX path)"
        if settings.get("wsl") is not None
        else "Codex executable name or absolute path",
        settings["executable"],
        lambda value: field("executable", value),
    )
    settings["model"] = prompts.ask(
        "Codex model (none uses CLI default)",
        settings["model"] or "none",
        lambda value: field("model", None if value == "none" else value),
    )
    _managed_mode(prompts, settings, CodexSettings, initial)
    print("Codex read-only is the conservative default; workspace-write permits project edits.")
    settings["sandbox"] = prompts.choice(
        "Codex sandbox", ("read-only", "workspace-write"), settings["sandbox"]
    )
    if settings.get("wsl") is None and not shutil.which(settings["executable"]):
        print(
            "Codex executable not found in this OS. Install Codex separately if needed, "
            "add it to PATH or select an existing absolute path; setup does not install it."
        )
    print(
        "Codex uses its own CLI login on this computer. Complete login separately if needed; "
        "setup does not read or change accounts. No model call is made."
    )
    return CodexSettings.model_validate(settings).model_dump(mode="json", exclude_none=True)


def _claude_code(prompts, old):
    from acp_gateway.config import ClaudeCodeSettings

    initial = dict(old)
    pending_executable = initial.pop("executable", None)
    settings = ClaudeCodeSettings.model_validate(
        {k: v for k, v in initial.items() if k != "wsl"}
    ).model_dump(mode="json")
    settings["wsl"] = initial.get("wsl")
    if pending_executable is not None:
        settings["executable"] = pending_executable

    def field(name, value):
        return ClaudeCodeSettings.model_validate({**settings, name: value}).model_dump(mode="json")[
            name
        ]

    settings["executable"] = prompts.ask(
        "Claude Code Linux executable (absolute POSIX path)"
        if settings.get("wsl") is not None
        else "Claude Code executable name or absolute path",
        settings["executable"],
        lambda value: field("executable", value),
    )
    settings["model"] = prompts.ask(
        "Claude Code model (none uses CLI default)",
        settings["model"] or "none",
        lambda value: field("model", None if value == "none" else value),
    )
    _managed_mode(prompts, settings, ClaudeCodeSettings, initial)
    if settings.get("wsl") is None and not shutil.which(settings["executable"]):
        print(
            "Claude Code executable not found in this OS. Install it separately if needed, "
            "add it to PATH or select an existing absolute path; setup does not install it."
        )
    print(
        "Claude Code uses its own CLI login on this computer. Complete login separately if needed; "
        "setup does not read or change accounts. No model call is made."
    )
    print("Native manual permissions are not an OS sandbox and do not guarantee read-only access.")
    return ClaudeCodeSettings.model_validate(settings).model_dump(mode="json", exclude_none=True)


def _agent(prompts, values, *, routed=False, old=None):
    old = old or {}
    result = old.copy()
    default_kind = old.get("kind", "goose")
    if (
        not routed
        and default_kind in {"codex", "claude_code"}
        and (old.get(default_kind) or {}).get("wsl") is not None
    ):
        default_kind += "-wsl"
    if not routed:
        print("Codex/Claude: plain agent type = native (default); -wsl = explicit WSL environment.")
    selected = prompts.choice(
        "Agent type",
        ("goose", "hermes", "generic", "codex", "claude_code")
        + (() if routed else ("codex-wsl", "claude_code-wsl")),
        default_kind,
    )
    environment = "wsl" if selected.endswith("-wsl") else "native"
    kind = selected.removesuffix("-wsl")
    if old.get("kind") != kind:
        result.pop("session_mode", None)
    default_alias = (
        "claude" if kind == "claude_code" else kind if kind in {"hermes", "codex"} else "goose"
    )
    managed_settings = None
    if not routed and kind in {"codex", "claude_code"}:
        managed_settings = _managed_environment(
            prompts, (old.get(kind) or {}) if old.get("kind") == kind else {}, kind, environment
        )
    wsl = managed_settings is not None and managed_settings.get("wsl") is not None
    print("Working directory is the real project folder used for agent sessions.")
    if routed:
        print("Legacy route: use the exact directory configured on the remote computer.")
    elif wsl:
        print("Choose a Linux cwd in the explicit WSL distribution/user; not checked on this host.")
    else:
        print("Choose an existing folder here; this does not sandbox agent file access.")
    result.update(
        {
            "alias": prompts.ask("Agent alias", old.get("alias", default_alias), _alias),
            "title": prompts.ask(
                "Display name",
                old.get("title") or ("Claude Code" if kind == "claude_code" else kind.capitalize()),
            ),
            "kind": kind,
            "default_cwd": prompts.ask(
                "Linux working directory in WSL"
                if wsl
                else "Working directory on the agent computer",
                old.get("default_cwd", "" if wsl else str(Path.home())),
                _required if routed else _linux_path if wsl else _working_directory,
            ),
        }
    )
    # Switching connection type must not retain the former transport fields.
    for key in ("backend", "computer_id", "hermes", "codex", "claude_code"):
        result.pop(key, None)
    if routed:
        for key in ("url", "secret_env", "tls_fingerprint", "allow_insecure_transport"):
            result.pop(key, None)
        result.update(
            backend="connector",
            computer_id=prompts.ask("Computer ID", old.get("computer_id", "home"), _identifier),
        )
    elif result["kind"] == "claude_code":
        for key in (
            "url",
            "secret_env",
            "tls_fingerprint",
            "allow_insecure_transport",
            "session_mode",
        ):
            result.pop(key, None)
        result.update(backend="claude_code", claude_code=_claude_code(prompts, managed_settings))
        return AgentProfile.model_validate(result).model_dump(mode="json", exclude_none=True)
    elif result["kind"] == "codex":
        for key in (
            "url",
            "secret_env",
            "tls_fingerprint",
            "allow_insecure_transport",
            "session_mode",
        ):
            result.pop(key, None)
        result.update(backend="codex", codex=_codex(prompts, managed_settings))
        return AgentProfile.model_validate(result).model_dump(mode="json", exclude_none=True)
    elif result["kind"] == "hermes":
        from acp_gateway.cli.hermes_setup import select

        for key in (
            "url",
            "secret_env",
            "tls_fingerprint",
            "allow_insecure_transport",
            "session_mode",
        ):
            result.pop(key, None)
        result.update(backend="hermes", hermes=select(prompts, old.get("hermes") or {}))
        return AgentProfile.model_validate(result).model_dump(mode="json", exclude_none=True)
    else:
        if old.get("kind") != result["kind"]:
            result.pop("session_mode", None)
        if result["kind"] == "goose" and prompts.yes("Prepare a managed local Goose listener?"):
            port = prompts.ask("Local Goose port", "3284", _port)
            name = old.get("secret_env") or "AGENT_" + result["alias"].upper() + "_SECRET"
            values.setdefault(name, secrets.token_urlsafe(32))
            result.update(
                url=f"ws://127.0.0.1:{port}/acp", secret_env=name, allow_insecure_transport=False
            )
            result.pop("tls_fingerprint", None)
            print(
                "A private agent key was prepared. After saving, use agents provider and "
                f"agents serve {result['alias']} (or agents service {result['alias']} install)."
            )
            return AgentProfile.model_validate(result).model_dump(mode="json", exclude_none=True)
        while True:
            result["url"] = prompts.ask(
                "ACP URL", old.get("url") or "ws://127.0.0.1:3284/acp", _required
            )
            name = old.get("secret_env") or "AGENT_" + result["alias"].upper() + "_SECRET"
            secret = prompts.ask("Agent secret (empty if none)", values.get(name, ""), secret=True)
            result["secret_env"] = name if secret else None
            if secret:
                values[name] = secret
            if urlsplit(result["url"]).scheme in {"wss", "https"}:
                result["tls_fingerprint"] = (
                    prompts.ask(
                        "Known SHA-256 certificate pin (empty for TOFU)",
                        old.get("tls_fingerprint") or "",
                    )
                    or None
                )
            else:
                result.pop("tls_fingerprint", None)
            try:
                AgentProfile.model_validate(result)
                break
            except ValidationError:
                print(
                    "Invalid agent transport or pin. Use TLS outside loopback "
                    "and enter credentials in the hidden field."
                )
    if old.get("session_mode") and old.get("kind") == result["kind"]:
        result["session_mode"] = old["session_mode"]
    return AgentProfile.model_validate(result).model_dump(mode="json", exclude_none=True)


def _profiles(prompts, raw, values, role, *, allow_empty=False):
    agents = list(raw.get("agents", []))
    if agents:
        print("Existing agents: " + ", ".join(a["alias"] for a in agents))
        action = prompts.choice(
            "Agent profiles", ("keep", "edit", "add", "remove", "replace"), "keep"
        )
    else:
        action = "add"
    if action == "edit":
        for index, agent in enumerate(agents):
            if prompts.yes("Edit " + agent["alias"] + "?"):
                while True:
                    profile = _agent(
                        prompts, values, routed=agent.get("backend") == "connector", old=agent
                    )
                    address = AgentProfile.model_validate(profile).address
                    if any(
                        i != index and AgentProfile.model_validate(other).address == address
                        for i, other in enumerate(agents)
                    ):
                        print("That address already exists; choose another alias or computer ID.")
                        continue
                    agents[index] = profile
                    break
    if action == "replace":
        agents = []
        action = "add"
    if action == "remove":
        address = prompts.choice(
            "Remove agent",
            tuple(AgentProfile.model_validate(a).address for a in agents),
            AgentProfile.model_validate(agents[0]).address,
        )
        if len(agents) == 1 and not allow_empty:
            raise ValueError("Keep at least one agent; use replace to change the only profile")
        if prompts.yes("Remove " + address + "? (saved secrets are kept)"):
            agents = [a for a in agents if AgentProfile.model_validate(a).address != address]
    if action == "add":
        while True:
            profile = _agent(prompts, values, routed=role == "server")
            if any(
                AgentProfile.model_validate(a).address
                == AgentProfile.model_validate(profile).address
                for a in agents
            ):
                print("That address already exists; choose another alias or computer ID.")
                continue
            agents.append(profile)
            backend = profile.get("backend")
            if backend in {"codex", "claude_code"} and prompts.yes(
                "Create both safe and work profiles?"
            ):
                import copy

                from acp_gateway.config import ClaudeCodeSettings, CodexSettings

                other = copy.deepcopy(profile)
                mode = "work" if profile[backend]["mode"] == "safe" else "safe"
                suffix = "_" + mode
                default_alias = profile["alias"][: 32 - len(suffix)] + suffix
                while True:
                    alias = prompts.ask("Second profile alias", default_alias, _alias)
                    if any(a["alias"] == alias for a in agents):
                        print("That alias already exists; choose another alias.")
                        continue
                    break
                other["alias"] = alias
                other["title"] = profile.get("title", profile["alias"]) + " (" + mode + ")"
                settings_type = CodexSettings if backend == "codex" else ClaudeCodeSettings
                defaults = settings_type(mode=profile[backend]["mode"]).model_dump()
                new = settings_type(mode=mode).model_dump()
                for name in ("turn_timeout", "max_turns", "sandbox"):
                    if name in other[backend] and other[backend][name] == defaults.get(name):
                        other[backend][name] = new[name]
                other[backend]["mode"] = mode
                agents.append(AgentProfile.model_validate(other).model_dump(exclude_none=True))
            if not prompts.yes("Add another agent?"):
                break
    if not agents and not allow_empty:
        raise ValueError("At least one agent profile is required")
    if role == "connector" and any(
        a.get("backend", "remote") not in {"remote", "hermes", "codex", "claude_code"}
        for a in agents
    ):
        raise ValueError(
            "A computer connector needs local ACP, Hermes, Codex or Claude Code profiles; "
            "choose replace to change role"
        )
    from acp_gateway.config import omit_default_idle_timeout

    raw["agents"] = [omit_default_idle_timeout(agent) for agent in agents]
    if any(a.get("backend") == "connector" for a in agents):
        raw.setdefault("connector", {})["enabled"] = True


def _connection(prompts, state, *, pairing_path=None):
    from acp_gateway.connectors.pairing_client import PairingClient

    method = prompts.choice(
        "Connection method", ("owner approval", "advanced credential file"), "owner approval"
    )
    if method == "advanced credential file":
        return _legacy_connection(prompts, state)
    if pairing_path is None:
        raise ValueError("a persistent pairing path is required")
    client = PairingClient(pairing_path)
    saved = client.load() if pairing_path.exists() else {}
    if saved:
        import time

        finished = (
            "connection" in saved
            or saved.get("request", {}).get("expires", float("inf")) <= time.time()
        )
        # A credential can already be issued while its ACK was lost. Replacing
        # that state would orphan the disabled computer and create a duplicate on
        # the next attempt, so activation must be resumed or explicitly aborted.
        unacknowledged = "issuance" in saved and "connection" not in saved
        if unacknowledged:
            print(
                "An issued credential has not been acknowledged yet. Resume to finish "
                "activation; do not start another pairing for this computer."
            )
            action = "resume"
        else:
            choices = ("resume", "start another") if finished else ("resume", "cancel")
            action = prompts.choice("Existing pairing", choices, "resume")
        if action == "start another":
            print(
                "The old binding and credential are kept. Revoke the old computer separately "
                "if it is no longer yours; switching endpoints is not a revoke."
            )
            if not prompts.yes("Archive this completed/expired pairing and start another?"):
                raise ValueError("Pairing kept unchanged")
            client.reset(confirm=True)
            saved = {}
        elif action == "cancel":
            run_setup(client.cancel(), operation="enrollment-cancel")
            raise ValueError("Pairing cancelled; rerun connect to create another request")
    old = state.get("connection", {})
    url = prompts.ask(
        "Server connection URL",
        saved.get("input_url", old.get("dispatcher_url", "")),
        lambda value: _url(value if "://" in value else "wss://" + value),
    )
    allow_ws = False
    if urlsplit(url).scheme == "ws":
        print(
            "WARNING: WS exposes applicant secrets, device credentials and conversations. "
            "Pairing does not encrypt them."
        )
        allow_ws = prompts.yes("I accept unencrypted WS credentials and conversations?")
        if not allow_ws:
            raise ValueError("Choose WSS instead; no automatic downgrade is allowed")
    owner = prompts.ask("Dispatcher owner ID", saved.get("owner_id", ""), _required)
    name = prompts.ask("Computer display name", saved.get("name", socket.gethostname()), _required)
    target = prompts.ask(
        "Previous computer ID to rebind (empty for new)",
        saved.get("target") or "",
        lambda value: _identifier(value) if value else None,
    )
    if target:
        print(
            "Rebinding preserves history but revokes previous credentials and connections. "
            "The owner must confirm this exact target."
        )
    print("Enrollment is durable; cancelling later setup does not undo an owner-approved binding.")
    request = run_setup(
        client.begin(url, owner, name, target=target, allow_ws=allow_ws),
        operation="enrollment-begin",
    )
    print(
        f"Request: {request['request_id']}\n"
        f"Compare: {request['verification']}\n"
        f"Target: {request['target'] or 'NEW COMPUTER'}\n"
        f"Expires: {request['expires']}"
    )
    print(
        "On the dispatcher use pairing list/approve, or /pairing in the owner's Telegram. "
        "Compare this exact request before approving."
    )
    while prompts.yes("Owner approved this exact request? Check now", True):
        result = run_setup(client.finish(), operation="enrollment-finish")
        if "computer_id" in result:
            print("Device binding active. Agent and autostart readiness are separate checks.")
            return result
        if result.get("status") != "pending":
            raise ValueError(
                "Pairing "
                + result.get("status", "unavailable")
                + "; cancel or inspect the request before retrying"
            )
        print("Approval pending; no device access has been granted.")
    raise ValueError("Approval pending; run acpgw connect again to resume this exact request")


def _legacy_connection(prompts, state):
    old = state.get("connection", {})
    print("Dispatcher URL example: ws://YOUR_SERVER_IP:18766/acpgw/connect.")
    print("Use the enrolled computer ID and its privately copied key file.")
    url = prompts.ask(
        "Server connection URL",
        old.get("dispatcher_url", ""),
        _url,
    )
    if urlsplit(url).scheme == "ws":
        print(
            "WS sends enrollment keys and conversations without encryption. "
            "Pairing does not protect plaintext transport."
        )
        if not prompts.yes("I accept unencrypted WS credentials and conversations?"):
            raise ValueError("Choose a WSS URL instead")
    connection = {
        "dispatcher_url": url,
        "computer_id": prompts.ask(
            "Computer ID registered on the server", old.get("computer_id", "home"), _identifier
        ),
        "token_file": prompts.ask(
            "Enrollment key file copied privately from the server",
            old.get("token_file", ""),
            _credential_path,
        ),
        "tls_fingerprint": None,
    }
    if urlsplit(url).scheme == "wss":
        connection["tls_fingerprint"] = prompts.ask(
            "Server SHA-256 pin (empty for normal CA validation)",
            old.get("tls_fingerprint") or "",
            lambda v: normalize_pin(v) if v else None,
        )
    return connection


def _pairing_owner_setup(prompts, cfg):
    from acp_gateway.pairing import PairingService
    from acp_gateway.storage.db import Store

    data = cfg.settings.resolved_data_dir()
    store = Store.open_in(data)
    try:
        pairing = PairingService(store, boundary=data / "pairing-boundary.json")
        exists = store._conn.execute("SELECT 1 FROM pairing_owner").fetchone()
        if exists:
            owner = pairing.owner("local")
        elif prompts.yes("Bootstrap connector pairing owner now?", True):
            print(
                "The Telegram allowlist is not owner authority. Explicitly choose "
                "pairing approvers; empty keeps local CLI only."
            )
            ids = prompts.ask(
                "Telegram pairing approver IDs (comma-separated; empty means local CLI only)",
                "",
                lambda value: _ids(value) if value else [],
            )
            if cfg.settings.telegram.enabled and any(
                i not in cfg.settings.telegram.allowed_user_ids for i in ids
            ):
                raise ValueError("pairing approvers must also be on the Telegram allowlist")
            owner = pairing.bootstrap(ids)
        else:
            print(
                "Pairing owner not bootstrapped; use acpgw owner bootstrap locally "
                "before connecting PCs."
            )
            return
        print("Dispatcher owner ID: " + owner["owner_id"])
        print(
            "This public ID is not a password. Legacy rebind activation is separate "
            "and never automatic."
        )
    finally:
        store.close()


def _settings_section(prompts, raw, key, model):
    """Edit only this section; each answer is validated before accepting it."""
    from typing import Literal, get_args, get_origin

    values = model.model_validate(raw.get(key, {})).model_dump(mode="json")
    print("Use 'none' to clear an optional value; lists use comma-separated values.")
    for name, field in model.model_fields.items():
        old = values[name]
        label = key.replace("_", " ").capitalize() + ": " + name.replace("_", " ")
        if isinstance(old, bool):
            value = prompts.yes(label, old)
        elif get_origin(field.annotation) is Literal:
            value = prompts.choice(label, get_args(field.annotation), old)
        else:
            default = (
                ",".join(old) if isinstance(old, list) else str(old) if old is not None else "none"
            )

            def validate(answer, name=name, old=old, field=field):
                if answer.lower() == "none" and not isinstance(old, list):
                    value = None
                elif isinstance(old, list):
                    value = [v.strip() for v in answer.split(",") if v.strip()]
                elif isinstance(old, int) or int in get_args(field.annotation):
                    value = int(answer)
                else:
                    value = answer
                candidate = model.model_validate({**values, name: value})
                return candidate.model_dump(mode="json")[name]

            value = prompts.ask(label, default, validate)
        values[name] = value
    raw[key] = model.model_validate(values).model_dump(mode="json", exclude_none=True)


def _status_interval(value):
    value = int(value)
    if not 1 <= value <= 60:
        raise ValueError("Telegram progress interval must be 1..60 seconds")
    return value


def _telegram(prompts, raw, values):
    section = raw.setdefault("telegram", {})
    section["enabled"] = prompts.yes("Enable Telegram?", section.get("enabled", False))
    if not section["enabled"]:
        return
    name = section.get("token_env", "TELEGRAM_BOT_TOKEN")
    values[name] = prompts.ask(
        "Telegram bot token from BotFather", values.get(name, ""), _required, secret=True
    )
    section["allowed_user_ids"] = prompts.ask(
        "Allowed Telegram user IDs, separated by commas",
        ",".join(str(v) for v in section.get("allowed_user_ids", [])),
        _ids,
    )
    section["status_enabled"] = prompts.yes(
        "Enable Telegram progress status?", section.get("status_enabled", True)
    )
    section["status_interval_seconds"] = prompts.ask(
        "Telegram progress interval (seconds)",
        str(section.get("status_interval_seconds", 2)),
        lambda value: _status_interval(value),
    )
    approvers = raw.setdefault("policy", {}).setdefault("approver_channels", ["cli"])
    if "telegram" not in approvers:
        approvers.append("telegram")
    print("Use one polling process for this bot. Existing webhooks are not deleted by setup.")


def _hermes(prompts, files, originals, settings, values):
    if not prompts.yes("Connect this gateway to a local Hermes installation?"):
        return
    home = (
        Path(prompts.ask("Hermes configuration directory", str(Path.home() / ".hermes"), _required))
        .expanduser()
        .absolute()
    )
    config_file, env_file = home / "config.yaml", home / ".env"
    if config_file in files or env_file in files:
        raise ValueError("Hermes and Gateway need different configuration files")
    originals[config_file] = _snapshot(config_file)
    originals[env_file] = _snapshot(env_file)
    if originals[config_file] is None:
        raise ValueError("Hermes config.yaml is missing; finish Hermes setup first")
    raw = _yaml(originals[config_file])
    raw.setdefault("mcp_servers", {})["acpgw"] = {
        "url": _base_url(settings) + "/mcp",
        "headers": {"Authorization": "Bearer $" + "{ACPGW_MCP_TOKEN}"},
        "tool_timeout": 45,
    }
    files[config_file] = yaml.safe_dump(raw, sort_keys=False).encode()
    files[env_file] = _env_contents(
        originals[env_file], {"ACPGW_MCP_TOKEN": values[settings.gateway.mcp_token_env]}
    )
    print("Hermes receives only the MCP token. Restart Hermes after saving to discover the tools.")
    return home


def _managed_running(config_file, role):
    if sys.platform == "win32" and role == "connector":
        from acp_gateway.windows_service import _request

        task = _request("status")
        return task["installed"] and (task["enabled"] or task["state"] in {2, 4})
    if sys.platform != "linux":
        return False
    from acp_gateway.service import _marker, _quote, _systemctl, unit_path

    unit = unit_path(role)
    if not unit.is_file():
        return False
    contents = unit.read_text()
    if not contents.startswith(_marker(role)) or _quote(str(config_file)) not in contents:
        return False
    try:
        return "ActiveState=active" in _systemctl("show", unit.name, "--property=ActiveState")
    except ValueError:
        return False


def _ensure_idle(config_file, env_file):
    from acp_gateway.config import load_config

    cfg = load_config(config_file, env_file)
    token = cfg.secrets.get(cfg.settings.gateway.api_token_env)
    try:
        response = httpx.get(
            _base_url(cfg.settings) + "/health",
            headers={"Authorization": "Bearer " + (token.get_secret_value() if token else "")},
            timeout=5,
            trust_env=False,
        )
        response.raise_for_status()
        if response.json()["running_jobs"]:
            raise ValueError("Finish or cancel running jobs before restarting the gateway")
    except (httpx.HTTPError, KeyError):
        raise ValueError(
            "Cannot confirm the gateway is idle; stop it explicitly before setup"
        ) from None


async def _check_agents(cfg):
    from acp_gateway.agents.client import AgentClient

    for profile in cfg.settings.agents:
        if profile.backend == "connector":
            print(f"{profile.address}: routed agent; check after its computer connects")
            continue
        if profile.backend == "hermes":
            from acp_gateway.cli.hermes_setup import inspect_async
            from acp_gateway.discovery.hermes import default_lock_dir

            attach = profile.hermes
            row = (
                await inspect_async(
                    attach.lock_dir or default_lock_dir(), role=attach.role, profile=attach.profile
                )
            )[0]
            print(
                f"{profile.address}: Hermes {row['status']} "
                "(observer check; no session or prompt sent)"
            )
            continue
        client = AgentClient(
            profile,
            cfg.secrets.get(profile.secret_env) if profile.secret_env else None,
            pin_dir=cfg.settings.resolved_data_dir() / "pins",
            open_timeout=5,
        )
        try:
            await asyncio.wait_for(client.connect(), 12)
            if profile.backend == "claude_code":
                print(
                    f"{profile.address}: Claude Code initialize passed "
                    "(no session or model call made)"
                )
                print("CLI login and model availability are not verified by initialization.")
            elif profile.backend == "codex":
                print(f"{profile.address}: Codex initialize passed (no thread or model call made)")
                print("CLI login and model availability are not verified by initialization.")
            else:
                print(f"{profile.address}: ACP initialize passed (no session or prompt sent)")
        except Exception:
            managed = (
                profile.claude_code
                if profile.backend == "claude_code"
                else profile.codex
                if profile.backend == "codex"
                else None
            )
            wsl = getattr(managed, "wsl", None)
            where = (
                f"in WSL {wsl.distribution}/{wsl.user}" if wsl is not None else "on this computer"
            )
            if profile.backend == "claude_code":
                print(
                    f"{profile.address}: Claude Code initialize failed; "
                    "verify the executable name/PATH "
                    f"or absolute path and working directory {where}. Check CLI "
                    "installation and login separately; setup does not install or change accounts."
                )
            elif profile.backend == "codex":
                print(
                    f"{profile.address}: Codex initialize failed; verify the executable name/PATH "
                    f"or absolute path and working directory {where}. Check Codex CLI "
                    "installation and login separately; setup does not install or change accounts."
                )
            else:
                print(f"{profile.address}: ACP check failed; verify listener, secret and TLS")
        finally:
            await client.close()


def check_connections(cfg, state):
    run_setup(_check_agents(cfg), operation="checks")
    if state["role"] == "server":
        print("Remote agents are discovered automatically; inspect acpgw computers status.")
    if state["role"] == "connector" and not state.get("connection"):
        print("Server connection is not configured; choose connection in acpgw setup.")
    elif state["role"] == "connector":
        parts = validate_url(state["connection"]["dispatcher_url"])
        try:
            with socket.create_connection(
                (parts.hostname, parts.port or (443 if parts.scheme == "wss" else 80)), timeout=5
            ):
                pass
            print(
                "Server TCP reachable. Credentials and TLS are checked when the connector starts."
            )
        except OSError:
            print("Server TCP unreachable; verify URL, port and firewall.")
    if cfg.settings.telegram.enabled:
        secret = cfg.secrets.get(cfg.settings.telegram.token_env)
        try:
            response = httpx.get(
                "https://api.telegram.org/bot" + secret.get_secret_value() + "/getMe", timeout=10
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                raise ValueError
            print("Telegram getMe passed (no message sent).")
        except (httpx.HTTPError, ValueError, AttributeError):
            print("Telegram token check failed; verify token and network.")


def _enroll(prompts, cfg, directory):
    if not any(a.backend == "connector" for a in cfg.settings.agents):
        return
    if not prompts.yes("Export enrollment keys for new configured computers?", True):
        return
    from acp_gateway.storage.db import Store

    store = Store.open_in(cfg.settings.resolved_data_dir())
    try:
        known = {c["computer_id"] for c in store.computers.list()}
        for identity in sorted(
            {a.computer_id for a in cfg.settings.agents if a.backend == "connector"}
        ):
            if identity in known:
                print(f"{identity}: already enrolled; existing key kept")
                continue
            target = directory / "enrollment"
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = target / (identity + ".key")
            store.computers.issue(identity, path, display_name=identity)
            print(f"{identity}: copy {path} privately to that computer, then run acpgw setup there")
    finally:
        store.close()


def _check_ports(settings):
    """Probe before committing settings, after managed services have stopped."""
    listeners = [(settings.gateway.host, settings.gateway.port)]
    if settings.connector.enabled:
        listeners.append((settings.connector.host, settings.connector.port))
    for host, port in listeners:
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            for family, kind, protocol, _, address in addresses:
                with socket.socket(family, kind, protocol) as probe:
                    probe.bind(address)
        except OSError:
            raise ValueError(
                f"Cannot bind {host}:{port}; choose another port or stop its current listener"
            ) from None


def _windows_connector_defaults(raw, config_file, *, existing=False):
    if raw.get("data_dir") is None:
        raw["data_dir"] = str(paths.data_dir() if existing else config_file.parent / "data")
    logging = raw.setdefault("logging", {})
    if not isinstance(logging, dict):
        raise ValueError("Windows connector logging must be a mapping")
    if logging.get("file") is None:
        logging["file"] = str(config_file.parent / "logs" / "connector.log")
    values = (raw["data_dir"], logging["file"])
    if any(not isinstance(value, (str, Path)) or not Path(value).is_absolute() for value in values):
        raise ValueError("Windows connector data_dir and logging.file must be absolute")


def _prepare_windows_connector(cfg, launch):
    from acp_gateway.windows_acl import check_private_paths, ensure_private_directory

    if launch is not None:
        check_private_paths(
            [launch.token_file],
            directories=[launch.token_file.parent],
            purpose="Connector credential",
        )
    log = cfg.settings.logging.file
    ensure_private_directory(cfg.settings.resolved_data_dir())
    ensure_private_directory(log.parent)
    if log.exists() or log.is_symlink():
        check_private_paths([log], purpose="Connector log")


def wizard(
    directory=None, *, config_file=None, env_file=None, prompts=None, section="all", role=None
):
    if (
        sys.platform == "win32"
        and section != "connection"
        and _managed_running(config_file, "connector")
    ):
        raise ValueError(
            "Disable the connector task before editing settings: "
            "acpgw service --role connector disable"
        )
    prompts = prompts or Prompts()
    directory = (
        (directory or (config_file.parent if config_file else paths.config_dir()))
        .expanduser()
        .absolute()
    )
    config_file = (config_file or directory / "config.yaml").expanduser().absolute()
    env_file = (env_file or directory / ".env").expanduser().absolute()
    state_file = config_file.parent / STATE
    originals = {p: _snapshot(p) for p in (config_file, env_file, state_file)}
    raw = _yaml(originals[config_file])
    if len(originals) != 3:
        raise ValueError("Configuration, secrets and setup state need distinct paths")
    values = {
        key: value
        for key, value in dotenv_values(
            stream=io.StringIO((originals[env_file] or b"").decode()), interpolate=False
        ).items()
        if value
    }
    for value in values.values():
        register_secret(value)
    previous = _state(originals[state_file])
    if section not in SECTIONS:
        raise ValueError("Unknown configuration section")
    if section not in {"all", "base"} and not originals[config_file]:
        raise ValueError("Run acpgw setup first before editing an individual section")
    print("ACP Gateway setup. Ctrl+C cancels. Secrets are hidden; existing keys are kept.")
    print(
        "local: gateway to ACP agents; server: gateway for remote computers; "
        "connector: connect this computer to a server."
    )
    saved_role = previous.get("role", "local")
    role = (
        (role or prompts.choice("Role", ("local", "server", "connector"), saved_role))
        if section in {"all", "base"}
        else saved_role
    )
    if section == "connection":
        if saved_role == "server":
            raise ValueError(
                "Outgoing connection belongs on a connector; server uses gateway ingress"
            )
        role = "connector"
    if role not in {"local", "server", "connector"}:
        raise ValueError("Unknown setup role")
    if role == "connector" and section in {"gateway", "telegram", "hermes"}:
        raise ValueError(
            "Configure API, Telegram and Hermes on the gateway/server host; "
            "this host is an outgoing connector"
        )
    if role is not None and section not in {"all", "base", "connection"} and role != saved_role:
        raise ValueError("Choose base setup to change this installation's role")
    state = {**previous, "version": 1, "role": role, "env_file": str(env_file)}
    if role == "connector":
        raw.setdefault("connector", {})["enabled"] = False
    if role != "connector":
        state.pop("connection", None)
    if section in {"policy", "rate-limits", "retention", "logging", "updates"}:
        key, model = {
            "policy": ("policy", PolicySettings),
            "rate-limits": ("rate_limits", RateLimitSettings),
            "retention": ("retention", RetentionSettings),
            "logging": ("logging", LoggingSettings),
            "updates": ("updates", UpdateSettings),
        }[section]
        _settings_section(prompts, raw, key, model)
    gateway = raw.setdefault("gateway", {})
    if role != "connector" and section in {"all", "base", "gateway"}:
        gateway["port"] = prompts.ask(
            "Local API/MCP port (loopback only)", str(gateway.get("port", 8765)), _port
        )
    if role == "server" and section in {"all", "base", "gateway"}:
        ingress = raw.setdefault("connector", {})
        ingress["enabled"] = True
        ingress["host"] = prompts.ask(
            "Connector listen address (127.0.0.1 behind nginx; 0.0.0.0 for IP test)",
            ingress.get("host", "127.0.0.1"),
            _required,
        )
        ingress["port"] = prompts.ask("Connector port", str(ingress.get("port", 18766)), _port)
        from acp_gateway.connectors.control import validate_connect_path

        ingress["connect_path"] = prompts.ask(
            "Connector URL path",
            ingress.get("connect_path", "/acpgw/connect"),
            validate_connect_path,
        )
        certificate = prompts.ask(
            "TLS certificate path (empty if nginx terminates TLS)",
            str(ingress.get("tls_cert") or ""),
        )
        ingress["tls_cert"] = (
            str(Path(certificate).expanduser().absolute()) if certificate else None
        )
        ingress["tls_key"] = (
            str(
                Path(
                    prompts.ask(
                        "TLS private key path", str(ingress.get("tls_key") or ""), _required
                    )
                )
                .expanduser()
                .absolute()
            )
            if certificate
            else None
        )
        if ingress["host"] not in {"127.0.0.1", "::1", "localhost"} and not certificate:
            print(
                "Public WS ingress is unencrypted. Use only for the IP test; "
                "configure WSS for regular use."
            )
            if not prompts.yes("Allow unencrypted public test ingress?"):
                raise ValueError("Choose loopback with nginx or provide TLS files")
    elif role == "connector" and section in {"all", "connection"}:
        raw.setdefault("connector", {})["enabled"] = False
        state["connection"] = _connection(
            prompts,
            previous,
            pairing_path=config_file.parent
            / ("." + config_file.name + ".pairing")
            / "pending.json",
        )
    if section in {"all", "agents"}:
        _profiles(prompts, raw, values, role, allow_empty=section == "agents")
    if (
        role == "local"
        and section == "all"
        and prompts.yes("Connect this computer to an external dispatcher now?")
    ):
        role = "connector"
        state["role"] = role
        state["connection"] = _connection(
            prompts,
            previous,
            pairing_path=config_file.parent
            / ("." + config_file.name + ".pairing")
            / "pending.json",
        )
        raw.setdefault("connector", {})["enabled"] = False
    if role == "connector" and section in {"all", "connection"} and state.get("connection"):
        connector = raw.setdefault("connector", {})
        connector["remote_profiles"] = prompts.yes(
            "Allow the dispatcher to manage profiles from locally discovered agents?",
            connector.get("remote_profiles", True),
        )
    if role != "connector" and section in {"all", "telegram"}:
        _telegram(prompts, raw, values)
    elif role == "connector":
        raw.setdefault("telegram", {})["enabled"] = False
    for name in (
        gateway.get("api_token_env", "ACPGW_API_TOKEN"),
        gateway.get("mcp_token_env", "ACPGW_MCP_TOKEN"),
    ):
        values.setdefault(name, secrets.token_urlsafe(32))
    if sys.platform == "win32" and role == "connector":
        _windows_connector_defaults(raw, config_file, existing=originals[config_file] is not None)
    settings = Settings.model_validate(raw)
    cfg = AppConfig(settings, SecretStore(values), config_file, env_file)
    from acp_gateway.daemon import owner_token

    owner_token(cfg)
    mcp = cfg.secrets.get(settings.gateway.mcp_token_env).get_secret_value()
    agent_secrets = [cfg.secrets.get(a.secret_env) for a in settings.agents if a.secret_env]
    if any(s and s.get_secret_value() == mcp for s in agent_secrets):
        raise ValueError("MCP and agent credentials must be different")
    for name in cfg.referenced_secret_names():
        if os.environ.get(name) and os.environ[name] != values.get(name):
            raise ValueError(f"Unset the environment override {name} before saving setup")
    if settings.connector.enabled and settings.connector.port == settings.gateway.port:
        raise ValueError("API and connector ingress need separate ports")
    if settings.connector.tls_cert:
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(settings.connector.tls_cert, settings.connector.tls_key)
    for value in values.values():
        register_secret(value)
    if cfg.missing_agent_secrets():
        raise ValueError("Some agent secrets are missing; edit the profiles before saving")
    launch = (
        ConnectorLaunch.from_args(
            ConnectorLaunch(
                **(state["connection"] | {"token_file": Path(state["connection"]["token_file"])})
            ),
            cfg,
        )
        if role == "connector" and state.get("connection") and settings.agents
        else None
    )
    files = {
        config_file: yaml.safe_dump(raw, sort_keys=False).encode(),
        env_file: _env_contents(originals[env_file], values),
        state_file: json.dumps(state, indent=2).encode(),
    }
    if (
        role != "connector"
        and section in {"all", "hermes"}
        and (hermes_home := _hermes(prompts, files, originals, settings, values))
    ):
        state["hermes_home"] = str(hermes_home)
        files[state_file] = json.dumps(state, indent=2).encode()
    service_role = "connector" if role == "connector" else "gateway"
    active_roles = [r for r in ("gateway", "connector") if _managed_running(config_file, r)]
    if active_roles and section != "all" and previous.get("role", "local") != role:
        raise ValueError("Stop the existing service explicitly before changing its role")
    start_service = False
    windows_task = None
    old_unit = None
    was_enabled = False
    if section == "all" and sys.platform == "linux" and shutil.which("systemctl"):
        start_service = prompts.yes(
            "Install and start the systemd user service?", bool(active_roles)
        )
        if start_service:
            from acp_gateway.service import _owned, _quote, _systemctl, unit_path

            unit = unit_path(service_role)
            if unit.exists() or unit.is_symlink():
                _owned(unit, service_role)
                if _quote(str(config_file)) not in unit.read_text():
                    raise ValueError(
                        "The existing service uses another configuration; "
                        "rerun with its --config or decline service installation"
                    )
            old_unit = _snapshot(unit)
            was_enabled = "UnitFileState=enabled" in _systemctl(
                "show", unit.name, "--property=UnitFileState", allow_not_found=True
            )
    elif section == "all" and sys.platform == "win32" and role == "connector":
        start_service = prompts.yes("Install and start the Windows connector logon task?", False)
        if start_service:
            from acp_gateway.windows_service import SetupTask

            windows_task = SetupTask(cfg)
    print(f"Save configuration: {config_file}\nPrivate secrets: {env_file}\nRole: {role}")
    print("Agents: " + ", ".join(a.address for a in settings.agents))
    print("Telegram: " + ("enabled" if settings.telegram.enabled else "disabled"))
    print("Autostart: " + (service_role if start_service else "not requested"))
    if active_roles:
        print("Running services will be stopped: " + ", ".join(active_roles))
    if launch:
        print(f"Server: {launch.dispatcher_url}; computer: {launch.computer_id}")
    if len(files) > 3:
        print("Hermes configuration and its MCP token will be updated; other servers are kept.")
    if not prompts.yes("Save these settings?", True):
        print("Cancelled; configuration and services were not changed.")
        return 0
    if role == "connector" and section == "connection":
        from acp_gateway.cli.connection_service import apply_connection_settings

        if sys.platform == "win32":
            _prepare_windows_connector(cfg, launch)
        backup, managed = apply_connection_settings(
            cfg, launch, files, originals, directory, prompts
        )
        print(
            "Connection saved; installed service arguments refreshed."
            if managed
            else "Connection saved; no managed connector service is installed."
        )
        if (
            not managed
            and launch is not None
            and prompts.yes("Install and start connector autostart now?", False)
        ):
            from acp_gateway.service import control, install

            if sys.platform == "win32":
                from acp_gateway.windows_service import SetupTask

                SetupTask(cfg).apply(launch)
            else:
                install(cfg, launch=launch)
                control("enable", role="connector")
        if prompts.yes("Check agent and connection readiness now (no model calls)?", True):
            check_connections(cfg, state)
        print(
            "Binding alone is not ready-agent acceptance. If an agent is missing/offline, "
            "run acpgw agents configure/probe and finish its own authorization/backend "
            "startup. No agent is installed silently."
        )
        print(
            "Start: acpgw --config "
            + str(config_file)
            + " --env-file "
            + str(env_file)
            + (
                " start. In Telegram select /agent, then explicitly /new; no active dialogue is "
                "changed by pairing."
            )
        )
        return 0
    if active_roles:
        if "gateway" in active_roles:
            _ensure_idle(config_file, env_file)
        if not prompts.yes("Stop the running service before applying settings?", True):
            print("Cancelled; configuration and services were not changed.")
            return 0
        from acp_gateway.service import control

        stopped = []
        try:
            for running_role in active_roles:
                control("stop", role=running_role)
                stopped.append(running_role)
        except BaseException:
            for running_role in stopped:
                control("start", role=running_role)
            raise
    try:
        if role != "connector":
            _check_ports(settings)
        elif sys.platform == "win32":
            _prepare_windows_connector(cfg, launch)
        backup = save_files(files, originals, directory)
    except BaseException:
        for running_role in active_roles:
            control("start", role=running_role)
        raise
    print("Saved private configuration." + (f" Previous files: {backup}" if backup else ""))
    if (
        role == "connector"
        and previous.get("connection")
        and previous.get("connection") != state.get("connection")
        and not start_service
    ):
        print(
            "IMPORTANT: the installed connector service/task still has its previous "
            "connection arguments. enable/start does not refresh them. Before starting "
            "it, reinstall with: "
            + (subprocess.list2cmdline if sys.platform == "win32" else shlex.join)(
                [
                    "acpgw",
                    "--config",
                    str(config_file),
                    "--env-file",
                    str(env_file),
                    "service",
                    "--role",
                    "connector",
                    "install",
                ]
            )
        )
    if windows_task is not None:
        try:
            windows_task.apply(launch)
        except BaseException:
            try:
                # Quiesce only this setup's task before restoring configuration.
                windows_task.restore()
                if any(_snapshot(path) != content for path, content in files.items()):
                    raise ValueError("Configuration changed after saving")
                for path, original in originals.items():
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic(path, original)
            except BaseException as recovery:
                raise ValueError(
                    "Windows task rollback failed; inspect service status and setup backups "
                    "before retrying. Settings were not fully restored."
                ) from recovery
            raise
        print("Windows connector task enabled; startup requested. Check VPS computers status.")
    elif start_service:
        from acp_gateway.service import control, install, unit_path

        unit = unit_path(service_role)
        try:
            install(cfg, launch=launch)
            control("enable", role=service_role)
        except BaseException:
            from acp_gateway.service import _systemctl

            # Undo a partial enable/start before restoring the previous unit and files.
            if unit.is_file():
                try:
                    control("stop", role=service_role)
                    if not was_enabled:
                        _systemctl("disable", unit.name)
                except (OSError, ValueError):
                    print("Service cleanup failed; restoring files. Inspect service status.")
            # Restore the selected unit and all configuration before restarting old services.
            if old_unit is not None:
                _atomic(unit, old_unit)
            else:
                unit.unlink(missing_ok=True)
            for path, original in originals.items():
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic(path, original)
            _systemctl("daemon-reload")
            if was_enabled:
                _systemctl("enable", unit.name)
            for running_role in active_roles:
                control("start", role=running_role)
            raise
    for running_role in active_roles:
        if running_role != service_role:
            control("disable", role=running_role)
        elif not start_service:
            try:
                control("start", role=running_role)
            except BaseException:
                # A section edit must not leave a formerly running service stopped.
                control("stop", role=running_role)
                for path, original in originals.items():
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic(path, original)
                control("start", role=running_role)
                raise
    if start_service or windows_task is not None:
        from acp_gateway.update_bootstrap import ensure_updates

        updater_role = "connector" if service_role == "connector" else "dispatcher"
        try:
            bootstrap = ensure_updates(cfg, role=updater_role)
            print(
                "Updater: "
                + ("ready" if bootstrap.ready else bootstrap.reason)
                + (f" ({bootstrap.policy})" if bootstrap.policy else "")
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # Normal service setup already succeeded; updater provisioning is
            # reported honestly and can be retried by the next application start.
            print("Updater not ready: " + str(exc))
    if role == "server" and section == "all":
        if settings.pairing.enabled:
            _pairing_owner_setup(prompts, cfg)
        _enroll(prompts, cfg, directory)
    if role != "server" and section == "all":
        from types import SimpleNamespace

        from acp_gateway.cli.executors import local_goose, run_bootstrap, service

        goose_profiles = [a for a in settings.agents if a.kind == "goose" and a.backend == "remote"]
        if goose_profiles:
            if prompts.yes("Install Goose CLI on this host now?"):
                run_bootstrap(
                    SimpleNamespace(agent_command="install", version=None, dry_run=False, yes=True)
                )
            if prompts.yes("Configure Goose model provider now?") and run_bootstrap(
                SimpleNamespace(agent_command="provider")
            ):
                raise ValueError("Goose provider configuration failed; retry acpgw agents provider")
            if sys.platform == "linux" and shutil.which("systemctl"):
                for profile in goose_profiles:
                    try:
                        local_goose(cfg, profile.alias)
                    except ValueError:
                        continue
                    if prompts.yes(
                        f"Install and start the local Goose service for {profile.alias}?"
                    ):
                        service(cfg, profile.alias, "install")
                        service(cfg, profile.alias, "enable")
    if section == "all" and prompts.yes(
        "Check connections now (no model prompts or messages)?", True
    ):
        check_connections(cfg, state)
    if role == "connector":
        print(
            "Binding alone is not ready-agent acceptance. Finish the agent's own "
            "authorization/backend startup if unavailable; use acpgw agents "
            "configure/probe. No agent is installed silently."
        )
        print(
            "After the connector starts, select its /agent route in Telegram and "
            "explicitly /new to create a dialogue. Pairing never switches an existing "
            "active dialogue."
        )
    command = ["acpgw", "--config", str(config_file), "--env-file", str(env_file), "start"]
    print("Run: " + shlex.join(command))
    if sys.platform == "win32" and role == "connector" and windows_task is None:
        print(
            "Windows data/log paths and private files are prepared. For autostart run "
            "acpgw service --role connector install; the saved connection is reused. "
            "See docs/setup/windows-connector.md."
        )
    elif sys.platform != "linux" and windows_task is None:
        print(
            "Use the foreground command above; native service setup is unavailable "
            "on this platform."
        )
    return 0


def start(config_file=None, env_file=None):
    from acp_gateway.cli.main import main
    from acp_gateway.config import find_config_file, find_env_file

    config_file = find_config_file(config_file)
    if config_file is None:
        raise ValueError("Run acpgw setup first")
    state_file = config_file.parent / STATE
    state = _state(_snapshot(state_file)) or {"role": "local"}
    service_role = "connector" if state.get("role") == "connector" else "gateway"
    if _managed_running(config_file.absolute(), service_role):
        raise ValueError(
            "The saved role is already running as a service; use acpgw menu for control"
        )
    args = ["--config", str(config_file)]
    env_file = find_env_file(env_file or config_file.parent / ".env")
    if env_file is not None:
        args += ["--env-file", str(env_file)]
    if state.get("role") == "connector":
        from acp_gateway.config import load_config

        cfg = load_config(config_file, env_file)
        launch = ConnectorLaunch.from_saved(cfg)
        args += launch.arguments()
    else:
        args += ["serve"]
    return main(args)
