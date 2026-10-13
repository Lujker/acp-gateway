"""Instance-fenced local stop requests; use only while holding connector.lock."""

import asyncio
import json
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

RUNTIME_FILE = "connector-control.json"
STOP_FILE = "connector-stop.json"


def _read(path, *, limit=1024):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or getattr(info, "st_file_attributes", 0) & 0x400  # Windows reparse point
        or info.st_size > limit
    ):
        raise ValueError("invalid connector control file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("connector control file changed while opening")
        return json.loads(stream.read(limit + 1))


@contextmanager
def control_record(directory: Path, *, control_id=None):
    """Publish a fresh instance ID and remove the record before releasing the lock."""
    instance = uuid.uuid4().hex
    path = directory / RUNTIME_FILE
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(
                json.dumps(
                    {
                        "version": 1,
                        "instance": instance,
                        "pid": os.getpid(),
                        "controlId": control_id,
                    }
                )
            )
        temporary.replace(path)
        yield instance
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)


async def watch_stop(directory: Path, instance: str, terminate, *, interval=0.2):
    """Ignore stale/malformed requests, including a previous process's stop file."""
    while True:
        try:
            request = await asyncio.to_thread(_read, directory / STOP_FILE)
            if isinstance(request, dict) and (
                type(request.get("version")) is int
                and request["version"] == 1
                and request.get("instance") == instance
            ):
                terminate()
                return
        except (OSError, ValueError, UnicodeError, RecursionError):
            pass
        await asyncio.sleep(interval)
