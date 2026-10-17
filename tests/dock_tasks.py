"""Waiting for the GeoLens dock's background tasks."""

import time

from qgis.PyQt.QtTest import QTest


def settle(dock, timeout=10):
    """Process events until the dock runs no task but dataset previews."""
    deadline = time.monotonic() + timeout
    while any(key[0] != "preview" for key in dock._pending):
        assert time.monotonic() < deadline, f"still running: {list(dock._pending)}"
        QTest.qWait(10)
