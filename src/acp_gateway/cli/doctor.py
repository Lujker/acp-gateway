"""Read-only host diagnostics, with explicit bounded network probes."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import math
import os
import socket
import ssl
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from acp_gateway.agents import AgentClient
from acp_gateway.agents.errors import AuthenticationFailed, TLSFingerprintMismatch
from acp_gateway.cli.client import api_url
from acp_gateway.cli.wizard import STATE, _snapshot, _state, _yaml
from acp_gateway.config import find_config_file, is_loopback_host, load_config
from acp_gateway.connectors.control import normalize_pin, read_credential, validate_url
from acp_gateway.connectors.launch import ConnectorLaunch
from acp_gateway.daemon import owner_token
from acp_gateway.log import redact_value


def _dispatcher_probe(connection, timeout):
    parts = validate_url(connection["dispatcher_url"])
    pin = connection.get("tls_fingerprint")
    if pin and parts.scheme != "wss":
        raise ValueError("A certificate pin requires WSS")
    with socket.create_connection(
        (parts.hostname, parts.port or (443 if parts.scheme == "wss" else 80)), timeout=timeout
    ) as sock:
        if parts.scheme == "wss":
            context = ssl.create_default_context()
            if pin:
                # A configured pin is the trust anchor; compare before accepting the peer.
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            with context.wrap_socket(sock, server_hostname=parts.hostname) as tls:
                if pin:
                    actual = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
                    if normalize_pin(actual) != normalize_pin(pin):
                        raise ValueError("Dispatcher certificate fingerprint mismatch")


async def _acp_check(cfg, profile, timeout):
    # Preserve existing TOFU trust without changing the host's pin store during diagnosis.
    with tempfile.TemporaryDirectory(prefix="acpgw-doctor-") as directory:
        pins = Path(directory)
        existing = cfg.settings.resolved_data_dir() / "pins" / f"{profile.alias}.sha256"
        if contents := _snapshot(existing):
            (pins / existing.name).write_bytes(contents)
        client = AgentClient(
            profile,
            cfg.secrets.get(profile.secret_env) if profile.secret_env else None,
            pin_dir=pins,
            open_timeout=timeout,
        )
        try:
            await asyncio.wait_for(client.connect(), timeout)
        finally:
            await client.close()


def collect(config_file=None, env_file=None, *, live=False, timeout=5):
    checks = []

    def add(name, status, detail, fix=""):
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        add("timeout", "error", "Timeout must be greater than 0 and at most 60 seconds")
        return checks
    config_file = find_config_file(config_file)
    if config_file is None:
        add("configuration", "error", "No configuration found", "acpgw setup")
        return checks
    env_file = env_file or config_file.parent / ".env"
    try:
        cfg = load_config(config_file, env_file)
        state = _state(_snapshot(config_file.parent / STATE)) or {"role": "local"}
    except (OSError, ValueError):
        add(
            "configuration",
            "error",
            "Configuration, secrets file or saved role is invalid/missing",
            "acpgw config check; repair the selected config/.env/setup.json, then acpgw setup",
        )
        return checks
    role = state["role"]
    add("configuration", "ok", f"Valid {role} configuration: {config_file}")
    for path in (config_file, env_file, config_file.parent / STATE):
        if not path.exists():
            continue
        if path.is_symlink():
            add("private-file", "error", f"Symbolic link: {path}", "Use a regular private file")
        elif os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            add(
                "private-file",
                "warning",
                f"File is accessible to other users: {path}",
                "Restrict permissions to the file owner (mode 600)",
            )
    try:
        owner_token(cfg)
        mcp = cfg.secrets.get(cfg.settings.gateway.mcp_token_env)
        for agent in cfg.settings.agents:
            secret = cfg.secrets.get(agent.secret_env) if agent.secret_env else None
            if mcp and secret and mcp.get_secret_value() == secret.get_secret_value():
                raise ValueError("MCP and agent credentials must differ")
        if cfg.missing_agent_secrets():
            raise ValueError("An agent credential is missing")
        if cfg.settings.telegram.enabled and not cfg.secrets.get(cfg.settings.telegram.token_env):
            raise ValueError("Telegram token is missing")
        add("credentials", "ok", "Referenced keys are present and separated")
    except ValueError:
        add(
            "credentials",
            "error",
            "Missing or reused credentials",
            "acpgw setup; use distinct owner/MCP/agent keys",
        )
    if not cfg.settings.agents:
        add("agents", "error", "No agent profiles configured", "acpgw agents configure")
    data = cfg.settings.resolved_data_dir()
    parent = data
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    add(
        "storage",
        "ok" if parent.is_dir() and os.access(parent, os.W_OK | os.X_OK) else "error",
        f"Data directory: {data}",
        "Choose a writable absolute data_dir" if not os.access(parent, os.W_OK) else "",
    )
    for path in (cfg.settings.connector.tls_cert, cfg.settings.connector.tls_key):
        if path and not path.is_file():
            add(
                "ingress-tls",
                "error",
                "TLS certificate or key file is missing",
                "acpgw config edit --section gateway",
            )
    if cfg.settings.connector.tls_cert and cfg.settings.connector.tls_key:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cfg.settings.connector.tls_cert, cfg.settings.connector.tls_key)
            add("ingress-tls", "ok", "TLS certificate and private key can be loaded")
        except (OSError, ValueError):
            add(
                "ingress-tls",
                "error",
                "Invalid TLS certificate/private key pair",
                "Repair TLS files or terminate TLS at the reverse proxy",
            )
    if state.get("hermes_home") and role != "connector":
        try:
            home = Path(state["hermes_home"])
            entry = _yaml(_snapshot(home / "config.yaml"))["mcp_servers"]["acpgw"]
            values = dotenv_values(home / ".env", interpolate=False)
            mcp = cfg.secrets.get(cfg.settings.gateway.mcp_token_env)
            if (
                entry["url"] != api_url(cfg) + "/mcp"
                or not mcp
                or values.get("ACPGW_MCP_TOKEN") != mcp.get_secret_value()
            ):
                raise ValueError
            if entry.get("headers", {}).get("Authorization") != "Bearer ${ACPGW_MCP_TOKEN}":
                raise ValueError
            add("hermes", "ok", "Hermes MCP endpoint and private token match this gateway")
        except (OSError, ValueError, KeyError, TypeError):
            add(
                "hermes",
                "error",
                "Hermes MCP configuration/token is missing or stale",
                "acpgw config edit --section hermes; restart Hermes afterwards",
            )
    if cfg.settings.telegram.enabled and live:
        try:
            token = cfg.secrets.get(cfg.settings.telegram.token_env)
            if not token:
                raise ValueError
            response = httpx.get(
                "https://api.telegram.org/bot" + token.get_secret_value() + "/getMe",
                timeout=timeout,
                trust_env=False,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not payload.get("ok"):
                raise ValueError
            add("telegram", "ok", "Telegram getMe succeeded; no message sent")
        except (ValueError, httpx.HTTPError):
            add(
                "telegram",
                "error",
                "Telegram token rejected or API unreachable",
                "acpgw config edit --section telegram; verify bot token and network",
            )
    if role == "connector":
        try:
            launch = ConnectorLaunch(
                **(state["connection"] | {"token_file": Path(state["connection"]["token_file"])})
            )
            ConnectorLaunch.from_args(launch, cfg)
            read_credential(launch.token_file)
            add("enrollment", "ok", f"Valid private key for computer {launch.computer_id}")
            if live:
                _dispatcher_probe(state["connection"], timeout)
                add(
                    "dispatcher",
                    "ok",
                    "TCP/TLS reachable; registration auth is checked by acpgw start",
                )
            if validate_url(launch.dispatcher_url).scheme == "ws":
                add(
                    "transport",
                    "warning",
                    "Dispatcher connection is unencrypted",
                    "Use WSS for regular public use",
                )
        except (OSError, ValueError):
            add(
                "dispatcher",
                "error",
                "Invalid enrollment, unreachable dispatcher or TLS verification failed",
                "acpgw connect; verify server URL, key file, certificate/pin and firewall",
            )
    else:
        if (
            cfg.settings.connector.enabled
            and cfg.settings.connector.port == cfg.settings.gateway.port
        ):
            add(
                "ports",
                "error",
                "API and connector ingress use the same port",
                "acpgw config edit --section gateway",
            )
        if live:
            try:
                token = owner_token(cfg).get_secret_value()
                response = httpx.get(
                    api_url(cfg) + "/health",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout,
                    trust_env=False,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("running_jobs"), int
                ):
                    raise ValueError
                add("gateway", "ok", "Owner API responds with authenticated health")
            except (OSError, ValueError, httpx.HTTPError):
                add(
                    "gateway",
                    "error",
                    "Owner API unavailable or credentials rejected",
                    "acpgw start; acpgw service status",
                )
    for profile in cfg.settings.agents:
        if profile.backend == "connector":
            add(
                "agent:" + profile.address,
                "info",
                "Routed executor; inspect acpgw computers status on the server",
            )
            continue
        if profile.backend == "hermes":
            from acp_gateway.cli.hermes_setup import inspect
            from acp_gateway.discovery.hermes import default_lock_dir

            if not Path(profile.default_cwd).is_dir():
                add(
                    "cwd:" + profile.address,
                    "error",
                    "Local Hermes working directory does not exist",
                    "acpgw agents configure",
                )
            if live:
                attach = profile.hermes
                row = inspect(
                    attach.lock_dir or default_lock_dir(),
                    role=attach.role,
                    profile=attach.profile,
                    timeout=timeout,
                )[0]
                add(
                    "agent:" + profile.address,
                    "ok" if row["status"] == "ready" else "error",
                    f"Hermes {row['status']}; observer probe, no sessions/prompts/decisions",
                    "Start Hermes in this OS under the same user; acpgw agents configure"
                    if row["status"] != "ready"
                    else "",
                )
            else:
                add(
                    "agent:" + profile.address,
                    "info",
                    "Local Hermes attachment; live discovery was not probed",
                    "acpgw doctor --live",
                )
            continue
        if profile.kind == "goose" and is_loopback_host(urlsplit(profile.url).hostname or ""):
            from acp_gateway.cli.executors import goose_binary

            try:
                add("goose", "ok", "CLI found: " + goose_binary())
            except ValueError:
                add(
                    "goose",
                    "warning",
                    "Goose CLI not found; an existing listener can still be used",
                    "acpgw agents install goose; acpgw agents provider",
                )
            if not Path(profile.default_cwd).is_dir():
                add(
                    "cwd:" + profile.address,
                    "error",
                    "Local working directory does not exist",
                    "acpgw agents configure",
                )
        if live:
            try:
                asyncio.run(_acp_check(cfg, profile, timeout))
                add(
                    "agent:" + profile.address,
                    "ok",
                    "ACP initialize succeeded; no session or prompt sent",
                )
            except Exception as exc:
                detail = "ACP initialize failed (listener, authentication, TLS or protocol)"
                if isinstance(exc, AuthenticationFailed):
                    detail = "ACP rejected the agent credential"
                elif isinstance(exc, TLSFingerprintMismatch):
                    detail = "ACP certificate does not match the trusted fingerprint"
                add(
                    "agent:" + profile.address,
                    "error",
                    detail,
                    f"acpgw agents serve {profile.alias}; "
                    "verify URL, key and pin with acpgw agents configure",
                )
    if not live:
        add("network", "info", "Live endpoints were not probed", "acpgw doctor --live")
    return checks


def doctor(config_file=None, env_file=None, *, live=False, as_json=False, timeout=5):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            checks = collect(config_file, env_file, live=live, timeout=timeout)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError):
        checks = [
            {
                "name": "configuration",
                "status": "error",
                "detail": "Cannot inspect configuration files",
                "fix": "Check paths, ownership and permissions; run acpgw config check",
            }
        ]
    checks = redact_value(checks)
    ok = not any(c["status"] == "error" for c in checks)
    if as_json:
        print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"[{check['status'].upper()}] {check['name']}: {check['detail']}")
            if check["fix"]:
                print("  Next: " + check["fix"])
    return 0 if ok else 1
