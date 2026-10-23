"""Fail-closed Windows to WSL subprocess boundary."""

import asyncio
import contextlib
import importlib.resources
import json
import ntpath
import os
import re
import subprocess
import sys


def _linux_path(value):
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and ":" not in value
        and not any(ord(c) < 32 for c in value)
        and ".." not in value.split("/")
    )


async def spawn_wsl(
    settings, argv: list[str], *, cwd: str, agent_home_env: str, limit: int
) -> asyncio.subprocess.Process:
    if sys.platform != "win32":
        raise RuntimeError("WSL transport requires Windows")
    root = os.environ.get("SystemRoot", "")  # noqa: SIM112 -- Windows canonical key
    home = getattr(settings, "home", None)
    if (
        not re.fullmatch(r"[A-Za-z]:[\\/][^\x00-\x1f]*", root)
        or not isinstance(argv, list)
        or not argv
        or not _linux_path(argv[0])
        or any(not isinstance(v, str) or "\x00" in v for v in argv)
        or not _linux_path(cwd)
        or not _linux_path(settings.python)
        or agent_home_env not in {"CODEX_HOME", "CLAUDE_CONFIG_DIR"}
        or (settings.agent_home is not None and not _linux_path(settings.agent_home))
        or (home is not None and not _linux_path(home))
        or not isinstance(settings.path, list)
        or not all(map(_linux_path, settings.path))
        or not isinstance(limit, int)
        or not 0 < limit <= 16777216
        or not isinstance(settings.distribution, str)
        or settings.distribution != settings.distribution.strip()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]*", settings.distribution)
        or not isinstance(settings.user, str)
        or settings.user == "root"
        or not re.fullmatch(r"[a-z_][a-z0-9_-]*", settings.user)
    ):
        raise ValueError("Invalid WSL subprocess configuration")
    source = (
        importlib.resources.files("acp_gateway.adapters")
        .joinpath("_wsl_runner.py")
        .read_text(encoding="utf-8")
    )
    payload = json.dumps(
        {
            "home": home,
            "argv": argv,
            "cwd": cwd,
            "agent_home": settings.agent_home,
            "agent_home_env": agent_home_env,
            "path": settings.path,
        },
        ensure_ascii=True,
    )
    args = (
        ntpath.join(root, "System32", "wsl.exe"),
        "--distribution",
        settings.distribution,
        "--user",
        settings.user,
        "--cd",
        cwd,
        "--exec",
        "/usr/bin/env",
        "-i",
        "LANG=C.UTF-8",
        settings.python,
        "-I",
        "-u",
        "-c",
        source,
        payload,
    )
    if len(payload) > 16384 or len(subprocess.list2cmdline(args).encode("utf-16-le")) > 60000:
        raise ValueError("Invalid WSL subprocess configuration")
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"SystemRoot": root},
            limit=limit,
        )
    except (OSError, ValueError):
        raise RuntimeError("WSL subprocess launch failed") from None
    _watch_cleanup(process)
    return process


_CLOSE_TIMEOUT = 5.0


def _watch_cleanup(process):
    """Learn only the first supervisor frame, before native Popen can write.

    Reserved frames are private. Any malformed/duplicate/out-of-order frame
    poisons verification permanently. Parsing retains at most one bounded frame;
    EOF is required so an early forged CLEAN cannot verify a live stream.
    """
    process._wsl_cleanup_verified = False
    reader = process.stderr
    if reader is None:
        return
    prefix = b"\x1eACPGW_WSL_"
    begin = prefix + b"BEGIN:"
    clean = prefix + b"CLEAN:"
    frame_bound = len(begin) + 32 + 1
    original_data, original_eof = reader.feed_data, reader.feed_eof
    pending = bytearray()
    first, valid, completed, discarding = True, True, False, False
    token = None

    def feed_data(data):
        nonlocal first, valid, completed, discarding, token
        if completed and data:
            valid = False
        pending.extend(data)
        if first:
            if len(pending) < len(begin) and begin.startswith(pending):
                return
            if pending.startswith(begin):
                end = pending.find(b"\n")
                if end < 0 and len(pending) < frame_bound:
                    return
                candidate = bytes(pending[len(begin) : end]) if end >= 0 else b""
                if end == frame_bound - 1 and re.fullmatch(rb"[0-9a-f]{32}", candidate):
                    token = candidate
                    del pending[: end + 1]
                else:
                    valid = False
            else:
                valid = False
            first = False
        while pending:
            if discarding:
                end = pending.find(b"\n")
                if end < 0:
                    pending.clear()
                    return
                del pending[: end + 1]
                discarding = False
                continue
            index = pending.find(prefix)
            if index < 0:
                keep = next(
                    (
                        n
                        for n in range(min(len(prefix) - 1, len(pending)), 0, -1)
                        if pending.endswith(prefix[:n])
                    ),
                    0,
                )
                emit = len(pending) - keep
                if emit:
                    if completed:
                        valid = False
                    original_data(bytes(pending[:emit]))
                    del pending[:emit]
                return
            if index:
                if completed:
                    valid = False
                original_data(bytes(pending[:index]))
                del pending[:index]
            end = pending.find(b"\n")
            if end < 0:
                if len(pending) >= frame_bound:
                    valid = False
                    discarding = True
                    pending.clear()
                return
            frame = bytes(pending[: end + 1])
            del pending[: end + 1]
            if token is not None and not completed and frame == clean + token + b"\n":
                completed = True
            else:
                valid = False

    def feed_eof():
        nonlocal valid
        if pending:
            valid = False
            if not pending.startswith(prefix):
                original_data(bytes(pending))
            pending.clear()
        process._wsl_cleanup_verified = bool(valid and completed and not first and not discarding)
        original_eof()

    reader.feed_data = feed_data
    reader.feed_eof = feed_eof
    # Replay early bytes through the identical ordering checks, even at EOF.
    buffered, already_eof = bytes(reader._buffer), reader._eof
    reader._buffer.clear()
    reader._eof = False
    if buffered:
        feed_data(buffered)
    if already_eof:
        feed_eof()


async def close_wsl(process: asyncio.subprocess.Process) -> bool:
    """EOF first, then bounded supervisor cleanup; False means uncertain.

    Only a supervisor receipt after group cleanup AND normal wrapper exit is
    verified. Killing an owned Windows launcher is local resource cleanup, not
    evidence that the Linux native process was cancelled.
    """
    if process.stdin is not None:
        process.stdin.close()
    verified = False
    try:
        try:
            await asyncio.wait_for(process.wait(), _CLOSE_TIMEOUT)
            verified = process.returncode == 0 and getattr(process, "_wsl_cleanup_verified", False)
        except TimeoutError:
            # EOF was already allowed a full supervisor grace interval.
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 0.25)
            except TimeoutError:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), 0.25)
    finally:
        transport = process._transport
        for fd in (0, 1, 2):
            pipe = transport.get_pipe_transport(fd)
            if pipe is not None:
                pipe.close()
        transport.close()
    return verified
