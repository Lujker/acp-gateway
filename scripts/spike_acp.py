"""P0.2 — ACP spike against a real agent (Work Goose).

Answers the open protocol questions from road-map P0.2 and records every
JSON-RPC message to ``spike-runs/<timestamp>-<scenario>/traffic.jsonl``.

Connection data comes from the regular gateway config: ``config.yaml`` (url,
tls_fingerprint, default_cwd) and ``.env`` (the agent secret).

    uv run python scripts/spike_acp.py tls
    uv run python scripts/spike_acp.py init
    uv run python scripts/spike_acp.py ping
    uv run python scripts/spike_acp.py modes
    uv run python scripts/spike_acp.py permission [--permission reject|allow|ask]
    uv run python scripts/spike_acp.py cancel [--cancel-after 5] [--permission allow|hold]
    uv run python scripts/spike_acp.py load
    uv run python scripts/spike_acp.py all

Scenarios ``permission`` and ``cancel`` ask the agent to run harmless shell
commands (``echo``, ``sleep``) on the agent machine; with ``--permission allow``
they are actually executed there.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import secrets
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from acp import PROTOCOL_VERSION, connect_to_agent
from acp.http import create_http_stream
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    FileSystemCapabilities,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallUpdate,
)

from acp_gateway import __version__, paths
from acp_gateway.agents.tls import TlsPin, pin_certificate
from acp_gateway.agents.transport import PinnedWebSocketTransport
from acp_gateway.config import AgentProfile, load_config
from acp_gateway.log import redact_text

RUNS_DIR = Path("spike-runs")


def out(text: str = "") -> None:
    print(redact_text(text), flush=True)


# --------------------------------------------------------------------- transport
# TLS pinning and the WebSocket transport live in acp_gateway.agents (P1.1).


class RecordingTransport:
    """Writes every JSON-RPC message to a JSONL file (secrets redacted)."""

    def __init__(self, inner: Any, path: Path, label: str) -> None:
        self._inner = inner
        self._file = path.open("a", encoding="utf-8")
        self._label = label
        self._t0 = time.monotonic()

    def _record(self, direction: str, message: dict[str, Any]) -> None:
        line = json.dumps(
            {
                "t": round(time.monotonic() - self._t0, 3),
                "conn": self._label,
                "dir": direction,
                "msg": message,
            },
            ensure_ascii=False,
        )
        self._file.write(redact_text(line) + "\n")
        self._file.flush()

    async def send(self, message: dict[str, Any]) -> None:
        self._record("out", message)
        await self._inner.send(message)

    async def receive(self) -> dict[str, Any] | None:
        message = await self._inner.receive()
        if message is not None:
            self._record("in", message)
        return message

    async def close(self) -> None:
        await self._inner.close()
        self._file.close()


# ------------------------------------------------------------------------ client


@dataclass
class SpikeClient:
    """ACP client side: collects session updates and answers permission requests."""

    permission_mode: str = "reject"  # reject | allow | ask | hold
    quiet: bool = False
    updates: list[tuple[str, Any]] = field(default_factory=list)
    permission_requests: list[dict[str, Any]] = field(default_factory=list)
    unexpected_calls: list[str] = field(default_factory=list)
    permission_pending: asyncio.Event = field(default_factory=asyncio.Event)
    release_hold: asyncio.Event = field(default_factory=asyncio.Event)

    def reset(self) -> None:
        self.updates.clear()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))
        if self.quiet:
            return
        kind = getattr(update, "session_update", type(update).__name__)
        text = _update_text(update)
        out(f"  <- {kind}" + (f": {text[:120]!r}" if text else ""))

    async def request_permission(
        self, session_id: str, tool_call: ToolCallUpdate, options: list[PermissionOption], **kw: Any
    ) -> RequestPermissionResponse:
        info = {
            "session_id": session_id,
            "tool_call": tool_call.model_dump(by_alias=True, exclude_none=True),
            "options": [o.model_dump(by_alias=True, exclude_none=True) for o in options],
        }
        self.permission_requests.append(info)
        out(f"  <- request_permission: {tool_call.title!r} kind={tool_call.kind}")
        out(f"     raw_input={json.dumps(info['tool_call'].get('rawInput'), ensure_ascii=False)}")
        for o in options:
            out(f"     option {o.option_id!r} kind={o.kind} name={o.name!r}")
        self.permission_pending.set()

        mode = self.permission_mode
        if mode == "hold":
            await self.release_hold.wait()
            out("     -> cancelled (hold released)")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if mode == "ask":
            answer = await asyncio.to_thread(input, "     allow once? [y/N] ")
            mode = "allow" if answer.strip().lower() in ("y", "yes") else "reject"

        wanted = "allow_once" if mode == "allow" else "reject_once"
        chosen = next((o for o in options if o.kind == wanted), None)
        if chosen is None:
            out(f"     -> no {wanted} option; answering cancelled")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        out(f"     -> selected {chosen.option_id!r}")
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=chosen.option_id, outcome="selected")
        )

    async def _unexpected(self, name: str) -> Any:
        self.unexpected_calls.append(name)
        out(f"  !! agent called client method {name} (capability not advertised)")
        raise NotImplementedError(name)

    async def write_text_file(self, *args: Any, **kwargs: Any) -> Any:
        return await self._unexpected("fs/write_text_file")

    async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
        return await self._unexpected("fs/read_text_file")

    async def create_terminal(self, *args: Any, **kwargs: Any) -> Any:
        return await self._unexpected("terminal/create")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> Any:
        return await self._unexpected("terminal/output")

    async def release_terminal(self, *args: Any, **kwargs: Any) -> Any:
        return await self._unexpected("terminal/release")

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> Any:
        return await self._unexpected("terminal/wait_for_exit")

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> Any:
        return await self._unexpected("terminal/kill")

    async def create_elicitation(self, *args: Any, **kwargs: Any) -> Any:
        return await self._unexpected("elicitation/create")

    async def complete_elicitation(self, *args: Any, **kwargs: Any) -> Any:
        return await self._unexpected("elicitation/complete")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        out(f"  <- ext_method {method}")
        self.unexpected_calls.append(f"ext:{method}")
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        out(f"  <- ext_notification {method}")

    def on_connect(self, conn: Any) -> None:
        pass

    # helpers

    def text_of(self, session_id: str) -> str:
        return "".join(
            _update_text(u)
            for sid, u in self.updates
            if sid == session_id and getattr(u, "session_update", "") == "agent_message_chunk"
        )

    def kinds(self) -> Counter[str]:
        return Counter(getattr(u, "session_update", type(u).__name__) for _, u in self.updates)


def _update_text(update: Any) -> str:
    content = getattr(update, "content", None)
    return getattr(content, "text", "") or "" if content is not None else ""


# ----------------------------------------------------------------------- runtime


@dataclass
class Spike:
    profile: AgentProfile
    secret: str | None
    transport_kind: str
    run_dir: Path
    timeout: float
    pin: TlsPin | None = None
    connections: int = 0
    results: dict[str, Any] = field(default_factory=dict)

    async def prepare_tls(self) -> None:
        if self.profile.uses_tls and self.pin is None:
            parts = urlsplit(self.profile.acp_endpoint)
            self.pin = await pin_certificate(
                parts.hostname or "",
                parts.port or 443,
                configured=self.profile.tls_fingerprint,
                pin_file=paths.data_dir() / "pins" / f"{self.profile.alias}.sha256",
            )
            out(f"TLS: fingerprint {self.pin.fingerprint} ({self.pin.source})")
            self.results["tls"] = {"fingerprint": self.pin.fingerprint, "source": self.pin.source}

    async def open(self, client: SpikeClient) -> tuple[Any, Any]:
        await self.prepare_tls()
        self.connections += 1
        headers = {"X-Secret-Key": self.secret} if self.secret else {}
        url = self.profile.acp_endpoint  # ws(s)://host:port/acp
        if self.transport_kind == "ws":
            inner: Any = await PinnedWebSocketTransport.connect(url, headers=headers, pin=self.pin)
        else:
            url = url.replace("wss://", "https://").replace("ws://", "http://")
            http_client = httpx.AsyncClient(
                http2=True,
                verify=self.pin.context if self.pin else True,
                timeout=httpx.Timeout(None, connect=15),
            )
            inner = create_http_stream(url, client=http_client, headers=headers)
        transport = RecordingTransport(
            inner, self.run_dir / "traffic.jsonl", f"c{self.connections}"
        )
        conn = connect_to_agent(client, transport)
        return conn, transport

    async def initialize(self, conn: Any) -> Any:
        response = await asyncio.wait_for(
            conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
                    terminal=False,
                ),
                client_info=Implementation(name="acp-gateway-spike", version=__version__),
            ),
            timeout=30,
        )
        return response

    async def prompt(self, conn: Any, session_id: str, text: str) -> Any:
        out(f"  -> prompt: {text!r}")
        return await asyncio.wait_for(
            conn.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text=text)]),
            timeout=self.timeout,
        )


def dump(model: Any) -> Any:
    return model.model_dump(by_alias=True, exclude_none=True, mode="json") if model else None


@contextlib.asynccontextmanager
async def session(spike: Spike, client: SpikeClient):
    conn, transport = await spike.open(client)
    try:
        init = await spike.initialize(conn)
        new = await asyncio.wait_for(
            conn.new_session(cwd=spike.profile.default_cwd, mcp_servers=[]), timeout=60
        )
        out(f"session {new.session_id}")
        yield conn, init, new
    finally:
        with contextlib.suppress(Exception):
            await conn.close()
        await transport.close()


# --------------------------------------------------------------------- scenarios


async def scenario_tls(spike: Spike, args: argparse.Namespace) -> None:
    if not spike.profile.uses_tls:
        out("TLS: profile uses plaintext transport (loopback); nothing to check")
        spike.results["tls"] = None
        return
    await spike.prepare_tls()


async def scenario_init(spike: Spike, args: argparse.Namespace) -> None:
    client = SpikeClient(quiet=True)
    conn, transport = await spike.open(client)
    try:
        init = await spike.initialize(conn)
        data = dump(init)
        out(json.dumps(data, indent=2, ensure_ascii=False))
        spike.results["initialize"] = data
    finally:
        with contextlib.suppress(Exception):
            await conn.close()
        await transport.close()


async def scenario_modes(spike: Spike, args: argparse.Namespace) -> None:
    client = SpikeClient()
    async with session(spike, client) as (conn, init, new):
        data = {"modes": dump(new.modes), "config_options": dump_list(new.config_options)}
        out(json.dumps(data, indent=2, ensure_ascii=False))
        spike.results["modes"] = data
        caps = init.agent_capabilities
        if caps and caps.session_capabilities and caps.session_capabilities.list is not None:
            listed = await asyncio.wait_for(conn.list_sessions(cwd=spike.profile.default_cwd), 30)
            sessions = dump(listed).get("sessions", [])
            out(f"list_sessions: {len(sessions)} session(s)")
            spike.results["list_sessions_count"] = len(sessions)


def dump_list(items: Any) -> Any:
    return [dump(i) for i in items] if items else None


async def scenario_ping(spike: Spike, args: argparse.Namespace) -> None:
    client = SpikeClient()
    async with session(spike, client) as (conn, _init, new):
        await set_mode(conn, new, args.mode)
        response = await spike.prompt(conn, new.session_id, "Reply with exactly one word: pong")
        text = client.text_of(new.session_id)
        out(f"stop_reason={response.stop_reason} text={text!r}")
        spike.results["ping"] = {
            "stop_reason": response.stop_reason,
            "text": text,
            "update_kinds": dict(client.kinds()),
            "usage": dump(response.usage),
        }


async def set_mode(conn: Any, new: Any, mode: str | None) -> None:
    if not mode:
        return
    available = [m.id for m in (new.modes.available_modes if new.modes else [])]
    out(f"  -> set_session_mode {mode!r} (available: {available})")
    await asyncio.wait_for(conn.set_session_mode(session_id=new.session_id, mode_id=mode), 30)


async def scenario_permission(spike: Spike, args: argparse.Namespace) -> None:
    client = SpikeClient(permission_mode=args.permission)
    marker = f"acp-spike-{secrets.token_hex(3)}"
    async with session(spike, client) as (conn, _init, new):
        await set_mode(conn, new, args.mode)
        response = await spike.prompt(
            conn,
            new.session_id,
            f"Use your shell tool to run exactly this command: echo {marker} . "
            "Then reply with the command output only. If the command was not allowed, "
            "reply with: DENIED",
        )
        text = client.text_of(new.session_id)
        out(f"stop_reason={response.stop_reason} text={text!r}")
        spike.results["permission"] = {
            "decision": args.permission,
            "requests": client.permission_requests,
            "stop_reason": response.stop_reason,
            "text": text,
            "command_ran": marker in text,
            "update_kinds": dict(client.kinds()),
        }


async def scenario_cancel(spike: Spike, args: argparse.Namespace) -> None:
    mode = args.permission if args.permission in ("allow", "hold") else "allow"
    client = SpikeClient(permission_mode=mode)
    async with session(spike, client) as (conn, _init, new):
        await set_mode(conn, new, args.mode)
        prompt_task = asyncio.create_task(
            spike.prompt(
                conn,
                new.session_id,
                "Use your shell tool to run exactly: sleep 30 && echo finished . "
                "Then reply with the output.",
            )
        )
        if mode == "hold":
            await asyncio.wait_for(client.permission_pending.wait(), timeout=spike.timeout)
            out("  .. permission request is pending; cancelling now")
        else:
            await asyncio.sleep(args.cancel_after)
            out(f"  .. {args.cancel_after}s elapsed; cancelling")
        t0 = time.monotonic()
        await conn.cancel(session_id=new.session_id)
        out("  -> session/cancel sent")
        if mode == "hold":
            # ACP: the client must answer pending permission requests with "cancelled".
            client.release_hold.set()
        response = await asyncio.wait_for(prompt_task, timeout=60)
        elapsed = round(time.monotonic() - t0, 2)
        out(f"stop_reason={response.stop_reason} after {elapsed}s")
        spike.results[f"cancel_{mode}"] = {
            "stop_reason": response.stop_reason,
            "seconds_from_cancel_to_stop": elapsed,
            "permission_requests": len(client.permission_requests),
            "update_kinds": dict(client.kinds()),
        }


async def scenario_load(spike: Spike, args: argparse.Namespace) -> None:
    word = f"lynx{secrets.randbelow(10_000):04d}"
    client = SpikeClient()
    async with session(spike, client) as (conn, init, new):
        caps = init.agent_capabilities
        out(f"agent load_session capability: {caps.load_session if caps else None}")
        await spike.prompt(conn, new.session_id, f"Remember the code word {word}. Reply: OK")
        session_id = new.session_id
    out("connection closed; reconnecting")

    client2 = SpikeClient(quiet=True)
    conn2, transport2 = await spike.open(client2)
    try:
        await spike.initialize(conn2)
        loaded = await asyncio.wait_for(
            conn2.load_session(
                cwd=spike.profile.default_cwd, session_id=session_id, mcp_servers=[]
            ),
            timeout=60,
        )
        replay = dict(client2.kinds())
        out(f"load_session ok; replayed updates during load: {replay}")
        client2.reset()
        client2.quiet = False
        response = await spike.prompt(
            conn2, session_id, "What was the code word? Reply with the word only."
        )
        text = client2.text_of(session_id)
        out(f"stop_reason={response.stop_reason} text={text!r}")
        spike.results["load"] = {
            "load_session_capability": caps.load_session if caps else None,
            "load_response": dump(loaded),
            "replayed_update_kinds": replay,
            "remembered": word.lower() in text.lower(),
            "text": text,
        }
    finally:
        with contextlib.suppress(Exception):
            await conn2.close()
        await transport2.close()


SCENARIOS = {
    "tls": scenario_tls,
    "init": scenario_init,
    "modes": scenario_modes,
    "ping": scenario_ping,
    "permission": scenario_permission,
    "cancel": scenario_cancel,
    "load": scenario_load,
}


# -------------------------------------------------------------------------- main


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ACP spike against a real agent (P0.2)")
    parser.add_argument("scenario", choices=[*SCENARIOS, "all"])
    parser.add_argument("--agent", default="work", help="agent alias from config.yaml")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--transport", choices=["ws", "http"], default="ws")
    parser.add_argument(
        "--permission", choices=["reject", "allow", "ask", "hold"], default="reject"
    )
    parser.add_argument("--mode", help="session mode to set before prompting (see 'modes')")
    parser.add_argument("--cancel-after", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds per prompt")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, args.env_file)
    profile = cfg.settings.agent(args.agent)
    secret = cfg.secrets.get(profile.secret_env) if profile.secret_env else None
    if profile.secret_env and secret is None:
        out(f"error: secret {profile.secret_env} is not set (.env or environment)")
        return 2

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS_DIR / f"{stamp}-{args.scenario}-{args.transport}"
    run_dir.mkdir(parents=True, exist_ok=True)
    spike = Spike(
        profile=profile,
        secret=secret.get_secret_value() if secret else None,
        transport_kind=args.transport,
        run_dir=run_dir,
        timeout=args.timeout,
    )
    out(f"agent {profile.alias}: {profile.url} via {args.transport}; output {run_dir}")

    names = (
        ["tls", "init", "modes", "ping", "permission", "cancel", "load"]
        if args.scenario == "all"
        else [args.scenario]
    )
    failures = 0
    for name in names:
        out(f"\n=== {name}")
        try:
            await SCENARIOS[name](spike, args)
        except Exception as exc:
            failures += 1
            out(f"FAILED {name}: {type(exc).__name__}: {exc}")
            spike.results.setdefault("errors", {})[name] = f"{type(exc).__name__}: {exc}"

    summary = redact_text(json.dumps(spike.results, indent=2, ensure_ascii=False, default=str))
    (run_dir / "summary.json").write_text(summary + "\n", encoding="utf-8")
    out(f"\nsummary: {run_dir / 'summary.json'}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
