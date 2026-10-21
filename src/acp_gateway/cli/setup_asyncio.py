"""CLI-owned loops with privacy-safe capture of Windows Proactor teardown failures.

Capture only, not a cleanup repair. CPython calls protocol.connection_lost before
socket.shutdown; retrying the callback can notify the protocol twice. Completing
its private socket/server bookkeeping here is not a safe application-level fix.
Never use this runner for long-lived gateway/service loops.
"""

import asyncio
import logging
import platform
import sys
from asyncio.proactor_events import (
    _ProactorBasePipeTransport,
    _ProactorDatagramTransport,
    _ProactorSocketTransport,
)
from functools import partial

_OPERATIONS = frozenset(
    {
        "setup",
        "enrollment-begin",
        "enrollment-finish",
        "enrollment-cancel",
        "hermes-inspect",
        "checks",
        "goose-probe",
        "hermes-probe",
        "doctor-acp",
    }
)


def _exception_handler(loop, context, *, operation="setup"):
    error = context.get("exception")
    callback = getattr(context.get("handle"), "_callback", None)
    transport = getattr(callback, "__self__", None)
    if (
        sys.platform == "win32"
        and isinstance(error, OSError)
        and getattr(error, "winerror", None) == 10022
        and getattr(callback, "__func__", None) is _ProactorBasePipeTransport._call_connection_lost
        and isinstance(transport, _ProactorBasePipeTransport)
        and transport.is_closing()
        and transport._sock is not None
        and not transport._called_connection_lost
    ):
        operation = operation if operation in _OPERATIONS else "setup"
        kind = (
            "socket"
            if isinstance(transport, _ProactorSocketTransport)
            else "datagram"
            if isinstance(transport, _ProactorDatagramTransport)
            else "pipe"
        )
        # The default loop handler auto-adds handle creation paths in debug
        # mode, even with a sanitized context. Log ONLY allowlisted fields here.
        logging.getLogger(__name__).error(
            "Windows asyncio Proactor WinError 10022 during closing transport "
            "_call_connection_lost; cleanup incomplete (capture-only, not repaired). "
            "operation=%s; transport=%s; python=%s. "
            "This callback does not establish whether enrollment succeeded; "
            "inspect/resume existing pairing rather than creating another request.",
            operation,
            kind,
            platform.python_version(),
        )
        return
    loop.default_exception_handler(context)


def run(coro, *, operation="setup"):
    """Run one setup/check operation; preserve results and coroutine exceptions."""
    if sys.platform != "win32":
        return asyncio.run(coro)
    loop = None
    previous = None
    try:
        with asyncio.Runner() as runner:
            loop = runner.get_loop()
            previous = loop.get_exception_handler()
            loop.set_exception_handler(partial(_exception_handler, operation=operation))
            return runner.run(coro)
    finally:
        # Keep capture installed through async-generator/executor shutdown too.
        if loop is not None:
            loop.set_exception_handler(previous)
