"""Bounded pre-auth pairing adapter on the connector listener; no owner operations."""

import asyncio
import json
import sqlite3
import time
from collections import deque

from websockets.exceptions import ConnectionClosed

MAX_PAIRING_BYTES = 4096


def _unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate pairing member")
        result[key] = value
    return result


class PairingIngress:
    def __init__(self, service, *, per_minute=120):
        self.service = service
        self.per_minute = per_minute
        self.recent = deque(maxlen=per_minute)

    def admit(self):
        now = time.monotonic()
        while self.recent and self.recent[0] <= now - 60:
            self.recent.popleft()
        if len(self.recent) >= self.per_minute:
            return False
        self.recent.append(now)
        return True

    async def handle(self, connection):
        try:
            async with asyncio.timeout(5):
                payload = await connection.recv()
                if not isinstance(payload, str) or len(payload.encode()) > MAX_PAIRING_BYTES:
                    raise ValueError
                frame = json.loads(payload, object_pairs_hook=_unique_members)
                if not isinstance(frame, dict):
                    raise ValueError
                action = frame.pop("action", None)
                if action == "request":
                    result = self.service.request(**frame)
                elif action == "claim":
                    result = self.service.claim(**frame)
                elif action == "ack":
                    result = self.service.ack(**frame)
                elif action == "status":
                    result = self.service.status(**frame)
                elif action == "cancel":
                    result = self.service.cancel(**frame)
                else:
                    raise ValueError
                await connection.send(json.dumps(result))
        except (ValueError, TypeError, RecursionError, sqlite3.Error):
            await connection.send(json.dumps({"error": "pairing request rejected"}))
        except (ConnectionClosed, ConnectionError, TimeoutError):
            pass
        finally:
            await connection.close()
