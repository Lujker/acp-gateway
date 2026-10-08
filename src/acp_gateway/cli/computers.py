"""Local computer administration without contacting the owner HTTP API."""

import json
import sqlite3

from acp_gateway.storage.db import Store


def run_computers(cfg, args) -> int:
    store = None
    try:
        store = Store.open_in(cfg.settings.resolved_data_dir())
        registry = store.computers
        if args.computer_command == "list":
            print(json.dumps(registry.list(), ensure_ascii=True, indent=2))
        elif args.computer_command == "revoke":
            registry.revoke(args.computer_id)
            print("computer revoked")
        else:
            registry.issue(
                args.computer_id,
                args.token_file,
                display_name=args.name if args.computer_command == "enroll" else None,
            )
            print("credential exported to the requested private file")
    except sqlite3.Error:
        raise ValueError("computer registry operation failed") from None
    finally:
        if store is not None:
            store.close()
    return 0
