"""Docker entrypoint: start a UAMS instance with a health server.

Kept as a separate file because Dockerfile CMD arrays only accept single-line
JSON strings, not multi-line Python. See Dockerfile for the binding.
"""

import time

from uams.system import UniversalMemorySystem
from uams.health import HealthServer
from uams.utils.logging import configure_logging


def main() -> None:
    configure_logging("INFO")
    ums = UniversalMemorySystem()
    server = HealthServer(port=3111)
    server.start(ums_instance=ums)
    # Register SIGTERM/SIGINT handlers so `docker stop` (SIGTERM) and Ctrl-C
    # both trigger UniversalMemorySystem.shutdown(), which persists working
    # memories to episodic and closes backend connections. Without this,
    # Docker's default 10s SIGTERM grace period causes Python to exit hard:
    # WORKING-tier memories in the last <TTL> window are lost and SQLite WAL
    # may not be flushed cleanly.
    ums.register_signal_handlers()
    print("UAMS running with health check on :3111")
    while True:
        time.sleep(60)
        ums.decay_sweep()


if __name__ == "__main__":
    main()
