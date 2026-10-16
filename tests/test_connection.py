"""Credentials and transport against a real QGIS: auth configs, migration, layers."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request

import pytest

pytest.importorskip("pytest_qgis")

from qgis.core import Qgis, QgsApplication, QgsAuthMethodConfig  # noqa: E402
from qgis.PyQt.QtCore import QSettings  # noqa: E402

from dock_tasks import settle  # noqa: E402
from geolens_qgis.client import GeoLensError  # noqa: E402
from geolens_qgis.connection import (  # noqa: E402
    AUTHCFG_SETTING,
    LEGACY_KEY_SERVER_SETTING,
    LEGACY_KEY_SETTING,
    SERVER_SETTING,
    GeoLensConnection,
    migrate_plaintext_key,
    store_api_key,
)
from geolens_qgis.connections import (  # noqa: E402
    SavedConnection,
    current_connection_name,
    remove_connection,
    save_connection,
    saved_connections,
    unique_name,
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
        self.bodies = []
        self.routes = {}
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                headers = {name.lower(): value for name, value in self.headers.items()}
                recorder.seen.append((self.command, self.path, headers))
                length = int(headers.get("content-length") or 0)
                recorder.bodies.append(self.rfile.read(length) if length else b"")
                status, headers, body = recorder.routes.get(
                    urlparse(self.path).path, (404, {}, b'{"detail":"Not here"}')
                )
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

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


def test_a_config_goes_to_any_path_on_the_server_it_was_saved_for(auth_manager, server):
    server.routes["/api/search/datasets/"] = _json({"features": []})
    saved_for = server.url.replace("http://", "HTTP://") + "/geolens/"
    authcfg = store_api_key(TEST_KEY, saved_for, _quiet, "", auth_manager)
    GeoLensConnection(server.url, authcfg=authcfg).client.search()
    assert server.keys_seen() == [TEST_KEY]


_CLIENT_CALLS = {
    "search": lambda client: client.search(),
    "tile-token": lambda client: client.tile_source("d"),
    "features": lambda client: client.features("d"),
    "create": lambda client: client.create_feature("d", {"type": "Feature"}),
    "update": lambda client: client.update_feature("d", 1, {"type": "Feature"}),
    "patch": lambda client: client.patch_feature("d", 1, {"properties": {}}),
    "delete": lambda client: client.delete_feature("d", 1),
}


@pytest.mark.parametrize("call", sorted(_CLIENT_CALLS))
@pytest.mark.parametrize("saved_for", ["other-port", "https", "nothing"])
def test_a_config_is_sent_to_no_other_server(
    auth_manager, server, other_server, saved_for, call
):
    uri = {
        "other-port": other_server.url,
        "https": server.url.replace("http://", "https://"),
        "nothing": "",
    }[saved_for]
    authcfg = store_api_key(TEST_KEY, uri, _quiet, "", auth_manager)
    client = GeoLensConnection(server.url, authcfg=authcfg).client
    with pytest.raises(GeoLensError, match=f"won't send it to {server.url}"):
        _CLIENT_CALLS[call](client)
    assert server.seen == [] and other_server.seen == []


def test_capabilities_send_nothing_to_another_server(
    auth_manager, server, other_server
):
    # Capabilities fall back to defaults on any error, so only the traffic shows.
    authcfg = store_api_key(TEST_KEY, other_server.url, _quiet, "", auth_manager)
    GeoLensConnection(server.url, authcfg=authcfg).client.capabilities()
    assert server.seen == [] and other_server.seen == []


def test_a_pasted_key_travels_with_the_request(server):
    server.routes["/api/search/datasets/"] = _json({"features": []})
    server.routes["/api/datasets/d/features/"] = _json({"id": 3})
    client = GeoLensConnection(server.url, api_key=TEST_KEY).client
    client.search()
    assert client.create_feature("d", {"type": "Feature"}) == (3, "")
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


def test_a_patch_goes_through_qgis_networking_with_the_key(server):
    server.routes["/api/datasets/d/features/7"] = _json({"id": 7})
    client = GeoLensConnection(server.url, api_key=TEST_KEY).client
    client.patch_feature("d", 7, {"properties": {"name": "B"}}, "4242")
    ((method, path, headers),) = server.seen
    assert (method, path) == ("PATCH", "/api/datasets/d/features/7?table_id=4242")
    assert headers["x-api-key"] == TEST_KEY
    assert headers["content-type"] == "application/json"
    assert json.loads(server.bodies[0]) == {"properties": {"name": "B"}}


def test_a_patch_is_not_redirected_and_reports_the_error(server, other_server):
    server.routes["/api/datasets/d/features/7"] = (
        307,
        {"Location": other_server.url + "/landed"},
        b"",
    )
    client = GeoLensConnection(server.url, api_key=TEST_KEY).client
    with pytest.raises(GeoLensError, match="redirected"):
        client.patch_feature("d", 7, {"properties": {"name": "B"}})
    assert other_server.seen == []
    server.routes["/api/datasets/d/features/7"] = (
        409,
        {"Content-Type": "application/json"},
        b'{"detail": {"code": "dataset_replaced", "message": "Read it again."}}',
    )
    with pytest.raises(GeoLensError, match=r"HTTP 409\): Read it again\.$") as caught:
        client.patch_feature("d", 7, {"properties": {"name": "B"}})
    assert (caught.value.status, caught.value.code) == (409, "dataset_replaced")


@pytest.mark.parametrize("method", ["PATCH", "PUT", "POST", "DELETE"])
def test_a_write_that_times_out_is_an_error(method):
    from qgis.core import QgsNetworkAccessManager

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    url = "http://127.0.0.1:%d" % silent.getsockname()[1]
    client = GeoLensConnection(url, api_key=TEST_KEY).client
    write = {
        "PATCH": lambda: client.patch_feature("d", 7, {"properties": {}}),
        "PUT": lambda: client.update_feature("d", 7, {"properties": {}}),
        "POST": lambda: client.create_feature("d", {"properties": {}}),
        "DELETE": lambda: client.delete_feature("d", 7),
    }[method]
    previous = QgsNetworkAccessManager.timeout()
    QgsNetworkAccessManager.setTimeout(500)
    started = time.monotonic()
    try:
        with pytest.raises(GeoLensError, match="Could not reach GeoLens"):
            write()
    finally:
        QgsNetworkAccessManager.setTimeout(previous)
        silent.close()
    assert time.monotonic() - started < 10


def test_a_config_that_sends_no_api_key_is_refused(auth_manager, server):
    server.routes["/api/search/datasets/"] = _json({"features": []})
    config = QgsAuthMethodConfig("APIHeader")
    config.setName("not GeoLens")
    config.setUri(server.url)
    config.setConfig("X-Other", "value")
    stored, config = auth_manager.storeAuthenticationConfig(config)
    assert stored
    with pytest.raises(GeoLensError, match="X-Api-Key"):
        GeoLensConnection(server.url, authcfg=config.id()).client.search()
    assert server.seen == []


def test_an_unusable_auth_config_fails_instead_of_going_anonymous(auth_manager, server):
    server.routes["/api/search/datasets/"] = _json({"features": []})
    with pytest.raises(GeoLensError, match="no longer exists"):
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
    assert settings.value(LEGACY_KEY_SERVER_SETTING) is None
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
    assert settings.value(LEGACY_KEY_SERVER_SETTING) == "https://gis.example.org"
    assert settings.value(AUTHCFG_SETTING) is None
    assert events[-1][1] == Qgis.MessageLevel.Warning


def test_migration_binds_the_key_to_the_server_it_was_saved_with(
    auth_manager, settings
):
    _legacy(settings)
    # A later session connected elsewhere and overwrote the server setting.
    settings.setValue(LEGACY_KEY_SERVER_SETTING, "https://first.example.org")
    authcfg = migrate_plaintext_key(settings, _quiet, auth_manager)
    loaded, config = auth_manager.loadAuthenticationConfig(
        authcfg, QgsAuthMethodConfig(), False
    )
    assert loaded and config.uri() == "https://first.example.org"


def test_migration_keeps_a_key_saved_without_a_server(settings):
    settings.setValue(LEGACY_KEY_SETTING, TEST_KEY)
    events = []
    recording = _RecordingAuthManager(events)
    assert migrate_plaintext_key(settings, lambda *e: events.append(e), recording) == ""
    assert "store" not in events
    assert settings.value(LEGACY_KEY_SETTING) == TEST_KEY


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
            "/api/datasets/roads/features/1": {
                "type": "Feature",
                "id": 1,
                "geometry": {"type": "Point", "coordinates": [1, 2]},
                "properties": {"name": "A"},
                "table_id": "4242",
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


def _add_connection(dock, name, url, authcfg=""):
    save_connection(SavedConnection(name, url, authcfg))
    dock._refresh_connections(name)


def _choose(dock, name):
    dock.connection_combo.setCurrentIndex(dock.connection_combo.findData(name))


def test_no_key_in_any_layer_uri_saved_project_or_setting(
    qgis_iface, clean_profile_settings, tmp_path
):
    from qgis.core import QgsProject

    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        dock.use_server("https://gis.example.org")
        dock.key_edit.setText(TEST_KEY)
        dock.connect_server()
        settle(dock)
        assert dock.client is not None and dock.editing_enabled
        dock.view_only.setChecked(False)
        _select(dock, "Roads")
        dock.add_tiles()
        settle(dock)
        dock.add_features()
        settle(dock)
        _select(dock, "dem")
        dock.add_tiles()
        settle(dock)
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


def test_selecting_an_edited_layer_enables_sync(qgis_iface, clean_profile_settings):
    from qgis.core import QgsProject

    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        dock.use_server("https://gis.example.org")
        dock.key_edit.setText(TEST_KEY)
        dock.connect_server()
        settle(dock)
        dock.view_only.setChecked(False)
        _select(dock, "Roads")
        dock.add_features()
        settle(dock)
        (layer,) = QgsProject.instance().mapLayers().values()
        assert not dock.sync_button.isEnabled()
        qgis_iface.setActiveLayer(layer)
        # QGIS emits this with the layer; pytest-qgis declares a canvas argument.
        qgis_iface.currentLayerChanged.emit(qgis_iface.mapCanvas())
        assert dock.sync_button.isEnabled()
    finally:
        dock.cleanup()
        QgsProject.instance().clear()


def test_opening_the_dock_moves_a_saved_key_into_a_connection(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    _legacy(clean_profile_settings)
    dock = GeoLensDock(qgis_iface)
    try:
        (saved,) = saved_connections()
        assert saved.name == "gis.example.org"
        assert saved.url == "https://gis.example.org"
        loaded, config = auth_manager.loadAuthenticationConfig(
            saved.authcfg, QgsAuthMethodConfig(), True
        )
        assert loaded and config.config("X-Api-Key") == TEST_KEY
        assert config.uri() == "https://gis.example.org"
        assert current_connection_name() == saved.name
        assert dock.connection_combo.currentData() == saved.name
        profile = QSettings()
        for name in (
            LEGACY_KEY_SETTING,
            LEGACY_KEY_SERVER_SETTING,
            SERVER_SETTING,
            AUTHCFG_SETTING,
        ):
            assert profile.value(name) is None
        assert all(
            TEST_KEY not in str(profile.value(name)) for name in profile.allKeys()
        )
        assert dock.key_edit.text() == ""
    finally:
        dock.cleanup()


def test_a_key_moved_while_another_config_was_selected_gets_a_connection(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    _legacy(clean_profile_settings)
    chosen = store_api_key(
        "glk_other_key_cccccccccccccccccccc",  # pragma: allowlist secret
        "https://other.example.org",
        _quiet,
        "",
        auth_manager,
    )
    clean_profile_settings.setValue(AUTHCFG_SETTING, chosen)
    dock = GeoLensDock(qgis_iface)
    try:
        connections = {c.name: c for c in saved_connections()}
        assert set(connections) == {"gis.example.org", "other.example.org"}
        assert connections["other.example.org"].authcfg == chosen
        moved = connections["gis.example.org"].authcfg
        loaded, config = auth_manager.loadAuthenticationConfig(
            moved, QgsAuthMethodConfig(), True
        )
        assert loaded and config.config("X-Api-Key") == TEST_KEY
        assert config.uri() == "https://gis.example.org"
        assert current_connection_name() == "gis.example.org"
    finally:
        dock.cleanup()


def test_a_single_server_setting_becomes_one_saved_connection(auth_manager, settings):
    authcfg = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    settings.setValue(SERVER_SETTING, "https://gis.example.org/")
    settings.setValue(AUTHCFG_SETTING, authcfg)
    expected = [SavedConnection("gis.example.org", "https://gis.example.org", authcfg)]
    assert saved_connections(settings) == expected
    assert current_connection_name(settings) == "gis.example.org"
    assert settings.value(SERVER_SETTING) is None
    assert settings.value(AUTHCFG_SETTING) is None
    assert saved_connections(settings) == expected
    remove_connection("gis.example.org", settings)
    assert saved_connections(settings) == []
    assert current_connection_name(settings) == ""


def test_a_single_server_config_saved_elsewhere_gets_its_own_connection(
    auth_manager, settings
):
    authcfg = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    settings.setValue(SERVER_SETTING, "https://other.example.org:8443")
    settings.setValue(AUTHCFG_SETTING, authcfg)
    assert saved_connections(settings) == [
        SavedConnection("gis.example.org", "https://gis.example.org", authcfg),
        SavedConnection("other.example.org:8443", "https://other.example.org:8443", ""),
    ]
    assert current_connection_name(settings) == "other.example.org:8443"


def test_an_unmoved_key_keeps_its_server_when_the_server_setting_goes(settings):
    _legacy(settings)
    saved_connections(settings)
    assert settings.value(SERVER_SETTING) is None
    assert settings.value(LEGACY_KEY_SERVER_SETTING) == "https://gis.example.org"


def test_names_are_unique_ignoring_case(settings):
    save_connection(
        SavedConnection("Demo", "https://a.example.org", ""), settings=settings
    )
    assert unique_name("demo", settings) == "demo (2)"
    save_connection(
        SavedConnection("Renamed", "https://a.example.org", ""), "Demo", settings
    )
    assert [c.name for c in saved_connections(settings)] == ["Renamed"]


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
        dock.use_server("https://gis.example.org")
        dock.key_edit.setText(TEST_KEY)
        dock.save_key.setChecked(True)
        dock.connect_server()
        settle(dock)
        profile = QSettings()
        (saved,) = saved_connections()
        loaded, config = auth_manager.loadAuthenticationConfig(
            saved.authcfg, QgsAuthMethodConfig(), True
        )
        assert loaded and config.config("X-Api-Key") == TEST_KEY
        assert config.uri() == "https://gis.example.org"
        assert profile.value(LEGACY_KEY_SETTING) == other
        assert all(
            TEST_KEY not in str(profile.value(name)) for name in profile.allKeys()
        )
        assert dock.key_edit.text() == ""
        assert messages and not any(TEST_KEY in text for text in messages)
    finally:
        dock.cleanup()


def _dialog(connection=None, taken=()):
    from geolens_qgis.dialogs.connection_dialog import ConnectionDialog

    return ConnectionDialog(None, taken, connection)


def _accepted(dialog):
    from qgis.PyQt.QtWidgets import QDialog

    return dialog.result() == QDialog.DialogCode.Accepted


def test_changing_the_server_deselects_a_config_saved_for_another(
    qgis_app, auth_manager
):
    authcfg = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    dialog = _dialog(SavedConnection("gis", "https://gis.example.org", authcfg))
    dialog.url_edit.setText("HTTPS://GIS.example.org:443/geolens")
    dialog.url_edit.editingFinished.emit()
    assert dialog.auth_select.configId() == authcfg
    dialog.url_edit.setText("https://other.example.org")
    dialog.url_edit.editingFinished.emit()
    assert dialog.auth_select.configId() == ""
    assert "saved for https://gis.example.org" in dialog.notice.text()
    assert "https://other.example.org" in dialog.notice.text()


def test_connecting_with_a_config_saved_for_another_server_sends_nothing(
    qgis_iface, auth_manager, clean_profile_settings, server, other_server
):
    from geolens_qgis.dialogs import GeoLensDock

    authcfg = store_api_key(TEST_KEY, other_server.url, _quiet, "", auth_manager)
    dock = GeoLensDock(qgis_iface)
    messages = _recording_messages(dock)
    try:
        _add_connection(dock, "here", server.url, authcfg)
        dock.connect_server()
        settle(dock)
        assert server.seen == [] and other_server.seen == []
        assert dock.client is None
        assert f"won't send it to {server.url}" in messages[-1]
        assert not dock.connection_box.isCollapsed()
    finally:
        dock.cleanup()


def test_a_malformed_server_url_deselects_the_config_and_is_not_saved(
    qgis_app, auth_manager
):
    authcfg = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    dialog = _dialog(SavedConnection("gis", "https://gis.example.org", authcfg))
    dialog.url_edit.setText("https://[")
    dialog.url_edit.editingFinished.emit()
    assert dialog.auth_select.configId() == ""
    assert "won't send it to https://[" in dialog.notice.text()
    dialog.accept()
    assert not _accepted(dialog) and dialog.connection() is None
    assert "HTTP(S) URL" in dialog.notice.text()


def test_ok_right_after_a_server_edit_still_refuses_once(qgis_app, auth_manager):
    saved_elsewhere = store_api_key(
        TEST_KEY, "https://other.example.org", _quiet, "", auth_manager
    )
    saved_here = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    dialog = _dialog()
    dialog.name_edit.setText("gis")
    dialog.auth_select.setConfigId(saved_elsewhere)
    dialog.url_edit.setText("https://gis.example.org")
    # Clicking OK takes focus from the Server URL field first.
    dialog.url_edit.editingFinished.emit()
    dialog.accept()
    assert not _accepted(dialog)
    assert "Click OK again" in dialog.notice.text()
    dialog.accept()
    assert dialog.connection() == SavedConnection("gis", "https://gis.example.org", "")
    dialog = _dialog()
    dialog.name_edit.setText("gis")
    dialog.url_edit.setText("https://gis.example.org")
    dialog.auth_select.setConfigId(saved_here)
    dialog.accept()
    assert dialog.connection().authcfg == saved_here


def test_a_config_edited_out_of_its_server_is_never_saved(qgis_app, auth_manager):
    authcfg = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    dialog = _dialog(SavedConnection("gis", "https://gis.example.org", authcfg))
    # A URL set without the field ever losing focus.
    dialog.url_edit.setText("https://other.example.org")
    dialog.accept()
    assert not _accepted(dialog)
    assert "won't send it to https://other.example.org" in dialog.notice.text()


def test_a_refusal_is_dropped_when_the_server_changes_again(qgis_app, auth_manager):
    authcfg = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    dialog = _dialog(SavedConnection("gis", "https://gis.example.org", authcfg))
    dialog.url_edit.setText("https://b.example.org")
    dialog.url_edit.editingFinished.emit()
    assert dialog.auth_select.configId() == ""
    dialog.url_edit.setText("https://c.example.org")
    dialog.url_edit.editingFinished.emit()
    dialog.accept()
    assert dialog.connection() == SavedConnection("gis", "https://c.example.org", "")


def test_a_config_comes_back_when_the_server_does(qgis_app, auth_manager):
    authcfg = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    dialog = _dialog(SavedConnection("gis", "https://gis.example.org", authcfg))
    dialog.url_edit.setText("https://gis.example.orgg")
    dialog.url_edit.editingFinished.emit()
    assert dialog.auth_select.configId() == ""
    dialog.url_edit.setText("https://GIS.example.org/")
    dialog.url_edit.editingFinished.emit()
    assert dialog.auth_select.configId() == authcfg
    assert "selected again" in dialog.notice.text()
    dialog.auth_select.setConfigId("")
    dialog.url_edit.setText("https://other.example.org")
    dialog.url_edit.editingFinished.emit()
    dialog.url_edit.setText("https://gis.example.org")
    dialog.url_edit.editingFinished.emit()
    assert dialog.auth_select.configId() == ""


def test_a_dialog_name_must_be_new_and_usable_as_a_setting(qgis_app):
    dialog = _dialog(taken=["Demo"])
    dialog.url_edit.setText("https://gis.example.org")
    for name in ("", "a/b", "a\\b", "demo"):
        dialog.name_edit.setText(name)
        dialog.accept()
        assert not _accepted(dialog)
    dialog.name_edit.setText("Demo 2")
    dialog.accept()
    assert dialog.connection().name == "Demo 2"


def test_a_key_pasted_in_the_dialog_is_saved_for_its_server_only(
    qgis_app, auth_manager, clean_profile_settings
):
    dialog = _dialog()
    dialog.name_edit.setText("gis")
    dialog.url_edit.setText("gis.example.org/")
    dialog.key_edit.setText(TEST_KEY)
    dialog.accept()
    saved = dialog.connection()
    assert saved.url == "https://gis.example.org"
    loaded, config = auth_manager.loadAuthenticationConfig(
        saved.authcfg, QgsAuthMethodConfig(), True
    )
    assert loaded and config.config("X-Api-Key") == TEST_KEY
    assert config.uri() == "https://gis.example.org"
    assert dialog.key_edit.text() == ""


def test_connecting_with_a_config_saved_for_this_server_sends_it(
    qgis_iface, auth_manager, clean_profile_settings, server
):
    from geolens_qgis.dialogs import GeoLensDock

    server.routes["/api/search/datasets/"] = _json({"features": []})
    authcfg = store_api_key(TEST_KEY, server.url + "/", _quiet, "", auth_manager)
    dock = GeoLensDock(qgis_iface)
    try:
        _add_connection(dock, "here", server.url, authcfg)
        dock.connect_server()
        settle(dock)
        assert dock.client is not None
        assert set(server.keys_seen()) == {TEST_KEY}
        assert dock.connection_box.isCollapsed()
    finally:
        dock.cleanup()


def test_switching_connections_sends_each_server_only_its_own_credentials(
    qgis_iface, auth_manager, clean_profile_settings, server, other_server
):
    from qgis.PyQt.QtTest import QTest

    from geolens_qgis.dialogs import GeoLensDock

    other_key = "glk_other_key_dddddddddddddddddddd"  # pragma: allowlist secret
    session_key = "glk_session_key_eeeeeeeeeeeeeeeeee"  # pragma: allowlist secret
    for instance in (server, other_server):
        instance.routes["/api/search/datasets/"] = _json({"features": []})
    here = store_api_key(TEST_KEY, server.url, _quiet, "", auth_manager)
    there = store_api_key(other_key, other_server.url, _quiet, "", auth_manager)
    dock = GeoLensDock(qgis_iface)
    try:
        save_connection(SavedConnection("here", server.url, here))
        save_connection(SavedConnection("there", other_server.url, there))
        save_connection(SavedConnection("there, anonymous", other_server.url, ""))
        dock._refresh_connections("here")
        dock.connect_server()
        settle(dock)
        _choose(dock, "there")
        dock.connect_server()
        settle(dock)
        _choose(dock, "here")
        dock.connect_server()
        settle(dock)
        assert set(server.keys_seen()) == {TEST_KEY}
        assert set(other_server.keys_seen()) == {other_key}
        # A key pasted for this session belongs to its connection.
        _choose(dock, "there, anonymous")
        QTest.keyClicks(dock.key_edit, session_key)
        dock.connect_server()
        settle(dock)
        assert other_server.keys_seen()[-1] == session_key
        _choose(dock, "here")
        assert dock.key_edit.text() == ""
        dock.connect_server()
        settle(dock)
        _choose(dock, "there")
        dock.connect_server()
        settle(dock)
        assert set(server.keys_seen()) == {TEST_KEY}
        assert other_server.keys_seen()[-1] == other_key
        _choose(dock, "there, anonymous")
        assert dock.key_edit.text() == session_key
    finally:
        dock.cleanup()


def test_a_key_is_not_sent_after_its_connection_moves_to_another_server(
    qgis_iface, clean_profile_settings, server, other_server
):
    from qgis.PyQt.QtTest import QTest

    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface)
    messages = _recording_messages(dock)
    try:
        _add_connection(dock, "gis", other_server.url)
        QTest.keyClicks(dock.key_edit, TEST_KEY)
        # Edited by something other than this panel, such as a second dock.
        save_connection(SavedConnection("gis", server.url, ""))
        dock.connect_server()
        settle(dock)
        assert server.seen == [] and other_server.seen == []
        assert dock.client is None and dock.key_edit.text() == ""
        assert "won't send it to" in messages[-1] and TEST_KEY not in messages[-1]
    finally:
        dock.cleanup()


def _answer_dialog(monkeypatch, **fields):
    """Make the connection dialog fill in `fields` and press OK until it closes."""
    from geolens_qgis.dialogs.connection_dialog import ConnectionDialog

    def answer(dialog):
        for name, value in fields.items():
            getattr(dialog, name).setText(value)
        dialog.url_edit.editingFinished.emit()
        dialog.accept()
        if not _accepted(dialog):
            dialog.accept()
        return dialog.result()

    monkeypatch.setattr(ConnectionDialog, "exec", answer)


def test_editing_a_connection_to_another_server_drops_its_credentials(
    qgis_iface, auth_manager, clean_profile_settings, server, other_server, monkeypatch
):
    from qgis.PyQt.QtTest import QTest

    from geolens_qgis.dialogs import GeoLensDock

    other_server.routes["/api/search/datasets/"] = _json({"features": []})
    authcfg = store_api_key(TEST_KEY, server.url, _quiet, "", auth_manager)
    dock = GeoLensDock(qgis_iface)
    try:
        _add_connection(dock, "gis", server.url, authcfg)
        QTest.keyClicks(dock.key_edit, TEST_KEY)
        _answer_dialog(monkeypatch, name_edit="moved", url_edit=other_server.url)
        dock.edit_connection()
        assert saved_connections() == [SavedConnection("moved", other_server.url, "")]
        assert current_connection_name() == "moved"
        assert dock.key_edit.text() == ""
        dock.connect_server()
        settle(dock)
        assert dock.client is not None
        assert set(other_server.keys_seen()) == {None} and server.seen == []
    finally:
        dock.cleanup()


def test_a_new_connection_with_a_pasted_key_connects_with_it(
    qgis_iface, auth_manager, clean_profile_settings, server, monkeypatch
):
    from geolens_qgis.dialogs import GeoLensDock

    server.routes["/api/search/datasets/"] = _json({"features": []})
    dock = GeoLensDock(qgis_iface)
    try:
        _answer_dialog(
            monkeypatch, name_edit="local", url_edit=server.url, key_edit=TEST_KEY
        )
        dock.new_connection()
        (saved,) = saved_connections()
        assert saved.name == "local" and saved.authcfg
        assert dock.connection_combo.currentData() == "local"
        dock.connect_server()
        settle(dock)
        assert set(server.keys_seen()) == {TEST_KEY}
        profile = QSettings()
        assert all(
            TEST_KEY not in str(profile.value(name)) for name in profile.allKeys()
        )
    finally:
        dock.cleanup()


def test_removing_a_connection_keeps_its_auth_config(
    qgis_iface, auth_manager, clean_profile_settings, server, monkeypatch
):
    from qgis.PyQt.QtWidgets import QMessageBox

    from geolens_qgis.dialogs import GeoLensDock

    server.routes["/api/search/datasets/"] = _json({"features": []})
    authcfg = store_api_key(TEST_KEY, server.url, _quiet, "", auth_manager)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes
    )
    dock = GeoLensDock(qgis_iface)
    try:
        save_connection(SavedConnection("first", server.url, authcfg))
        _add_connection(dock, "second", server.url + "/geolens", authcfg)
        _choose(dock, "first")
        dock.remove_connection()
        assert [c.name for c in saved_connections()] == ["second"]
        assert dock.connection_combo.currentData() == "second"
        loaded, _ = auth_manager.loadAuthenticationConfig(
            authcfg, QgsAuthMethodConfig(), False
        )
        assert loaded
        dock.remove_connection()
        assert saved_connections() == [] and current_connection_name() == ""
        assert not dock.connect_button.isEnabled()
        loaded, _ = auth_manager.loadAuthenticationConfig(
            authcfg, QgsAuthMethodConfig(), False
        )
        assert loaded
    finally:
        dock.cleanup()


class _RecordingRequests(_FakeGeoLens):
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout=0):
        self.requests.append(
            (request.full_url, {k.lower(): v for k, v in request.header_items()})
        )
        return super().__call__(request, timeout)


def test_the_demo_preset_connects_anonymously(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    authcfg = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    opener = _RecordingRequests()
    dock = GeoLensDock(qgis_iface, opener=opener)
    try:
        _add_connection(dock, "gis", "https://gis.example.org", authcfg)
        dock.key_edit.setText(TEST_KEY)
        demo = dock.samples.findText("GeoLens demo")
        dock.samples.setCurrentIndex(demo)
        assert dock.samples.currentIndex() == 0
        assert dock.connection_combo.currentData() == "GeoLens demo"
        assert dock.key_edit.text() == ""
        dock.connect_server()
        settle(dock)
        assert dock.client is not None
        assert dock.client.base_url == "https://demo.getgeolens.com"
        assert opener.requests
        for url, headers in opener.requests:
            assert urlparse(url).hostname == "demo.getgeolens.com"
            assert "x-api-key" not in headers
        assert (
            SavedConnection("GeoLens demo", "https://demo.getgeolens.com", "")
            in saved_connections()
        )
        # Choosing it again selects the saved connection instead of a copy.
        _choose(dock, "gis")
        dock.samples.setCurrentIndex(demo)
        assert len(saved_connections()) == 2
    finally:
        dock.cleanup()


def test_use_server_selects_only_a_connection_for_that_server(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    authcfg = store_api_key(
        TEST_KEY, "https://gis.example.org", _quiet, "", auth_manager
    )
    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        save_connection(SavedConnection("gis", "https://gis.example.org", authcfg))
        anonymous = dock.use_server("https://gis.example.org/")
        assert anonymous == SavedConnection(
            "gis.example.org", "https://gis.example.org", ""
        )
        assert dock.use_server("HTTPS://GIS.example.org:443/") == anonymous
        assert dock.connection_combo.currentData() == anonymous.name
        for url in (
            "http://gis.example.org",
            "https://gis.example.org:8443",
            "https://gis.example.org/geolens",
            "https://other.example.org",
        ):
            selected = dock.use_server(url)
            assert selected.authcfg == "" and selected.url == url
            assert dock.connection_combo.currentData() == selected.name
            assert current_connection_name() == selected.name
        assert len(saved_connections()) == 6
        with pytest.raises(ValueError):
            dock.use_server("https://[")
    finally:
        dock.cleanup()


def test_the_demo_preset_sends_no_credential_of_a_saved_demo_connection(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    demo_url = "https://demo.getgeolens.com"
    authcfg = store_api_key(TEST_KEY, demo_url, _quiet, "", auth_manager)
    opener = _RecordingRequests()
    dock = GeoLensDock(qgis_iface, opener=opener)
    try:
        # Sorts before the preset's name, so a first match by URL finds it.
        _add_connection(dock, "Demo with my key", demo_url, authcfg)
        # A plain-text key from an older version, for the same server.
        dock._legacy_key, dock._legacy_key_server = TEST_KEY, demo_url
        dock.samples.setCurrentIndex(dock.samples.findText("GeoLens demo"))
        selected = dock._current_connection()
        assert selected == SavedConnection("GeoLens demo", demo_url, "")
        assert dock.key_edit.text() == ""
        dock.connect_server()
        settle(dock)
        assert dock.client is not None and opener.requests
        assert all("x-api-key" not in headers for _, headers in opener.requests)
        assert SavedConnection("Demo with my key", demo_url, authcfg) in (
            saved_connections()
        )
    finally:
        dock.cleanup()


def test_choosing_another_connection_disconnects_from_the_first(
    qgis_iface, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    opener = _RecordingRequests()
    dock = GeoLensDock(qgis_iface, opener=opener)
    try:
        save_connection(SavedConnection("b", "https://b.example.org", ""))
        first = dock.use_server("https://a.example.org")
        dock.connect_server()
        settle(dock)
        _select(dock, "Roads")
        assert dock.tiles_button.isEnabled()
        # Choosing the connected server again keeps the session.
        assert dock.use_server("https://a.example.org/") == first
        dock._refresh_connections(first.name)
        assert dock.client is not None and dock.tiles_button.isEnabled()
        _choose(dock, "b")
        assert dock.client is None and dock.connection is None
        assert dock.results.topLevelItemCount() == 0 and dock.datasets == {}
        assert not dock.editing_enabled
        for button in (
            dock.tiles_button,
            dock.features_button,
            dock.metadata_button,
            dock.sync_button,
        ):
            assert not button.isEnabled()
        seen = len(opener.requests)
        dock.search()
        settle(dock)
        assert opener.requests[seen:]
        assert all(
            urlparse(url).hostname == "b.example.org"
            for url, _ in opener.requests[seen:]
        )
    finally:
        dock.cleanup()


def test_choosing_a_preset_ends_a_session_with_a_pasted_key(
    qgis_iface, clean_profile_settings
):
    from qgis.PyQt.QtTest import QTest

    from geolens_qgis.dialogs import GeoLensDock

    opener = _RecordingRequests()
    dock = GeoLensDock(qgis_iface, opener=opener)
    try:
        demo = dock.samples.findText("GeoLens demo")
        dock.samples.setCurrentIndex(demo)
        QTest.keyClicks(dock.key_edit, TEST_KEY)
        dock.connect_server()
        settle(dock)
        assert opener.requests[-1][1].get("x-api-key") == TEST_KEY
        dock.samples.setCurrentIndex(demo)
        assert dock.client is None and dock.key_edit.text() == ""
        seen = len(opener.requests)
        dock.search()
        settle(dock)
        assert dock.client is not None and opener.requests[seen:]
        assert all("x-api-key" not in h for _, h in opener.requests[seen:])
        _add_connection(dock, "other", "https://other.example.org")
        _choose(dock, "GeoLens demo")
        assert dock.key_edit.text() == ""
    finally:
        dock.cleanup()


@pytest.mark.parametrize("edited", ["", "cfg0002"])
def test_editing_the_connected_auth_config_disconnects(
    qgis_iface, clean_profile_settings, edited
):
    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        _add_connection(dock, "gis", "https://gis.example.org", "cfg0001")
        dock.connect_server()
        settle(dock)
        assert dock.client is not None
        # What Edit saves when only the authentication changes.
        _add_connection(dock, "gis", "https://gis.example.org", edited)
        assert dock.client is None and dock.results.topLevelItemCount() == 0
        assert not dock.tiles_button.isEnabled()
    finally:
        dock.cleanup()


def test_saving_the_pasted_key_keeps_the_session(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        dock.use_server("https://gis.example.org")
        dock.key_edit.setText(TEST_KEY)
        dock.save_key.setChecked(True)
        dock.connect_server()
        settle(dock)
        (saved,) = saved_connections()
        assert saved.authcfg
        dock._refresh_connections(saved.name)
        assert dock.client is not None
    finally:
        dock.cleanup()


def test_the_form_collapses_after_connecting_and_opens_on_failure(
    qgis_iface, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        dock.use_server("https://gis.example.org")
        dock.connect_server()
        settle(dock)
        assert dock.connection_box.isCollapsed()
        assert "gis.example.org" in dock.connection_box.title()
        dock._opener = _raise_network_error
        dock.connect_server()
        settle(dock)
        assert not dock.connection_box.isCollapsed()
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
        assert saved_connections() == [
            SavedConnection("gis.example.org", "https://gis.example.org", "")
        ]
        assert dock.key_edit.text() == TEST_KEY
        dock.connect_server()
        settle(dock)
        assert dock.client is not None
        assert QSettings().value(LEGACY_KEY_SETTING) == TEST_KEY
    finally:
        dock.cleanup()
        assert auth_manager.setMasterPassword(MASTER, True)


@pytest.fixture
def unmigrated_key(auth_manager, clean_profile_settings):
    """A plain-text key the dock can't move, as when the prompt is cancelled."""
    _legacy(clean_profile_settings)
    auth_manager.clearMasterPassword()
    yield clean_profile_settings
    assert auth_manager.setMasterPassword(MASTER, True)


