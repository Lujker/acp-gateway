"""Read-only SSH reverse-tunnel preview; lifecycle operations are disabled."""

import argparse
import re
from pathlib import PurePosixPath

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
SAFE_PATH = re.compile(r"/[A-Za-z0-9_./-]+\Z")


def _path(value):
    path = PurePosixPath(value)
    if not path.is_absolute() or not SAFE_PATH.fullmatch(str(path)) or ".." in path.parts:
        raise ValueError("path must be absolute and contain only safe characters")
    return str(path)


def prepare(**kwargs):
    raise ValueError("prepare is disabled: safe filesystem ownership is not established")


def control(action, name, base, units, confirm, run):
    raise ValueError("start/stop are disabled: active unit ownership cannot be guaranteed")


def preview(
    *,
    name,
    host,
    user,
    port,
    jump,
    jump_user,
    jump_port,
    key,
    known_hosts,
    local_port,
    remote_port,
    config,
):
    if not NAME.fullmatch(name):
        raise ValueError("invalid tunnel name")
    if not HOST.fullmatch(host) or not HOST.fullmatch(jump):
        raise ValueError("invalid host")
    if not USER.fullmatch(user) or not USER.fullmatch(jump_user):
        raise ValueError("invalid user")
    if any(
        type(p) is not int or not 1 <= p <= 65535
        for p in (port, jump_port, local_port, remote_port)
    ):
        raise ValueError("invalid port")
    key, known_hosts, config = map(_path, (key, known_hosts, config))
    common = (
        f"    IdentityFile {key}\n    UserKnownHostsFile {known_hosts}\n"
        "    GlobalKnownHostsFile /dev/null\n    StrictHostKeyChecking yes\n"
        "    IdentitiesOnly yes\n    BatchMode yes\n    ForwardAgent no\n"
    )
    text = (
        f"Host acpgw-jump\n    HostName {jump}\n    User {jump_user}\n"
        f"    Port {jump_port}\n{common}\nHost acpgw-target\n"
        f"    HostName {host}\n    User {user}\n    Port {port}\n"
        f"    ProxyJump acpgw-jump\n{common}"
    )
    command = (
        f"/usr/bin/ssh -F {config} -N -T -o ExitOnForwardFailure=yes "
        f"-R 0.0.0.0:{remote_port}:127.0.0.1:{local_port} acpgw-target"
    )
    return text, command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "prepare", "start", "stop"))
    parser.add_argument("name")
    parser.add_argument("--base")
    parser.add_argument("--confirm", action="store_true")
    for opt in ("host", "user", "jump", "jump-user", "key", "known-hosts", "config"):
        parser.add_argument("--" + opt)
    for opt in ("port", "jump-port", "local-port", "remote-port"):
        parser.add_argument("--" + opt, type=int)
    args = parser.parse_args(argv)
    try:
        if args.action != "preview":
            raise ValueError(
                f"{args.action} is disabled: ownership and filesystem safety are not established"
            )
        fields = (
            "host",
            "user",
            "jump",
            "jump_user",
            "key",
            "known_hosts",
            "config",
            "port",
            "jump_port",
            "local_port",
            "remote_port",
        )
        if any(getattr(args, field) is None for field in fields):
            raise ValueError(
                "preview requires all host, user, port, key, known-hosts and config options"
            )
        text, command = preview(name=args.name, **{field: getattr(args, field) for field in fields})
        print("# PREVIEW ONLY: no files written; host keys are NOT verified\n")
        print(text)
        print("# Command (not executed):")
        print(command)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
