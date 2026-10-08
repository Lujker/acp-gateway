"""Entry point for the bundled executable."""

import multiprocessing

from acp_gateway.cli.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
