"""Trusted local administration. OS access to the selected private DB is authority."""

import json

from acp_gateway.pairing import PairingService
from acp_gateway.storage.db import Store


def run(cfg, args):
    data = cfg.settings.resolved_data_dir()
    store = Store.open_in(data)
    try:
        pairing = PairingService(store, boundary=data / "pairing-boundary.json")
        if args.command == "owner":
            if args.owner_command == "bootstrap":
                # An allowlist is not silently promoted to owner authority.
                if (
                    any(i not in cfg.settings.telegram.allowed_user_ids for i in args.telegram_id)
                    and cfg.settings.telegram.enabled
                ):
                    raise ValueError(
                        "Telegram approver must also be on the configured Telegram allowlist"
                    )
                result = pairing.bootstrap(args.telegram_id)
            else:
                result = pairing.owner("local")
        elif args.pairing_command == "list":
            result = pairing.pending("local")
        elif args.pairing_command == "activate-legacy-rebind":
            result = pairing.activate_migration("local", confirm=args.confirm)
        else:
            result = pairing.decide(
                "local",
                args.request_id,
                args.verification,
                approve=args.pairing_command == "approve",
            )
        print(json.dumps(result, indent=2, ensure_ascii=True))
    finally:
        store.close()
    return 0
