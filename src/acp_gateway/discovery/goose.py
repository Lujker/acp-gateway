"""Explicit local Goose endpoint inspection; no process/memory/credential scraping."""

import asyncio
import ipaddress
import math
from urllib.parse import urlsplit

from acp.schema import InitializeResponse

from acp_gateway import __version__
from acp_gateway.agents.errors import AgentUnavailable, AuthenticationFailed, TLSFingerprintMismatch
from acp_gateway.agents.tls import TlsPin, fetch_peer_certificate, fingerprint_of, pinned_context
from acp_gateway.agents.transport import PinnedWebSocketTransport
from acp_gateway.config import AgentProfile
from acp_gateway.log import redact_text, register_secret


async def probe_goose(url=None, *, secret=None, fingerprint=None, timeout=5):
    row = {
        "agent": "goose",
        "discovery": "explicit-endpoint",
        "access": {
            "mode": "initialize-only",
            "shared_prompt": False,
            "shared_cancel": False,
            "approval_decisions": False,
            "session_adoption": False,
        },
    }
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("probe timeout must be greater than zero and at most 60 seconds")
    if not url:
        return row | {
            "status": "manual-endpoint-required",
            "detail": "Provide a local ACP URL; automatic Goose Desktop discovery is not verified",
        }
    try:
        parts = urlsplit(url)
        if parts.query or parts.fragment or not ipaddress.ip_address(parts.hostname).is_loopback:
            raise ValueError
        profile = AgentProfile(
            alias="probe", kind="goose", url=url, default_cwd="/", tls_fingerprint=fingerprint
        )
        parts = urlsplit(profile.acp_endpoint)
        if profile.uses_tls and not profile.tls_fingerprint:
            return row | {
                "status": "auth-required",
                "detail": "Provide the expected TLS fingerprint",
            }
        if secret is not None and (not secret or not secret.isprintable()):
            raise ValueError
    except (ValueError, TypeError):
        return row | {
            "status": "incompatible",
            "detail": "Use a credential-free numeric-loopback ACP URL and valid TLS pin",
        }
    if secret:
        register_secret(secret)
    transport = None
    try:
        async with asyncio.timeout(timeout):
            pin = None
            if profile.uses_tls:
                der = await fetch_peer_certificate(parts.hostname, parts.port or 443, timeout)
                actual = fingerprint_of(der)
                if actual != profile.tls_fingerprint:
                    raise TLSFingerprintMismatch("Goose probe pin mismatch")
                pin = TlsPin(actual, pinned_context(der), "config")
            transport = await PinnedWebSocketTransport.connect(
                profile.acp_endpoint,
                headers={"X-Secret-Key": secret} if secret else {},
                pin=pin,
                open_timeout=timeout,
                max_message_bytes=65536,
            )
            await transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": "acpgw-observer",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientInfo": {"name": "acpgw-observer", "version": __version__},
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False},
                            "terminal": False,
                        },
                    },
                }
            )
            for _ in range(64):
                message = await transport.receive()
                if message is None:
                    raise AgentUnavailable("Goose observer connection closed")
                # Never answer unsolicited session/permission/filesystem requests.
                if message.get("method") is not None or message.get("id") != "acpgw-observer":
                    continue
                raw = message.get("result")
                if (
                    message.get("jsonrpc") != "2.0"
                    or not isinstance(raw, dict)
                    or (type(raw.get("protocolVersion")) is not int or raw["protocolVersion"] != 1)
                ):
                    raise ValueError
                response = InitializeResponse.model_validate(raw)
                info = response.agent_info
                if (
                    info is None
                    or info.name.casefold() != "goose"
                    or not (
                        info.version and len(info.version) <= 128 and info.version.isprintable()
                    )
                ):
                    raise ValueError
                return row | {
                    "status": "ready",
                    "version": redact_text(info.version),
                    "endpoint": redact_text(profile.acp_endpoint),
                    "protocol_version": 1,
                    "advertised_load_session": response.agent_capabilities.load_session,
                }
            raise ValueError
    except AuthenticationFailed:
        return row | {"status": "auth-required", "detail": "Goose rejected the backend credential"}
    except TLSFingerprintMismatch:
        return row | {"status": "auth-required", "detail": "Goose TLS fingerprint does not match"}
    except (AgentUnavailable, OSError, ConnectionError, TimeoutError):
        return row | {"status": "offline", "detail": "Goose ACP endpoint is unavailable"}
    except (ValueError, TypeError):
        return row | {
            "status": "incompatible",
            "detail": "Goose returned an incompatible initialization",
        }
    finally:
        if transport is not None:
            await transport.close()
