"""Create private Docker runtime files without overwriting an existing deployment."""

import os
import secrets
from pathlib import Path


def write_new(path: Path, text: str) -> None:
    try:
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8"
        ) as file:
            file.write(text)
    except FileExistsError:
        print(f"Kept existing {path.name}")


def main() -> None:
    root = Path(__file__).resolve().parent
    for name in ("runtime", "runtime/tls", "state", "state/enrollment"):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    write_new(root / ".env", f"ACPGW_UID={os.getuid()}\nACPGW_GID={os.getgid()}\n")
    write_new(root / "runtime/gateway.yaml", (root / "gateway.example.yaml").read_text())
    write_new(root / "runtime/nginx.conf", (root / "nginx.conf.example").read_text())
    write_new(
        root / "runtime/gateway.env",
        f"ACPGW_API_TOKEN={secrets.token_urlsafe(32)}\n"
        f"ACPGW_MCP_TOKEN={secrets.token_urlsafe(32)}\n"
        "TELEGRAM_BOT_TOKEN=\n",
    )
    print(
        "Prepared Docker files. Edit runtime/gateway.yaml and runtime/gateway.env before starting."
    )


if __name__ == "__main__":
    main()
