"""Start the Phase 1 Flask mock if it is not already listening."""

from __future__ import annotations

import logging
import socket
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def ensure_mock(base_url: str) -> bool:
    """Return True if this process started the mock (caller may leave it running)."""
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8000
    if _port_open(host, port):
        logger.info("mock already running at %s", base_url)
        return False

    from apps.legacy_core.app import app

    logger.info("starting mock on %s:%s", host, port)
    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        name="legacy-core-mock",
        daemon=True,
    )
    thread.start()
    for _ in range(50):
        if _port_open(host, port):
            return True
        time.sleep(0.1)
    raise RuntimeError(f"mock failed to start on {host}:{port}")
