"""Independent stdlib updater. Policy is local; requests contain no executable paths."""

import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

if __package__:
    from acp_gateway.maintenance_worker import lease
    from acp_gateway.update_journal import Journal
else:
    from update_journal import Journal
    from update_lock import lease

VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
IMAGE = re.compile(r"(?:[a-zA-Z0-9._:/-]{1,255}@)?sha256:[a-f0-9]{64}\Z")
OPERATION = re.compile(r"[a-f0-9]{32}\Z")


class RecoveryUnknown(ValueError):
    """Admission or recovery did not acknowledge completion; never replay."""


def valid_intent(policy, row):
    return bool(
        isinstance(row.get("id"), str)
        and OPERATION.fullmatch(row["id"])
        and row.get("role") == policy["role"]
        and row.get("target") == policy["target"]
        and all(
            isinstance(row.get(key), str) and len(row[key]) <= 64 and VERSION.fullmatch(row[key])
            for key in ("version", "current_version")
        )
        and type(row.get("expires")) in {float, int}
        and math.isfinite(row["expires"])
    )


def read_json(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Invalid updater JSON file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Updater JSON must be an object")
    return value


def load_policy(path):
    path = Path(path)
    if sys.platform == "win32":
        if __package__:
            from acp_gateway.windows_acl import check_private_paths, ensure_private_directory
        else:
            from windows_acl import check_private_paths, ensure_private_directory

        check_private_paths([path], directories=[path.parent], purpose="Updater policy")
    if os.name == "posix" and (path.stat().st_mode & 0o022 or path.parent.stat().st_mode & 0o022):
        raise ValueError("Updater policy and its directory must not be writable by other users")
    policy = read_json(path)
    if (
        type(policy.get("version")) is not int
        or policy.get("version") != 1
        or policy.get("backend") not in {"uv", "docker"}
        or policy.get("role") not in {"dispatcher", "connector"}
        or not isinstance(policy.get("target"), str)
        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", policy.get("target", ""))
    ):
        raise ValueError("Invalid updater policy")
    required = ["spool_dir", "config", "env_file"]
    required += (
        ["entry", "result_dir"]
        if policy["backend"] == "uv"
        else ["docker", "project_dir", "override_file", "database", "work_dir", "container_spool"]
    )
    if any(
        not isinstance(policy.get(key), str) or not Path(policy[key]).is_absolute()
        for key in required
    ):
        raise ValueError("Updater policy requires absolute local paths")
    if policy["backend"] == "docker":
        if policy["role"] != "dispatcher" or sys.platform != "linux":
            raise ValueError("Docker updater supports Linux dispatchers only")
        files, images = policy.get("compose_files"), policy.get("images")
        if (
            not isinstance(files, list)
            or not 1 <= len(files) <= 8
            or any(not isinstance(p, str) or not Path(p).is_absolute() for p in files)
            or not isinstance(policy.get("service"), str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", policy.get("service", ""))
            or not isinstance(images, dict)
            or len(images) > 100
            or any(
                not VERSION.fullmatch(v) or not isinstance(i, str) or not IMAGE.fullmatch(i)
                for v, i in images.items()
            )
        ):
            raise ValueError("Docker policy requires compose files and immutable version images")
        if policy["override_file"] in files:
            raise ValueError("Updater override must be separate from base compose files")
    elif sys.platform not in {"linux", "win32"}:
        raise ValueError("Native updater currently supports Linux and Windows")
    if sys.platform == "win32":
        ensure_private_directory(Path(policy["spool_dir"]))
    return policy


def invoke(command, *, capture=False, timeout=900):
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(  # noqa: S603 — local policy and validated version/operation IDs
            command,
            stdin=subprocess.DEVNULL,
            stdout=output if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise ValueError("Updater subprocess failed")
        if not capture:
            return ""
        output.seek(0)
        contents = output.read(65537)
        if len(contents) > 65536:
            raise ValueError("Updater subprocess output exceeded its limit")
        return contents.decode("utf-8")


def compose(policy):
    command = [policy["docker"], "compose", "--project-directory", policy["project_dir"]]
    for path in [*policy["compose_files"], policy["override_file"]]:
        command += ["-f", path]
    return command


def cli(policy, *arguments, capture=False):
    prefix = (
        [policy["entry"]]
        if policy["backend"] == "uv"
        else [*compose(policy), "exec", "-T", "--interactive=false", policy["service"], "acpgw"]
    )
    return invoke(
        [*prefix, "--config", policy["config"], "--env-file", policy["env_file"], *arguments],
        capture=capture,
        timeout=30,
    )


def version_info(policy):
    value = json.loads(cli(policy, "version", "--json", capture=True))
    if not isinstance(value, dict):
        raise ValueError("Invalid installation version report")
    return value


def result_for(policy, row):
    if not isinstance(row.get("id"), str) or not OPERATION.fullmatch(row["id"]):
        raise ValueError("Invalid update operation identity")
    path = Path(policy["result_dir"]) / (row["id"] + ".service-result.json")
    if not path.is_file():
        return None
    value = read_json(path)
    if value.get("operation_id") != row["id"]:
        return None
    if value.get("status") == "failed":
        return "failed", "failed"
    if value.get("status") == "succeeded" and version_info(policy).get("version") == row["version"]:
        return "succeeded", ""
    return None


def uv_update(policy, row):
    command = [
        policy["entry"],
        "--config",
        policy["config"],
        "--env-file",
        policy["env_file"],
        "update",
        "--with-services",
        "--service-role",
        "connector" if policy["role"] == "connector" else "gateway",
        "--version",
        row["version"],
        "--operation-id",
        row["id"],
    ]
    invoke(command)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        result = result_for(policy, row)
        if result is not None:
            return result
        time.sleep(0.5)
    return "unknown", "unknown"


def image_override(policy, image):
    path = Path(policy["override_file"])
    if path.is_symlink():
        raise ValueError("Updater override must not be a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, delete=False, encoding="utf-8"
    ) as f:
        temporary = Path(f.name)
        json.dump({"services": {policy["service"]: {"image": image}}}, f)
    try:
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def docker_start(policy):
    invoke(
        [
            *compose(policy),
            "up",
            "-d",
            "--no-build",
            "--no-deps",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "90",
            policy["service"],
        ],
        timeout=150,
    )


def docker_health(policy, version):
    if version_info(policy).get("version") != version:
        raise ValueError("Updated container version mismatch")
    cli(policy, "status", capture=True)


def docker_update(policy, row):
    if not valid_intent(policy, row):
        return "failed", "conflict"
    image = policy["images"].get(row["version"])
    if image is None:
        return "failed", "version_not_allowed"
    invoke([policy["docker"], "image", "inspect", image], timeout=30)
    # Pin the old image ID before replacing any container or definition.
    container = invoke(
        [*compose(policy), "ps", "-q", policy["service"]], capture=True, timeout=30
    ).strip()
    if not re.fullmatch(r"[a-f0-9]{12,64}", container):
        return "failed", "stale"
    old_image = invoke(
        [policy["docker"], "inspect", "--format", "{{.Image}}", container], capture=True, timeout=30
    ).strip()
    if not IMAGE.fullmatch(old_image):
        return "failed", "stale"
    reservation_name = "reservation-" + row["id"] + ".json"
    reservation = policy["container_spool"].rstrip("/") + "/" + reservation_name
    try:
        cli(policy, "maintenance", "prepare", "--reservation-file", reservation)
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            activity = json.loads(cli(policy, "maintenance", "status", capture=True))
            if isinstance(activity, dict) and activity.get("reserved") is False:
                return "failed", "busy"
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return "unknown", "unknown"
    backup = Path(policy["work_dir"]) / (row["id"] + ".db")
    changed = False
    try:
        backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if backup.exists():
            raise ValueError("Update backup already exists")
        backup.touch(mode=0o600)
        with (
            sqlite3.connect(Path(policy["database"]).as_uri() + "?mode=ro", uri=True) as source,
            sqlite3.connect(backup) as destination,
        ):
            source.backup(destination)
        image_override(policy, image)
        changed = True
        docker_start(policy)
        docker_health(policy, row["version"])
        (Path(policy["spool_dir"]) / reservation_name).unlink(missing_ok=True)
        return "succeeded", ""
    except subprocess.TimeoutExpired:
        return "unknown", "unknown"
    except (OSError, ValueError, sqlite3.Error):
        if not changed:
            raise
        try:
            invoke([*compose(policy), "stop", policy["service"]], timeout=60)
            with (
                sqlite3.connect(backup) as source,
                sqlite3.connect(policy["database"]) as destination,
            ):
                source.backup(destination)
            image_override(policy, old_image)
            docker_start(policy)
            docker_health(policy, row["current_version"])
            (Path(policy["spool_dir"]) / reservation_name).unlink(missing_ok=True)
            return "failed", "rolled_back"
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            return "unknown", "unknown"
    finally:
        if not changed:
            try:
                cli(policy, "maintenance", "resume", "--reservation-file", reservation)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise RecoveryUnknown("Reservation release unacknowledged") from exc


def execute(policy, row):
    if not valid_intent(policy, row):
        return "failed", "conflict"
    if row["expires"] <= time.time():
        return "failed", "expired"
    if not VERSION.fullmatch(row["version"]):
        return "failed", "version_not_allowed"
    if not VERSION.fullmatch(row["current_version"]) or tuple(
        map(int, row["version"].split("."))
    ) <= tuple(map(int, row["current_version"].split("."))):
        return "failed", "version_not_allowed"
    try:
        info = version_info(policy)
    except (OSError, ValueError, subprocess.SubprocessError):
        return "failed", "stale"
    if info.get("version") != row["current_version"]:
        return "failed", "stale"
    if policy["backend"] == "uv" and (info.get("manager"), info.get("source")) != (
        "uv",
        "registry",
    ):
        return "failed", "unsupported"
    try:
        return uv_update(policy, row) if policy["backend"] == "uv" else docker_update(policy, row)
    except (subprocess.TimeoutExpired, RecoveryUnknown):
        return "unknown", "unknown"
    except (OSError, ValueError, sqlite3.Error):
        if policy["backend"] == "uv":
            result = result_for(policy, row)
            if result:
                return result
            return "unknown", "unknown"
        return "failed", "failed"


def heartbeat(policy, stopped, instance):
    while not stopped.is_set():
        journal = Journal(Path(policy["spool_dir"]) / "operations.db")
        try:
            journal.set_worker(
                {
                    "role": policy["role"],
                    "target": policy["target"],
                    "backend": policy["backend"],
                    "versions": list(policy.get("images", {})),
                    "published": time.time(),
                    "instance": instance,
                }
            )
        finally:
            journal.close()
        stopped.wait(2)


def run(policy):
    root = Path(policy["spool_dir"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or (
        os.name == "posix" and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077)
    ):
        raise ValueError("Updater spool must be private to the current user")
    with lease(root / "worker.lock", exclusive=True):
        if policy["backend"] == "docker" and not Path(policy["override_file"]).exists():
            # Empty override until the first explicitly allowed immutable image is selected.
            path = Path(policy["override_file"])
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text('{"services":{}}', encoding="utf-8")
            path.chmod(0o600)
        info = version_info(policy)
        if policy["backend"] == "uv" and (info.get("manager"), info.get("source")) != (
            "uv",
            "registry",
        ):
            raise ValueError("Updater requires a registry uv installation")
        journal = Journal(root / "operations.db")
        # A claimed operation is never dispatched again after companion restart.
        for row in journal.unfinished():
            if valid_intent(policy, row) and row["status"] == "running":
                journal.transition(row["id"], ("running",), "unknown", "unknown")
        stopped = threading.Event()
        instance = uuid4().hex
        handlers = {}
        signalled = [False]

        def request_stop(*_):
            # Signal handlers must not acquire Event's lock: a signal can interrupt wait().
            signalled[0] = True

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                handlers[signum] = signal.signal(signum, request_stop)
        pulse = threading.Thread(target=heartbeat, args=(policy, stopped, instance), daemon=True)
        pulse.start()
        try:
            while pulse.is_alive() and not signalled[0]:
                request = root / "worker-stop.json"
                if request.exists():
                    value = read_json(request)
                    if value.get("instance") == instance:
                        break
                for row in journal.unfinished():
                    if signalled[0]:
                        break
                    if not valid_intent(policy, row):
                        journal.conn.execute(
                            "UPDATE operations SET status='failed',reason='conflict' WHERE id=?",
                            (row["id"],),
                        )
                        continue
                    if row["status"] == "unknown" and policy["backend"] == "uv":
                        result = result_for(policy, row)
                        if result:
                            journal.transition(row["id"], ("unknown",), *result)
                    elif row["status"] == "queued" and journal.transition(
                        row["id"], ("queued",), "running"
                    ):
                        status, reason = execute(policy, row)
                        journal.transition(row["id"], ("running",), status, reason)
                stopped.wait(0.5)
        finally:
            stopped.set()
            pulse.join(timeout=5)
            journal.set_worker({"published": 0})
            journal.close()
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


def main():
    try:
        run(load_policy(Path(sys.argv[1])))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        print(
            "Updater stopped; inspect local policy, installation and operation status.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