def test_a_legacy_key_is_filled_in_only_for_its_server(qgis_iface, unmigrated_key):
    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    messages = _recording_messages(dock)
    try:
        assert dock.key_edit.text() == TEST_KEY
        dock.use_server("HTTPS://GIS.example.org:443/geolens")
        assert dock.key_edit.text() == TEST_KEY
        dock.use_server("https://other.example.org")
        assert dock.key_edit.text() == ""
        assert not any(TEST_KEY in text for text in messages)
        assert QSettings().value(LEGACY_KEY_SETTING) == TEST_KEY
        dock.use_server("https://gis.example.org/")
        assert dock.connection_combo.currentData() == "gis.example.org"
        assert dock.key_edit.text() == TEST_KEY
    finally:
        dock.cleanup()


def test_a_legacy_key_for_another_server_is_not_filled_in(
    qgis_iface, auth_manager, unmigrated_key
):
    from geolens_qgis.dialogs import GeoLensDock

    assert auth_manager.setMasterPassword(MASTER, True)
    authcfg = store_api_key(
        TEST_KEY, "https://other.example.org", _quiet, "", auth_manager
    )
    auth_manager.clearMasterPassword()
    unmigrated_key.setValue(LEGACY_KEY_SERVER_SETTING, "https://gis.example.org")
    unmigrated_key.setValue(SERVER_SETTING, "https://other.example.org")
    unmigrated_key.setValue(AUTHCFG_SETTING, authcfg)
    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        assert dock.key_edit.text() == ""
        assert saved_connections() == [
            SavedConnection("other.example.org", "https://other.example.org", authcfg)
        ]
        assert dock.connection_combo.currentData() == "other.example.org"
        dock.connect_server()
        settle(dock)
        assert dock.client is not None
        dock.use_server("https://gis.example.org")
        assert dock.key_edit.text() == TEST_KEY
    finally:
        dock.cleanup()


