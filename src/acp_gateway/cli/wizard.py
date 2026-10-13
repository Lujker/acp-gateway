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
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from dotenv import dotenv_values
from pydantic import ValidationError

from acp_gateway import paths
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings
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


def _agent(prompts, values, *, routed=False, old=None):
    old = old or {}
    result = old.copy()
    kind = prompts.choice("Agent type", ("goose", "hermes", "generic"), old.get("kind", "goose"))
    if old.get("kind") != kind:
        result.pop("session_mode", None)
    default_alias = "hermes" if kind == "hermes" else "goose"
    result.update(
        {
            "alias": prompts.ask("Agent alias", old.get("alias", default_alias), _alias),
            "title": prompts.ask("Display name", old.get("title") or kind.capitalize()),
            "kind": kind,
            "default_cwd": prompts.ask(
                "Working directory on the agent computer",
                old.get("default_cwd", str(Path.home())),
                _required,
            ),
        }
    )
    # Switching connection type must not retain the former transport fields.
    for key in ("backend", "computer_id", "hermes"):
        result.pop(key, None)
    if routed:
        for key in ("url", "secret_env", "tls_fingerprint", "allow_insecure_transport"):
            result.pop(key, None)
        result.update(
            backend="connector",
            computer_id=prompts.ask("Computer ID", old.get("computer_id", "home"), _identifier),
        )
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
                "ACP URL", old.get("url", "ws://127.0.0.1:3284/acp"), _required
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


def _profiles(prompts, raw, values, role):
    agents = raw.get("agents", [])
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
                agents[index] = _agent(
                    prompts, values, routed=agent.get("backend") == "connector", old=agent
                )
    if action == "replace":
        agents = []
        action = "add"
    if action == "remove":
        address = prompts.choice(
            "Remove agent",
            tuple(AgentProfile.model_validate(a).address for a in agents),
            AgentProfile.model_validate(agents[0]).address,
        )
        if len(agents) == 1:
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
            if not prompts.yes("Add another agent?"):
                break
    raw["agents"] = agents
    if not agents:
        raise ValueError("At least one agent profile is required")
    if role == "connector" and any(
        a.get("backend", "remote") not in {"remote", "hermes"} for a in agents
    ):
        raise ValueError(
            "A computer connector needs local ACP or Hermes profiles; choose replace to change role"
        )
    if any(a.get("backend") == "connector" for a in agents):
        raw.setdefault("connector", {})["enabled"] = True


def _connection(prompts, state):
    old = state.get("connection", {})
    url = prompts.ask(
        "Server connection URL",
        old.get("dispatcher_url", "wss://server.example/acpgw/connect"),
        _url,
    )
    if urlsplit(url).scheme == "ws":
        print(
            "WS sends enrollment keys and conversations without encryption. "
            "Use only for the initial IP test."
        )
        if not prompts.yes("Use this unencrypted test connection?"):
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
            from acp_gateway.cli.hermes_setup import inspect
            from acp_gateway.discovery.hermes import default_lock_dir

            attach = profile.hermes
            row = inspect(
                attach.lock_dir or default_lock_dir(), role=attach.role, profile=attach.profile
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
            print(f"{profile.address}: ACP initialize passed (no session or prompt sent)")
        except Exception:
            print(f"{profile.address}: ACP check failed; verify listener, secret and TLS")
        finally:
            await client.close()


def check_connections(cfg, state):
    asyncio.run(_check_agents(cfg))
    if state["role"] == "connector":
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

    check_private_paths(
        [launch.token_file], directories=[launch.token_file.parent], purpose="Connector credential"
    )
    log = cfg.settings.logging.file
    ensure_private_directory(cfg.settings.resolved_data_dir())
    ensure_private_directory(log.parent)
    if log.exists() or log.is_symlink():
        check_private_paths([log], purpose="Connector log")


def wizard(
    directory=None, *, config_file=None, env_file=None, prompts=None, section="all", role=None
):
    if sys.platform == "win32" and _managed_running(config_file, "connector"):
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
    if section not in {"all", "agents", "gateway", "telegram", "hermes", "connection"}:
        raise ValueError("Unknown configuration section")
    if section != "all" and not originals[config_file]:
        raise ValueError("Run acpgw setup first before editing an individual section")
    print("ACP Gateway setup. Ctrl+C cancels. Secrets are hidden; existing keys are kept.")
    print(
        "local: gateway to ACP agents; server: gateway for remote computers; "
        "connector: connect this computer to a server."
    )
    saved_role = previous.get("role", "local")
    role = (
        (role or prompts.choice("Role", ("local", "server", "connector"), saved_role))
        if section == "all"
        else saved_role
    )
    if section == "connection":
        role = "connector"
    if role not in {"local", "server", "connector"}:
        raise ValueError("Unknown setup role")
    if role == "connector" and section in {"gateway", "telegram", "hermes"}:
        raise ValueError(
            "Configure API, Telegram and Hermes on the gateway/server host; "
            "this host is an outgoing connector"
        )
    state = {**previous, "version": 1, "role": role}
    if role != "connector":
        state.pop("connection", None)
    gateway = raw.setdefault("gateway", {})
    if role != "connector" and section in {"all", "gateway"}:
        gateway["port"] = prompts.ask(
            "Local API/MCP port (loopback only)", str(gateway.get("port", 8765)), _port
        )
    if role == "server" and section in {"all", "gateway"}:
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
        state["connection"] = _connection(prompts, previous)
    if section in {"all", "agents"}:
        _profiles(prompts, raw, values, role)
    if (
        role == "local"
        and section == "all"
        and prompts.yes("Connect this computer to an external dispatcher now?")
    ):
        role = "connector"
        state["role"] = role
        state["connection"] = _connection(prompts, previous)
        raw.setdefault("connector", {})["enabled"] = False
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
        if role == "connector"
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
    start_service = False
    windows_task = None
    old_unit = None
    was_enabled = False
    if sys.platform == "linux" and shutil.which("systemctl"):
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
    elif sys.platform == "win32" and role == "connector":
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
    if role == "server":
        _enroll(prompts, cfg, directory)
    if role != "server" and section in {"all", "agents"}:
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
    if prompts.yes("Check connections now (no model prompts or messages)?", True):
        check_connections(cfg, state)
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
