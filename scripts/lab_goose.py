#!/app/.venv/bin/python
"""Lab-only Goose CLI stand-in backed by recorded ACP traffic; no model provider."""

import argparse
import os
import signal
import threading

from fakes.fake_goose import FakeGooseServer

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("command", choices=("serve", "configure"))
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=3284)
args = parser.parse_args()
if args.command == "configure":
    print("Lab-only provider configured: recorded ACP mock, no model calls.")
else:
    if args.host != "127.0.0.1":
        parser.error("The lab executor must remain on loopback")
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *args: stopped.set())
    signal.signal(signal.SIGINT, lambda *args: stopped.set())
    with FakeGooseServer(os.environ["GOOSE_SERVER__SECRET_KEY"], port=args.port):
        stopped.wait()
