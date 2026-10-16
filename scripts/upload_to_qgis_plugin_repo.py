#!/usr/bin/env python3
"""Upload a packaged plugin to the official QGIS plugin repository."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import quote

# nosec B411 - the only endpoint is the official plugins.qgis.org HTTPS RPC
# service, which is also the only XML this script ever parses.
from xmlrpc.client import Binary, SafeTransport, ServerProxy  # nosec B411

# ServerProxy otherwise inherits the global default socket timeout, which is
# None, so an unresponsive server would hang the job until the runner limit.
# plugins.qgis.org scans an upload before it replies, which can take over a minute.
DEFAULT_TIMEOUT = 300


class TimeoutTransport(SafeTransport):
    """HTTPS transport that gives every connection a finite socket timeout."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, **kwargs):
        super().__init__(**kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


def upload(path: Path, username: str, password: str, timeout: float = DEFAULT_TIMEOUT):
    endpoint = "https://{}:{}@plugins.qgis.org/plugins/RPC2/".format(
        quote(username, safe=""), quote(password, safe="")
    )
    with path.open("rb") as stream:
        payload = Binary(stream.read())
    server = ServerProxy(endpoint, transport=TimeoutTransport(timeout), verbose=False)
    return server.plugin.upload(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path", type=Path)
    args = parser.parse_args()
    username = os.environ.get("QGIS_PLUGIN_REPO_USERNAME")
    password = os.environ.get("QGIS_PLUGIN_REPO_PASSWORD")
    if not username or not password:
        parser.error("QGIS plugin repository credentials are not configured")
    plugin_id, version_id = upload(args.zip_path, username, password)
    print(f"Uploaded plugin id={plugin_id}, version id={version_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
