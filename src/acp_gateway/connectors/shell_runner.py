"""Bounded command execution behind a verified owned-process supervisor."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import json
import sys

from acp_gateway.adapters.wsl import _watch_cleanup, close_wsl, spawn_wsl
from acp_gateway.connectors.policy import LocalPolicyError, _cwd

OUTPUT_LIMIT = 60_000
RESULT_LIMIT = 1_048_576


def worker_source(*, windows=False):
    source = (
        importlib.resources.files("acp_gateway.connectors")
        .joinpath("_shell_worker.py")
        .read_text(encoding="utf-8")
    )
    if windows:
        source = (
            (
                importlib.resources.files("acp_gateway.connectors")
                .joinpath("_windows_shell_job.py")
                .read_text(encoding="utf-8")
            )
            + "\n"
            + source
        )
    return source


def supervisor_entry():
    """Frozen Linux entry: consume exactly one line, leaving worker input on fd 0."""
    import os

    if sys.platform != "linux":
        return 125
    from acp_gateway.adapters import _wsl_runner

    packet = bytearray()
    while len(packet) <= 16_384:
        value = os.read(0, 1)
        if not value:
            return 125
        if value == b"\n":
            sys.argv = [sys.argv[0], packet.decode("utf-8")]
            try:
                return _wsl_runner.main()
            except (OSError, ValueError, KeyError, TypeError):
                return 125
        packet.extend(value)
    return 125


class ShellRunner:
    def __init__(self, profile):
        self.profile = profile
        settings = getattr(profile, profile.kind, None)
        if (
            profile.kind not in {"codex", "claude_code"}
            or profile.backend != profile.kind
            or settings is None
            or settings.mode != "work"
        ):
            raise LocalPolicyError("direct shell requires a local managed work profile")
        self.settings = settings

    @property
    def interpreter(self):
        if self.settings.wsl:
            return "bash (WSL)"
        return "Windows PowerShell" if sys.platform == "win32" else "sh"

    def supported(self):
        return sys.platform == "win32" or (sys.platform == "linux" and self.settings.wsl is None)

    async def _spawn(self, cwd):
        if not self.supported():
            raise LocalPolicyError("native shell supervisor is unavailable on this platform")
        environment = "CODEX_HOME" if self.profile.kind == "codex" else "CLAUDE_CONFIG_DIR"
        if self.settings.wsl:
            child_argv = [self.settings.wsl.python, "-I", "-u", "-c", worker_source()]
            process = await spawn_wsl(
                self.settings.wsl,
                child_argv,
                cwd=cwd,
                agent_home_env=environment,
                limit=RESULT_LIMIT,
            )
            return process, b""
        if sys.platform == "win32":
            from acp_gateway.connectors.windows_shell import spawn_windows

            return await spawn_windows(worker_source(windows=True), cwd, RESULT_LIMIT), b""
        frozen = bool(getattr(sys, "frozen", False))
        child_argv = (
            [sys.executable, "__shell-worker"]
            if frozen
            else [
                sys.executable,
                "-I",
                "-u",
                "-c",
                worker_source(),
            ]
        )
        launch = json.dumps(
            {
                "argv": child_argv,
                "cwd": cwd,
                "agent_home_env": environment,
                "agent_home": None,
                "home": None,
                "path": [],
            },
            ensure_ascii=True,
        )
        if len(launch) > 16_384:
            raise LocalPolicyError("shell launch configuration exceeds limits")
        if frozen:
            argv = [sys.executable, "__shell-supervisor"]
        else:
            source = (
                importlib.resources.files("acp_gateway.adapters")
                .joinpath("_wsl_runner.py")
                .read_text(encoding="utf-8")
            )
            argv = [sys.executable, "-I", "-u", "-c", source, launch]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"LANG": "C.UTF-8"},
            limit=RESULT_LIMIT,
        )
        _watch_cleanup(process)
        return process, (launch.encode() + b"\n") if frozen else b""

    async def run(self, command, cwd, *, timeout=120, cancel=None):
        cwd = _cwd(cwd)
        if cwd != _cwd(self.profile.default_cwd):
            raise LocalPolicyError("shell cwd is not allowed by the local profile")
        if (
            not isinstance(command, str)
            or not command.strip()
            or len(command) > 4096
            or "\x00" in command
        ):
            raise LocalPolicyError("invalid shell command")
        if type(timeout) not in {int, float} or not 0 < timeout <= 600:
            raise LocalPolicyError("invalid shell timeout")
        argv = (
            ["/bin/bash", "--noprofile", "--norc", "-c", command]
            if self.settings.wsl
            else ["/bin/sh", "-c", command]
        )
        if sys.platform == "win32" and not self.settings.wsl:
            from acp_gateway.connectors.windows_shell import shell_argv

            argv = shell_argv(command)
        packet = (
            json.dumps(
                {"argv": argv, "cwd": cwd, "output_limit": OUTPUT_LIMIT}, ensure_ascii=True
            ).encode()
            + b"\n"
        )
        if len(packet) > 65_536:
            raise LocalPolicyError("shell request exceeds worker limits")
        if cancel is not None and cancel.is_set():
            return self._result("cancelled", cleanup_verified=True)
        process, prefix = await self._spawn(cwd)
        reading = asyncio.create_task(self._exchange(process, prefix + packet, cancel))
        draining = asyncio.create_task(self._drain(process.stderr))
        cancellation = asyncio.create_task(cancel.wait()) if cancel is not None else None
        timed_out, cancelled, line = False, False, b""
        try:
            done, _ = await asyncio.wait(
                {reading, cancellation} if cancellation else {reading},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation and cancellation in done:
                cancelled = True
            elif reading in done:
                line = reading.result()
            else:
                timed_out = True
        except asyncio.CancelledError:
            cancelled = True
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            if sys.platform == "win32" and not self.settings.wsl:
                from acp_gateway.connectors.windows_shell import close_windows

                verified = await close_windows(process)
            else:
                verified = await close_wsl(process)
            for task in (reading, draining, cancellation):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(t for t in (reading, draining, cancellation) if t is not None),
                return_exceptions=True,
            )
        if not verified:
            return self._result("unknown", reason="cleanup_unverified")
        if cancelled or timed_out:
            return self._result("cancelled" if cancelled else "timed_out", cleanup_verified=True)
        try:
            result = json.loads(line)
            if not isinstance(result, dict):
                raise ValueError
            if result.get("error") in {"spawn_failed", "execution_failed"}:
                return self._result(
                    "failed" if result["error"] == "spawn_failed" else "unknown",
                    cleanup_verified=True,
                    reason=result["error"],
                )
            code, out, err, omitted = (
                result[k] for k in ("exit_code", "stdout", "stderr", "output_omitted")
            )
            if type(code) is not int or not -(2**31) <= code < 2**32 or type(omitted) is not bool:
                raise ValueError
            if (
                not isinstance(out, str)
                or not isinstance(err, str)
                or len(out) + len(err) > OUTPUT_LIMIT
            ):
                raise ValueError
            output = out + ("\n[stderr]\n" + err if err else "")
            return self._result(
                "completed" if code == 0 else "failed",
                cleanup_verified=True,
                exit_code=code,
                output=output,
                output_omitted=omitted,
                reason="output_limit" if omitted else "",
            )
        except (ValueError, KeyError, TypeError):
            return self._result("unknown", cleanup_verified=True, reason="execution_failed")

    @staticmethod
    def _result(
        state, *, cleanup_verified=False, exit_code=None, output="", output_omitted=False, reason=""
    ):
        return dict(
            state=state,
            cleanup_verified=cleanup_verified,
            exit_code=exit_code,
            output=output,
            output_omitted=output_omitted,
            reason=reason,
        )

    @staticmethod
    async def _exchange(process, packet, cancel):
        if hasattr(process, "_shell_windows_job"):
            from acp_gateway.connectors.windows_shell import windows_ready

            await windows_ready(process)
        if cancel is not None and cancel.is_set():
            return b""
        process.stdin.write(packet)
        await process.stdin.drain()
        return await process.stdout.readline()

    @staticmethod
    async def _drain(reader):
        with contextlib.suppress(OSError):
            while await reader.read(4096):
                pass
