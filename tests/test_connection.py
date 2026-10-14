"""Credentials and transport against a real QGIS: auth configs, migration, layers."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request

import pytest

pytest.importorskip("pytest_qgis")

from qgis.core import Qgis, QgsApplication, QgsAuthMethodConfig  # noqa: E402
from qgis.PyQt.QtCore import QSettings  # noqa: E402

from geolens_qgis.client import GeoLensError  # noqa: E402
from geolens_qgis.connection import (  # noqa: E402
    AUTHCFG_SETTING,
    LEGACY_KEY_SETTING,
    SERVER_SETTING,
    GeoLensConnection,
    migrate_plaintext_key,
    store_api_key,
)

TEST_KEY = "glk_only_for_tests_aaaaaaaaaaaaaaaa"  # pragma: allowlist secret
MASTER = "geolens-test-master"  # pragma: allowlist secret


@pytest.fixture
def auth_manager(qgis_app):
    manager = QgsApplication.authManager()
    assert manager.setMasterPassword(MASTER, True)
    return manager


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def clean_profile_settings(qgis_app):
    QSettings().remove("geolens")
    yield QSettings()
    QSettings().remove("geolens")


class _Server:
    """A local HTTP server that records each request and answers from `routes`."""

    def __init__(self):
        self.seen = []
        self.routes = {}
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                headers = {name.lower(): value for name, value in self.headers.items()}
                recorder.seen.append((self.command, self.path, headers))
                status, headers, body = recorder.routes.get(
                    urlparse(self.path).path, (404, {}, b'{"detail":"Not here"}')
                )
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = _answer

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def keys_seen(self):
        return [headers.get("x-api-key") for _, _, headers in self.seen]


@pytest.fixture
def server():
    instance = _Server()
    yield instance
    instance.httpd.shutdown()


@pytest.fixture
def other_server():
    instance = _Server()
    yield instance
    instance.httpd.shutdown()


def _quiet(*_):
    pass


def _json(body):
    return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()


def test_requests_carry_the_selected_auth_config(auth_manager, server):
    server.routes["/api/search/datasets/"] = _json({"features": []})
    authcfg = store_api_key(TEST_KEY, server.url, _quiet, "", auth_manager)
    assert authcfg
    GeoLensConnection(server.url, authcfg=authcfg).client.search()
    assert server.keys_seen() == [TEST_KEY]


def test_a_pasted_key_travels_with_the_request(server):
    server.routes["/api/search/datasets/"] = _json({"features": []})
    server.routes["/api/datasets/d/features/"] = _json({"id": 3})
    client = GeoLensConnection(server.url, api_key=TEST_KEY).client
    client.search()
    assert client.create_feature("d", {"type": "Feature"}) == 3
    assert server.keys_seen() == [TEST_KEY, TEST_KEY]
    assert server.seen[1][2]["content-type"] == "application/json"


def test_a_redirect_to_another_host_does_not_get_the_key(
    auth_manager, server, other_server
):
    other_server.routes["/landed"] = _json({"features": []})
    server.routes["/api/search/datasets/"] = (
        302,
        {"Location": other_server.url + "/landed"},
        b"",
    )
    authcfg = store_api_key(TEST_KEY, server.url, _quiet, "", auth_manager)
    with pytest.raises(GeoLensError, match="redirected"):
        GeoLensConnection(server.url, authcfg=authcfg).client.search()
    assert server.keys_seen() == [TEST_KEY]
    assert other_server.keys_seen() == [None]


def test_a_redirect_is_refused_rather_than_answered_anonymously(server):
    # The redirected request goes out without the key, so its answer would
    # silently be the public catalog.
    server.routes["/landed"] = _json({"features": []})
    server.routes["/api/search/datasets/"] = (
        307,
        {"Location": server.url + "/landed"},
        b"",
    )
    with pytest.raises(GeoLensError, match="redirected"):
        GeoLensConnection(server.url, api_key=TEST_KEY).client.search()
    assert server.keys_seen() == [TEST_KEY, None]


def test_a_config_that_sends_no_api_key_is_refused(auth_manager, server):
    server.routes["/api/search/datasets/"] = _json({"features": []})
    config = QgsAuthMethodConfig("APIHeader")
    config.setName("not GeoLens")
    config.setConfig("X-Other", "value")
    stored, config = auth_manager.storeAuthenticationConfig(config)
    assert stored
    with pytest.raises(GeoLensError, match="X-Api-Key"):
        GeoLensConnection(server.url, authcfg=config.id()).client.search()
    assert server.seen == []


def test_an_unusable_auth_config_fails_instead_of_going_anonymous(auth_manager, server):
    server.routes["/api/search/datasets/"] = _json({"features": []})
    with pytest.raises(GeoLensError):
        GeoLensConnection(server.url, authcfg="missing").client.search()
    assert server.seen == []


def test_http_errors_keep_the_server_detail_and_empty_replies_are_valid(server):
    client = GeoLensConnection(server.url).client
    with pytest.raises(GeoLensError, match=r"HTTP 404\): Not here"):
        client.search()
    server.routes["/api/datasets/d/features/7"] = (204, {}, b"")
    client.delete_feature("d", 7)
    server.routes["/empty"] = (204, {}, b"")
    with GeoLensConnection(server.url).transport(
        Request(server.url + "/empty")
    ) as reply:
        assert reply.read() == b""


class _RecordingAuthManager:
    """Wraps the real auth manager, or stands in for a failing one."""

    def __init__(self, events, real=None, store_ok=True, readback=None):
        self.events = events
        self.real = real
        self.store_ok = store_ok
        self.readback = readback
        self.removed = []

    def masterPasswordIsSet(self):
        return False

    def storeAuthenticationConfig(self, config):
        self.events.append("store")
        if self.real is not None:
            return self.real.storeAuthenticationConfig(config)
        if not self.store_ok:
            return False, config
        config.setId("fake001")
        return True, config

    def loadAuthenticationConfig(self, authcfg, config, full):
        if self.real is not None:
            return self.real.loadAuthenticationConfig(authcfg, config, full)
        config.setConfig("X-Api-Key", self.readback)
        return True, config

    def removeAuthenticationConfig(self, authcfg):
        self.removed.append(authcfg)
        return True


def _legacy(settings):
    settings.setValue(SERVER_SETTING, "https://gis.example.org")
    settings.setValue(LEGACY_KEY_SETTING, TEST_KEY)


def test_migration_moves_the_key_and_deletes_the_plain_text(auth_manager, settings):
    _legacy(settings)
    events = []

    def notify(text, level):
        events.append((text, level))

    recording = _RecordingAuthManager(events, real=auth_manager)
    authcfg = migrate_plaintext_key(settings, notify, recording)
    assert authcfg
    assert settings.value(LEGACY_KEY_SETTING) is None
    assert settings.value(AUTHCFG_SETTING) == authcfg
    loaded, config = auth_manager.loadAuthenticationConfig(
        authcfg, QgsAuthMethodConfig(), True
    )
    assert loaded
    assert config.method() == "APIHeader"
    assert config.configMap() == {"X-Api-Key": TEST_KEY}
    # Name and URI are stored unencrypted.
    assert TEST_KEY not in config.name() + config.uri()
    # The user hears about the master password prompt before the write
    # that triggers it, and every message is free of the key.
    notice, level = events[0]
    assert "master password" in notice and level == Qgis.MessageLevel.Info
    assert events[1] == "store"
    assert events[-1][1] == Qgis.MessageLevel.Success
    assert not any(TEST_KEY in str(event) for event in events)


def test_a_failed_or_cancelled_write_keeps_the_plain_text_key(settings):
    _legacy(settings)
    events = []
    failing = _RecordingAuthManager(events, store_ok=False)
    assert migrate_plaintext_key(settings, lambda *e: events.append(e), failing) == ""
    assert settings.value(LEGACY_KEY_SETTING) == TEST_KEY
    assert settings.value(AUTHCFG_SETTING) is None
    assert events[-1][1] == Qgis.MessageLevel.Warning


def test_migration_keeps_a_config_the_user_already_selected(auth_manager, settings):
    _legacy(settings)
    settings.setValue(AUTHCFG_SETTING, "chosen1")
    authcfg = migrate_plaintext_key(settings, _quiet, auth_manager)
    assert authcfg
    assert settings.value(LEGACY_KEY_SETTING) is None
    assert settings.value(AUTHCFG_SETTING) == "chosen1"


def test_a_key_the_database_does_not_hand_back_is_kept(settings):
    _legacy(settings)
    events = []
    garbled = _RecordingAuthManager(events, readback="something else")
    assert migrate_plaintext_key(settings, lambda *e: events.append(e), garbled) == ""
    assert settings.value(LEGACY_KEY_SETTING) == TEST_KEY
    assert garbled.removed == ["fake001"]


class _FakeGeoLens:
    """An opener serving one vector and one raster dataset."""

    def __call__(self, request, timeout=0):
        path = urlparse(request.full_url).path
        bodies = {
            "/api/search/datasets/": {
                "features": [
                    {
                        "id": "roads",
                        "properties": {"title": "Roads", "geometry_type": "POINT"},
                    },
                    {
                        "id": "dem",
                        "properties": {
                            "record_type": "raster_dataset",
                            "band_count": 1,
                        },
                    },
                ]
            },
            "/api/capabilities": {"flags": {"enable_dataset_editing": True}},
            "/api/tiles/token/roads/": {
                "kind": "vector",
                "sig": "s",
                "exp": 9,
                "scope": "roads:p1",
                "expires_in": 600,
            },
            "/api/tiles/token/dem/": {
                "kind": "raster",
                "tile_url": "/raster-tiles/dem/tiles/{z}/{x}/{y}.png?sig=s&exp=9",
                "expires_in": 600,
            },
            "/api/collections/roads/items": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": 1,
                        "geometry": {"type": "Point", "coordinates": [1, 2]},
                        "properties": {"name": "A"},
                    }
                ],
            },
        }
        if path not in bodies:
            raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(b"{}"))
        return BytesIO(json.dumps(bodies[path]).encode())


def _select(dock, title):
    for index in range(dock.results.topLevelItemCount()):
        item = dock.results.topLevelItem(index)
        if item.text(0) == title:
            dock.results.setCurrentItem(item)
            return
    raise AssertionError(f"{title} is not listed")


def test_no_key_in_any_layer_uri_saved_project_or_setting(
    qgis_iface, clean_profile_settings, tmp_path
):
    from qgis.core import QgsProject

    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        dock.url_edit.setText("https://gis.example.org")
        dock.key_edit.setText(TEST_KEY)
        dock.connect_server()
        assert dock.client is not None and dock.editing_enabled
        dock.view_only.setChecked(False)
        _select(dock, "Roads")
        dock.add_tiles()
        dock.add_features()
        _select(dock, "dem")
        dock.add_tiles()
        layers = list(QgsProject.instance().mapLayers().values())
        assert len(layers) == 3
        for layer in layers:
            assert TEST_KEY not in layer.source()
        project_file = tmp_path / "project.qgs"
        assert QgsProject.instance().write(str(project_file))
        assert TEST_KEY not in project_file.read_text(encoding="utf-8")
        profile = QSettings()
        assert all(
            TEST_KEY not in str(profile.value(name)) for name in profile.allKeys()
        )
    finally:
        dock.cleanup()
        QgsProject.instance().clear()


def test_opening_the_dock_migrates_a_saved_key(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    _legacy(clean_profile_settings)
    dock = GeoLensDock(qgis_iface)
    try:
        authcfg = dock.auth_select.configId()
        assert authcfg
        assert QSettings().value(LEGACY_KEY_SETTING) is None
        assert QSettings().value(AUTHCFG_SETTING) == authcfg
        assert dock.key_edit.text() == ""
    finally:
        dock.cleanup()


def _recording_messages(dock):
    messages = []
    show = dock._message

    def record(text, level=Qgis.MessageLevel.Info):
        messages.append(text)
        show(text, level)

    dock._message = record
    return messages


def test_saving_a_pasted_key_stores_it_only_in_the_auth_database(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    messages = _recording_messages(dock)
    # A different key an earlier session could not move yet.
    other = "glk_older_key_bbbbbbbbbbbbbbbbbbbb"  # pragma: allowlist secret
    QSettings().setValue(LEGACY_KEY_SETTING, other)
    try:
        dock.url_edit.setText("https://gis.example.org")
        dock.key_edit.setText(TEST_KEY)
        dock.save_key.setChecked(True)
        dock.connect_server()
        profile = QSettings()
        authcfg = profile.value(AUTHCFG_SETTING)
        assert authcfg and dock.auth_select.configId() == authcfg
        loaded, config = auth_manager.loadAuthenticationConfig(
            authcfg, QgsAuthMethodConfig(), True
        )
        assert loaded and config.config("X-Api-Key") == TEST_KEY
        assert profile.value(LEGACY_KEY_SETTING) == other
        assert all(
            TEST_KEY not in str(profile.value(name)) for name in profile.allKeys()
        )
        assert dock.key_edit.text() == ""
        assert messages and not any(TEST_KEY in text for text in messages)
    finally:
        dock.cleanup()


def test_a_cancelled_migration_keeps_the_key_through_the_session(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    _legacy(clean_profile_settings)
    # Without a master password in memory, headless QGIS refuses the prompt
    # the way a user cancelling it does.
    auth_manager.clearMasterPassword()
    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        assert QSettings().value(LEGACY_KEY_SETTING) == TEST_KEY
        assert QSettings().value(AUTHCFG_SETTING) is None
        assert dock.key_edit.text() == TEST_KEY
        dock.url_edit.setText("https://gis.example.org")
        dock.connect_server()
        assert dock.client is not None
        assert QSettings().value(LEGACY_KEY_SETTING) == TEST_KEY
    finally:
        dock.cleanup()
        assert auth_manager.setMasterPassword(MASTER, True)


def _raise_http_error(request, timeout=0):
    raise HTTPError(request.full_url, 401, "Unauthorized", {}, BytesIO(b"{}"))


def _raise_network_error(request, timeout=0):
    raise OSError("connection refused")


def _not_geolens(request, timeout=0):
    return BytesIO(b'{"hello": "world"}')


@pytest.mark.parametrize(
    "url, opener, unusable_auth, shown",
    [
        ("https://gis.example.org", _raise_http_error, False, "401"),
        ("https://gis.example.org", _raise_network_error, False, "connection refused"),
        ("https://gis.example.org", _not_geolens, False, "GeoLens"),
        ("", None, False, "HTTP(S) server URL"),
        # Local server, real QGIS transport: the config sends no X-Api-Key.
        (None, None, True, "X-Api-Key"),
    ],
    ids=["http-error", "network-error", "not-geolens", "bad-url", "unusable-auth"],
)
def test_a_failed_connect_clears_the_previous_catalog(
    qgis_iface,
    auth_manager,
    clean_profile_settings,
    server,
    url,
    opener,
    unusable_auth,
    shown,
):
    from geolens_qgis.dialogs import GeoLensDock

    authcfg = ""
    if unusable_auth:
        config = QgsAuthMethodConfig("APIHeader")
        config.setName("not GeoLens")
        config.setConfig("X-Other", "value")
        stored, config = auth_manager.storeAuthenticationConfig(config)
        assert stored
        authcfg = config.id()
    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        dock.url_edit.setText("https://gis.example.org")
        dock.connect_server()
        _select(dock, "Roads")
        assert dock.tiles_button.isEnabled() and dock.editing_enabled
        dock._opener = opener
        dock.url_edit.setText(server.url if url is None else url)
        dock.auth_select.setConfigId(authcfg)
        dock.connect_server()
        assert shown in dock.status.text()
        assert server.seen == []
        assert dock.results.topLevelItemCount() == 0
        assert dock.datasets == {}
        assert dock.client is None and dock.connection is None
        assert not dock.editing_enabled
        assert not dock.tiles_button.isEnabled()
        assert not dock.features_button.isEnabled()
        assert not dock.metadata_button.isEnabled()
    finally:
        dock.cleanup()
