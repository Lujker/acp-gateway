"""Standalone, gated shell worker. Stdlib only: also runs inside WSL Python."""

import json
import os
import subprocess
import sys
import threading


def main(*, enter_job=None, job_name=None):
    child = None
    try:
        if sys.platform == "win32":
            if enter_job is None or job_name is None:
                raise ValueError("Windows shell requires an owned job")
            enter_job(job_name)
            sys.stdout.write("ACPGW_SHELL_READY:" + job_name + "\n")
            sys.stdout.flush()
        line = sys.stdin.buffer.readline(65_537)
        if len(line) > 65_536 or not line.endswith(b"\n"):
            raise ValueError("invalid shell worker input")
        data = json.loads(line)
        argv, cwd, limit = data["argv"], data["cwd"], data["output_limit"]
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 16
            or any(not isinstance(v, str) or "\x00" in v for v in argv)
            or not isinstance(cwd, str)
            or not cwd
            or "\x00" in cwd
            or type(limit) is not int
            or not 1 <= limit <= 60_000
        ):
            raise ValueError("invalid shell worker request")
        child = subprocess.Popen(  # noqa: S603 -- local work policy chooses the shell argv
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        lock = threading.Lock()
        buffers = [bytearray(), bytearray()]
        used, omitted = 0, False

        def drain(stream, index):
            nonlocal used, omitted
            while block := stream.read(4096):
                with lock:
                    if omitted:
                        continue
                    used += len(block)
                    if used > limit:
                        buffers[0].clear()
                        buffers[1].clear()
                        omitted = True
                    else:
                        buffers[index].extend(block)

        readers = [
            threading.Thread(target=drain, args=(stream, index), daemon=True)
            for index, stream in enumerate((child.stdout, child.stderr))
        ]
        for thread in readers:
            thread.start()
        exit_code = child.wait()
        # A background descendant may retain pipe handles. The supervisor kills
        # the owned group after this worker exits; do not wait on such handles forever.
        for thread in readers:
            thread.join(timeout=0.25)
        with lock:
            if any(thread.is_alive() for thread in readers):
                buffers[0].clear()
                buffers[1].clear()
                omitted = True
            result = {
                "exit_code": exit_code,
                "stdout": buffers[0].decode("utf-8", errors="replace"),
                "stderr": buffers[1].decode("utf-8", errors="replace"),
                "output_omitted": omitted,
            }
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        result = {"error": "spawn_failed" if child is None else "execution_failed"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    # No Python shutdown join on daemon readers of inherited descendant pipes.
    # This entry is used only by a dedicated child, never in the serving process.
    os._exit(0)


if __name__ == "__main__":
    main(
        enter_job=globals().get("enter_windows_job"),
        job_name=sys.argv[1] if len(sys.argv) == 2 else None,
    )