def test_a_legacy_key_is_never_sent_to_another_server(
    qgis_iface, unmigrated_key, server, other_server
):
    from geolens_qgis.dialogs import GeoLensDock

    server.routes["/api/search/datasets/"] = _json({"features": []})
    unmigrated_key.setValue(LEGACY_KEY_SERVER_SETTING, other_server.url)
    unmigrated_key.setValue(SERVER_SETTING, other_server.url)
    dock = GeoLensDock(qgis_iface)
    try:
        assert dock.key_edit.text() == TEST_KEY
        dock.use_server(server.url)
        assert dock.key_edit.text() == ""
        dock.connect_server()
        settle(dock)
        assert dock.client is not None
        assert set(server.keys_seen()) == {None} and other_server.seen == []
    finally:
        dock.cleanup()


def test_a_legacy_key_without_a_server_is_not_filled_in(
    qgis_iface, auth_manager, clean_profile_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    clean_profile_settings.setValue(LEGACY_KEY_SETTING, TEST_KEY)
    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        dock.use_server("https://gis.example.org")
        assert dock.key_edit.text() == ""
        assert "doesn't record which server" in (
            qgis_iface.messageBar().get_messages(Qgis.MessageLevel.Warning)[-1]
        )
        assert QSettings().value(LEGACY_KEY_SETTING) == TEST_KEY
    finally:
        dock.cleanup()


def _raise_http_error(request, timeout=0):
    raise HTTPError(request.full_url, 401, "Unauthorized", {}, BytesIO(b"{}"))


def _raise_network_error(request, timeout=0):
    raise OSError("connection refused")


def _not_geolens(request, timeout=0):
    return BytesIO(b'{"hello": "world"}')


@pytest.mark.parametrize(
    "url, opener, unusable_auth, shown",
    [
        ("https://gis.example.org", _raise_http_error, False, "refused the request"),
        ("https://gis.example.org", _raise_network_error, False, "reach the server"),
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
        config.setUri(server.url)
        config.setConfig("X-Other", "value")
        stored, config = auth_manager.storeAuthenticationConfig(config)
        assert stored
        authcfg = config.id()
    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        dock.use_server("https://gis.example.org")
        dock.connect_server()
        settle(dock)
        _select(dock, "Roads")
        assert dock.tiles_button.isEnabled() and dock.editing_enabled
        dock._opener = opener
        _add_connection(dock, "second", server.url if url is None else url, authcfg)
        dock.connect_server()
        settle(dock)
        assert (
            shown
            in qgis_iface.messageBar().get_messages(Qgis.MessageLevel.Critical)[-1]
        )
        assert dock.status.text() == "Not connected."
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


class _RecordingGeoLens(_FakeGeoLens):
    """The fake server, plus a log of every write it receives."""

    def __init__(self):
        self.writes = []

    def __call__(self, request, timeout=0):
        if request.get_method() != "GET":
            self.writes.append((request.get_method(), request.full_url))
            return BytesIO(b"{}")
        return super().__call__(request, timeout)


def _load_edited_roads(dock, qgis_iface, server):
    """Load roads from `server`, then change one attribute without committing."""
    dock.use_server(server)
    dock.connect_server()
    settle(dock)
    dock.view_only.setChecked(False)
    _select(dock, "Roads")
    dock.add_features()
    settle(dock)
    from qgis.core import QgsProject

    (layer,) = QgsProject.instance().mapLayersByName("Roads")
    qgis_iface.setActiveLayer(layer)
    layer.startEditing()
    feature = next(layer.getFeatures())
    layer.changeAttributeValue(feature.id(), layer.fields().indexOf("name"), "B")
    return layer


@pytest.mark.parametrize("recorded", ["https://other.example.org", ""])
def test_sync_refuses_a_layer_from_another_server(
    qgis_iface, clean_profile_settings, recorded
):
    from qgis.core import QgsProject

    from geolens_qgis.dialogs import GeoLensDock

    opener = _RecordingGeoLens()
    dock = GeoLensDock(qgis_iface, opener=opener)
    messages = _recording_messages(dock)
    try:
        layer = _load_edited_roads(dock, qgis_iface, "https://gis.example.org")
        layer.setCustomProperty("geolens/server", recorded)
        baseline = json.dumps(dock.baselines[layer.id()], sort_keys=True)
        dock.sync_active_layer()
        settle(dock)
        assert opener.writes == []
        if recorded:
            assert f"came from {recorded}" in messages[-1]
        else:
            assert "doesn't record" in messages[-1]
        # The edit is still pending in the layer and the baseline is unchanged.
        assert layer.isEditable() and layer.isModified()
        assert json.dumps(dock.baselines[layer.id()], sort_keys=True) == baseline
    finally:
        dock.cleanup()
        QgsProject.instance().clear()


def test_a_reopened_layer_from_another_server_is_told_to_reload_it(
    qgis_iface, clean_profile_settings
):
    from qgis.core import QgsProject

    from geolens_qgis.dialogs import GeoLensDock

    opener = _RecordingGeoLens()
    dock = GeoLensDock(qgis_iface, opener=opener)
    messages = _recording_messages(dock)
    try:
        layer = _load_edited_roads(dock, qgis_iface, "https://gis.example.org")
        layer.setCustomProperty("geolens/server", "https://other.example.org")
        del dock.baselines[layer.id()]
        dock.sync_active_layer()
        settle(dock)
        assert "no GeoLens edit baseline" in messages[-1]
        assert opener.writes == []
        assert layer.isEditable() and layer.isModified()
    finally:
        dock.cleanup()
        QgsProject.instance().clear()


def test_sync_after_reconnecting_to_the_same_server_sends_the_edit(
    qgis_iface, clean_profile_settings
):
    from qgis.core import QgsProject

    from geolens_qgis.dialogs import GeoLensDock

    opener = _RecordingGeoLens()
    dock = GeoLensDock(qgis_iface, opener=opener)
    try:
        _load_edited_roads(dock, qgis_iface, "https://gis.example.org")
        dock.use_server("https://other.example.org")
        dock.connect_server()
        settle(dock)
        dock.sync_active_layer()
        settle(dock)
        assert opener.writes == []
        dock.use_server("HTTPS://GIS.example.org:443/")
        dock.connect_server()
        settle(dock)
        dock.sync_active_layer()
        settle(dock)
        assert [(method, urlparse(url).path) for method, url in opener.writes] == [
            ("PATCH", "/api/datasets/roads/features/1")
        ]
    finally:
        dock.cleanup()
        QgsProject.instance().clear()
