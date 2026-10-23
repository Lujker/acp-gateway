"""Standalone Linux stdio supervisor (stdlib only, Python >= 3.10)."""

import contextlib
import ctypes
import json
import os
import pwd
import secrets
import selectors
import signal
import subprocess
import sys
import time


def linux_path(value):
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and ":" not in value
        and not any(ord(c) < 32 for c in value)
        and ".." not in value.split("/")
    )


def main():
    if sys.platform != "linux" or sys.version_info < (3, 10) or not hasattr(os, "WNOWAIT"):
        raise ValueError("unsupported runtime")
    if len(sys.argv) != 2 or len(sys.argv[1]) > 16384:
        raise ValueError("invalid launch data")
    data = json.loads(sys.argv[1])
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("argv"), list)
        or not data["argv"]
        or not linux_path(data["argv"][0])
        or any(not isinstance(v, str) or "\x00" in v for v in data["argv"])
        or not linux_path(data.get("cwd"))
        or data.get("agent_home_env") not in {"CODEX_HOME", "CLAUDE_CONFIG_DIR"}
        or (data.get("agent_home") is not None and not linux_path(data["agent_home"]))
        or (data.get("home") is not None and not linux_path(data["home"]))
        or not isinstance(data.get("path"), list)
        or not all(map(linux_path, data["path"]))
    ):
        raise ValueError("invalid launch data")
    home = data.get("home") or pwd.getpwuid(os.getuid()).pw_dir
    env = {
        "HOME": home,
        "PATH": home.rstrip("/")
        + "/.local/bin:/usr/local/bin:/usr/bin:/bin"
        + "".join(":" + p for p in data["path"]),
        "LANG": "C.UTF-8",
    }
    if data["agent_home"] is not None:
        env[data["agent_home_env"]] = data["agent_home"]
    stopping = False
    stalled = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    # An inherited SIGCHLD=SIG_IGN would auto-reap the leader and invalidate
    # group ownership before cleanup. Reset it before any native child exists.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    libc = ctypes.CDLL(None, use_errno=True)
    parent = os.getppid()
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError("parent death supervision unavailable")
    if os.getppid() != parent:
        raise OSError("supervisor parent lost")
    # Hide /proc environ, fd and memory from same-UID native peers BEFORE
    # publishing the capability or creating a child. This is not a sandbox
    # against root or hostile signals; unavailable protection fails closed.
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError("supervisor privacy unavailable")
    cleanup_token = secrets.token_hex(16)
    begin = ("\x1eACPGW_WSL_BEGIN:" + cleanup_token + "\n").encode("ascii")
    os.set_blocking(2, False)
    if os.write(2, begin) != len(begin):
        raise OSError("supervisor handshake unavailable")
    supervisor = os.getpid()

    def child_guard():
        # This runner is single threaded. The preexec hook sets Linux PDEATHSIG
        # before exec; a runner SIGKILL kills the immediate child only. A child
        # that changes credentials or descendants escaping the group are outside
        # this guarantee; do not claim verified group cleanup after runner loss.
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != supervisor:
            os._exit(125)

    child = subprocess.Popen(  # noqa: S603 -- validated absolute argv; no shell
        data["argv"],
        cwd=data["cwd"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=child_guard,
    )
    selector = selectors.DefaultSelector()
    outputs = {child.stdout.fileno(): 1, child.stderr.fileno(): 2}
    queues = {child.stdin.fileno(): bytearray(), 1: bytearray(), 2: bytearray()}
    last_progress = {fd: time.monotonic() for fd in queues}
    for fd in [0, 1, 2, child.stdin.fileno(), *outputs]:
        os.set_blocking(fd, False)
    reading = {0, *outputs}
    try:
        while not stopping:
            now = time.monotonic()
            if any(queue and now - last_progress[fd] > 2 for fd, queue in queues.items()):
                # A stalled consumer truncates the protocol stream and the child is
                # killed: that is not a clean completion and carries no receipt.
                stalled = True
                break
            if os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            selector.close()
            selector = selectors.DefaultSelector()
            for fd in reading:
                target = child.stdin.fileno() if fd == 0 else outputs[fd]
                if len(queues[target]) < 262144:
                    selector.register(fd, selectors.EVENT_READ)
            for fd, queue in queues.items():
                if queue:
                    selector.register(fd, selectors.EVENT_WRITE)
            for key, events in selector.select(0.05):
                fd = key.fd
                if events & selectors.EVENT_READ:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        reading.remove(fd)
                        if fd == 0:
                            stopping = True
                    else:
                        target = child.stdin.fileno() if fd == 0 else outputs[fd]
                        if not queues[target]:
                            last_progress[target] = time.monotonic()
                        queues[target].extend(chunk)
                if events & selectors.EVENT_WRITE:
                    queue = queues[fd]
                    del queue[: os.write(fd, queue)]
                    last_progress[fd] = time.monotonic()
    finally:
        selector.close()
        # Never reap the leader before signalling its group: the unreaped PID
        # pins the group identity against PID reuse, including after native exit.
        try:
            os.killpg(child.pid, signal.SIGTERM)
            time.sleep(0.2)
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=2)
        # The child may already have exited with unread bytes in its pipes.
        # Kill descendants before draining so inherited descriptors cannot hold
        # us forever. Keep the same per-channel queue cap and a fixed deadline.
        deadline = time.monotonic() + 2
        remaining = set(outputs)
        queues.pop(child.stdin.fileno())
        while time.monotonic() < deadline and (remaining or any(queues.values())):
            drain = selectors.DefaultSelector()
            try:
                for fd in remaining:
                    if len(queues[outputs[fd]]) < 262144:
                        drain.register(fd, selectors.EVENT_READ)
                for fd, queue in queues.items():
                    if queue:
                        drain.register(fd, selectors.EVENT_WRITE)
                for key, events in drain.select(0.05):
                    if events & selectors.EVENT_READ:
                        chunk = os.read(key.fd, 65536)
                        if chunk:
                            queues[outputs[key.fd]].extend(chunk)
                        else:
                            remaining.remove(key.fd)
                    if events & selectors.EVENT_WRITE:
                        queue = queues[key.fd]
                        del queue[: os.write(key.fd, queue)]
            except OSError:
                stalled = True
                break
            finally:
                drain.close()
        if remaining or any(queues.values()):
            # Deadline expiry is data loss even after native exit or stdin EOF.
            stalled = True
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()
    if stalled:
        return 126
    marker = ("\x1eACPGW_WSL_CLEAN:" + cleanup_token + "\n").encode("ascii")
    # Nonblocking receipt: a blocked/lost stderr must not delay owned cleanup.
    try:
        if os.write(2, marker) != len(marker):
            return 126
    except OSError:
        return 126
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        with contextlib.suppress(OSError):
            os.write(2, b"WSL subprocess failed\n")
        raise SystemExit(125) from None
