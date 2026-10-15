"""Saved GeoLens connections: a name, a server URL and the auth config bound to it."""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlparse

from qgis.core import QgsApplication, QgsAuthMethodConfig
from qgis.PyQt.QtCore import QSettings

from .client import GeoLensClient, same_origin, same_server
from .connection import (
    AUTHCFG_SETTING,
    LEGACY_KEY_SETTING,
    SERVER_SETTING,
    auth_server_mismatch,
    legacy_key_server,
)

CONNECTIONS_GROUP = "geolens/connections"
CURRENT_SETTING = "geolens/current_connection"


@dataclass(frozen=True)
class SavedConnection:
    name: str
    url: str
    authcfg: str


def normalized_url(url: str) -> str:
    """`url` as the client reads it: HTTPS by default, no trailing slash.

    Raises ValueError when it isn't an HTTP(S) server URL.
    """
    return GeoLensClient(url).base_url


def host_label(url: str) -> str:
    """The host and any port of `url`, the default name for its connection."""
    return urlparse(url).netloc.rpartition("@")[2] or url


def valid_name(name: str) -> bool:
    # QSettings reads a slash or backslash in a key as a group separator.
    return bool(name.strip()) and not any(char in name for char in "/\\")


def same_name(first: str, second: str) -> bool:
    # The Windows registry, where QGIS keeps settings, ignores key case.
    return first.casefold() == second.casefold()


def saved_connections(settings=None) -> list[SavedConnection]:
    """Every saved connection, sorted by name.

    The first call moves the single server and auth config that earlier
    versions saved into a connection named after the server's host.
    """
    settings = settings or QSettings()
    _migrate_single_server(settings)
    return _read(settings)


def current_connection_name(settings=None) -> str:
    """The selected connection's name, or "" when none is selected."""
    settings = settings or QSettings()
    name = str(settings.value(CURRENT_SETTING, "") or "")
    return name if any(c.name == name for c in saved_connections(settings)) else ""


def set_current_connection(name: str, settings=None) -> None:
    (settings or QSettings()).setValue(CURRENT_SETTING, name)


def save_connection(connection: SavedConnection, previous_name="", settings=None):
    """Write `connection`, replacing the one called `previous_name` when renamed."""
    settings = settings or QSettings()
    if previous_name and previous_name != connection.name:
        was_current = settings.value(CURRENT_SETTING, "") == previous_name
        remove_connection(previous_name, settings)
        if was_current:
            set_current_connection(connection.name, settings)
    settings.setValue(f"{CONNECTIONS_GROUP}/{connection.name}/url", connection.url)
    settings.setValue(
        f"{CONNECTIONS_GROUP}/{connection.name}/authcfg", connection.authcfg
    )


def remove_connection(name: str, settings=None) -> None:
    """Forget a connection. Its auth config stays in the QGIS auth database,
    where layers and other connections may still use it."""
    settings = settings or QSettings()
    settings.remove(f"{CONNECTIONS_GROUP}/{name}")
    if settings.value(CURRENT_SETTING, "") == name:
        settings.remove(CURRENT_SETTING)


def unique_name(base: str, settings=None) -> str:
    """`base`, or `base (2)` and so on when a connection already has that name."""
    taken = [c.name for c in _read(settings or QSettings())]
    name, number = base, 2
    while any(same_name(name, other) for other in taken):
        name, number = f"{base} ({number})", number + 1
    return name


def adopt_auth_config(authcfg: str, settings=None, auth_manager=None) -> None:
    """Make sure a saved connection uses `authcfg`.

    The config goes to an anonymous connection on the server it was saved
    for, or to a new connection for that server. A config that names no
    server is left alone, since it would never be sent.
    """
    settings = settings or QSettings()
    connections = saved_connections(settings)
    if not authcfg or any(c.authcfg == authcfg for c in connections):
        return
    auth_manager = auth_manager or QgsApplication.authManager()
    loaded, config = auth_manager.loadAuthenticationConfig(
        authcfg, QgsAuthMethodConfig(), False
    )
    try:
        url = normalized_url(config.uri()) if loaded and config.uri().strip() else ""
    except ValueError:
        url = ""
    if not url:
        return
    for connection in connections:
        if not connection.authcfg and same_origin(connection.url, url):
            save_connection(replace(connection, authcfg=authcfg), settings=settings)
            return
    name = unique_name(host_label(url), settings)
    save_connection(SavedConnection(name, url, authcfg), settings=settings)
    if not settings.value(CURRENT_SETTING, ""):
        set_current_connection(name, settings)


def _read(settings) -> list[SavedConnection]:
    settings.beginGroup(CONNECTIONS_GROUP)
    try:
        connections = [
            SavedConnection(
                name,
                str(settings.value(f"{name}/url", "") or ""),
                str(settings.value(f"{name}/authcfg", "") or ""),
            )
            for name in settings.childGroups()
        ]
    finally:
        settings.endGroup()
    return sorted(connections, key=lambda c: (c.name.casefold(), c.name))


def _migrate_single_server(settings) -> None:
    server = str(settings.value(SERVER_SETTING, "") or "").strip()
    authcfg = str(settings.value(AUTHCFG_SETTING, "") or "").strip()
    if not server and not authcfg:
        return
    if str(settings.value(LEGACY_KEY_SETTING, "") or "").strip():
        # A plain-text key not moved yet belongs to the server setting removed
        # below, so record it for the key first.
        legacy_key_server(settings)
    try:
        url = normalized_url(server) if server else ""
    except ValueError:
        url = ""
    if url:
        bound = authcfg if authcfg and not auth_server_mismatch(authcfg, url) else ""
        existing = [
            c.name
            for c in _read(settings)
            if same_server(c.url, url) and c.authcfg == bound
        ]
        name = existing[0] if existing else unique_name(host_label(url), settings)
        save_connection(SavedConnection(name, url, bound), settings=settings)
        if not settings.value(CURRENT_SETTING, ""):
            set_current_connection(name, settings)
        if bound:
            authcfg = ""
    settings.remove(SERVER_SETTING)
    settings.remove(AUTHCFG_SETTING)
    if authcfg:
        adopt_auth_config(authcfg, settings)
