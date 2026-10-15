"""The GeoLens Browser panel items against a real QGIS, with fake servers."""

import json
import time
from urllib.parse import urlparse

import pytest

pytest.importorskip("pytest_qgis")

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsBrowserModel,
    QgsProject,
    QgsVectorTileLayer,
)
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtGui import QIcon  # noqa: E402
from test_connection import (  # noqa: E402
    TEST_KEY,
    _FakeGeoLens,
    _json,
    _quiet,
    _select,
)

from geolens_qgis import browser  # noqa: E402
from geolens_qgis.client import Dataset, GeoLensClient, GeoLensError  # noqa: E402
from geolens_qgis.connection import QgisTransport, store_api_key  # noqa: E402
from geolens_qgis.connections import SavedConnection  # noqa: E402

WORK = SavedConnection("Work", "https://work.example.org", "workcfg")
PUBLIC = SavedConnection("Public", "https://public.example.org", "")
ROADS = Dataset(
    "roads",
    "Roads",
    "Road <b>centrelines</b>",
    None,
    "LINESTRING",
    3,
    None,
    (1, 2, 3, 4),
    keywords=("transport", "roads"),
    license="CC-BY-4.0",
)
DEM = Dataset("dem", "Elevation", "", "raster_dataset", None, None, 1, None)


@pytest.fixture
def connections(monkeypatch):
    saved = [PUBLIC, WORK]
    monkeypatch.setattr(browser, "saved_connections", lambda: saved)
    return saved


class _Catalog:
    """A client stand-in that records which connection it was built for."""

    def __init__(self, connection, datasets=(ROADS, DEM)):
        self.connection = connection
        self.datasets = list(datasets)
        self.searches = []

    def search(self, query="", limit=50, sort_by=""):
        self.searches.append((limit, sort_by))
        return self.datasets

    def metadata_url(self, dataset_id):
        return f"{self.connection.url}/datasets/{dataset_id}"


class _Factory:
    def __init__(self, **datasets):
        self.built = []
        self._datasets = datasets

    def __call__(self, connection):
        client = _Catalog(connection, self._datasets.get(connection.name, (ROADS, DEM)))
        self.built.append(client)
        return client


class _Dock:
    def __init__(self):
        self.calls = []

    def add_dataset_tiles(self, client, dataset):
        self.calls.append(("tiles", client, dataset))

    def add_dataset_features(self, client, dataset):
        self.calls.append(("features", client, dataset))


class _Browser:
    """The Browser panel's model with the GeoLens items, built as QGIS builds them.

    QGIS owns the items and lists connections on a worker thread.
    """

    def __init__(self, dock, factory):
        provider = browser.GeoLensDataItemProvider(lambda: dock, QIcon(), factory)
        self.registry = QgsApplication.dataItemProviderRegistry()
        self.registry.addProvider(provider)
        self.model = QgsBrowserModel()
        self.model.initialize()
        (self.root,) = [
            self.model.dataItem(self.model.index(row, 0))
            for row in range(self.model.rowCount())
            if self.model.data(self.model.index(row, 0)) == "GeoLens"
        ]

    def children(self, item):
        # Read through the model: on QGIS 3, QgsDataItem.children() hands
        # Python base-class copies and invalidates the plugin's own wrappers.
        index = self.model.findItem(item)
        self.model.fetchMore(index)
        deadline = time.monotonic() + 10
        while item.state() != Qgis.BrowserItemState.Populated:
            assert time.monotonic() < deadline, f"{item.name()} was not listed"
            QCoreApplication.processEvents()
            time.sleep(0.01)
        return [
            self.model.dataItem(self.model.index(row, 0, index))
            for row in range(self.model.rowCount(index))
        ]

    def connection(self, name):
        (item,) = [c for c in self.children(self.root) if c.name() == name]
        return item

    def datasets(self, name):
        return {child.name(): child for child in self.children(self.connection(name))}

    def close(self):
        self.registry.removeProvider(self.registry.provider("GeoLens"))


@pytest.fixture
def browse(real_qgis):
    opened = []

    def open_browser(dock, factory):
        opened.append(_Browser(dock, factory))
        return opened[-1]

    yield open_browser
    for panel in opened:
        panel.close()


def test_plugin_registers_and_removes_the_browser_items(real_qgis, qgis_iface):
    from geolens_qgis import classFactory

    registry = QgsApplication.dataItemProviderRegistry()
    plugin = classFactory(qgis_iface)
    try:
        plugin.initGui()
        assert registry.provider("GeoLens") is plugin.browser_provider
        qgis_iface.registerCustomDropHandler.assert_called_with(plugin.drop_handler)
        handler = plugin.drop_handler
        dock = plugin._ensure_dock()
        assert dock is plugin._ensure_dock() and dock.isHidden()
        plugin.toggle_dock()
        assert not dock.isHidden()
    finally:
        plugin.unload()
    assert registry.provider("GeoLens") is None
    assert "GeoLens" not in [provider.name() for provider in registry.providers()]
    qgis_iface.unregisterCustomDropHandler.assert_called_with(handler)
    assert plugin.dock is None


def test_root_lists_saved_connections_and_their_datasets(browse, connections):
    factory = _Factory()
    panel = browse(_Dock(), factory)
    root = panel.root
    assert root.path() == "geolens:" and root.toolTip()
    children = panel.children(root)
    assert [(c.name(), c.path(), c.toolTip()) for c in children] == [
        ("Public", "geolens:/Public", "https://public.example.org"),
        ("Work", "geolens:/Work", "https://work.example.org"),
    ]
    # Children are listed lazily, when a connection is expanded.
    assert factory.built == []
    datasets = {child.name(): child for child in panel.children(children[1])}
    assert [client.connection for client in factory.built] == [WORK]
    assert factory.built[0].searches == [(browser.BROWSER_LIMIT, "title")]
    roads, dem = datasets["Roads"], datasets["Elevation"]
    assert roads.path() == "geolens:/Work/roads"
    assert roads.toolTip() == "<p>Road &lt;b&gt;centrelines&lt;/b&gt;</p>"
    assert dem.toolTip() == "<p>Elevation</p>"
    assert not roads.icon().isNull() and not dem.icon().isNull()
    assert [a.text() for a in roads.actions(None)] == [
        "Add Vector Tiles",
        "Add Features (Editable)",
        "Open Metadata",
    ]
    assert [a.text() for a in dem.actions(None)] == [
        "Add Raster Tiles",
        "Open Metadata",
    ]
    assert all(a.toolTip() for a in roads.actions(None) + dem.actions(None))
    assert [a.text() for a in children[1].actions(None)] == ["Refresh"]


def test_refresh_replaces_a_connection_saved_again_under_the_same_name(
    browse, connections
):
    factory = _Factory()
    panel = browse(_Dock(), factory)
    assert [child.connection for child in panel.children(panel.root)] == [PUBLIC, WORK]
    panel.datasets("Public")
    moved = SavedConnection("Work", "https://new.example.org", "newcfg")
    connections[1] = moved
    {a.text(): a for a in panel.root.actions(None)}["Refresh"].trigger()
    public, work = panel.children(panel.root)
    assert (public.connection, work.connection) == (PUBLIC, moved)
    # The unchanged connection keeps its listing.
    assert public.state() == Qgis.BrowserItemState.Populated
    assert work.state() == Qgis.BrowserItemState.NotPopulated
    assert sorted(panel.datasets("Work")) == ["Elevation", "Roads"]
    assert factory.built[-1].connection == moved


def test_a_large_catalog_ends_with_a_pointer_to_the_panel(
    browse, connections, monkeypatch
):
    monkeypatch.setattr(browser, "BROWSER_LIMIT", 2)
    panel = browse(_Dock(), _Factory())
    more = panel.children(panel.connection("Work"))
    (pointer,) = [item for item in more if isinstance(item, browser.GeoLensMoreItem)]
    assert sorted(item.name() for item in more) == [
        "Elevation",
        "More datasets in the GeoLens panel",
        "Roads",
    ]
    assert "first 2 datasets" in pointer.toolTip()
    assert max(item.sortKey() for item in more) == pointer.sortKey()


def test_a_failed_listing_shows_the_error(browse, connections):
    def failing(connection):
        raise GeoLensError("GeoLens request failed (HTTP 401)")

    assert list(browse(_Dock(), failing).datasets("Work")) == [
        "GeoLens request failed (HTTP 401)"
    ]


def test_a_malformed_catalog_fails_only_its_own_listing(browse, connections):
    # A title that isn't text would raise on QGIS's worker thread.
    broken = Dataset("x", {"en": "X"}, "", None, None, None, None, None)
    panel = browse(_Dock(), _Factory(Work=(broken,)))
    (error,) = panel.datasets("Work")
    assert error.startswith("GeoLens returned a catalog QGIS can't list")
    assert sorted(panel.datasets("Public")) == ["Elevation", "Roads"]


def test_double_click_and_actions_use_the_client_that_listed_the_dataset(
    browse, connections
):
    dock, factory = _Dock(), _Factory()
    datasets = browse(dock, factory).datasets("Work")
    (client,) = factory.built
    assert datasets["Roads"].handleDoubleClick() is True
    actions = {a.text(): a for a in datasets["Roads"].actions(None)}
    actions["Add Vector Tiles"].trigger()
    actions["Add Features (Editable)"].trigger()
    assert dock.calls == [
        ("tiles", client, ROADS),
        ("tiles", client, ROADS),
        ("features", client, ROADS),
    ]


def test_a_drop_adds_the_dataset_with_its_own_connection(browse, connections):
    dock, factory = _Dock(), _Factory()
    datasets = browse(dock, factory).datasets("Work")
    (uri,) = datasets["Roads"].mimeUris()
    assert (uri.layerType, uri.providerKey, uri.name) == ("custom", "geolens", "Roads")
    assert datasets["Roads"].hasDragEnabled()
    handler = browser.GeoLensDropHandler(lambda: dock, factory)
    assert handler.customUriProviderKey() == "geolens"
    handler.handleCustomUriDrop(uri)
    ((kind, client, dataset),) = dock.calls
    assert kind == "tiles" and dataset == ROADS
    assert client is factory.built[-1] and client.connection == WORK


@pytest.mark.parametrize(
    "payload",
    [
        # The saved connection named here points at another server now.
        {"connection": "Work", "url": "https://attacker.example"},
        {"connection": "Gone", "url": WORK.url},
        {"dataset": {"id": 1}},
        {"dataset": {"keywords": "ab"}},
        {"dataset": {"license": 4}},
        "not json",
    ],
)
def test_a_drop_naming_no_matching_connection_adds_nothing(
    real_qgis, connections, payload
):
    from qgis.core import QgsMimeDataUtils

    if isinstance(payload, dict):
        base = json.loads(browser.drop_payload(WORK, ROADS))
        changes = dict(payload)
        base["dataset"] |= changes.pop("dataset", {})
        payload = json.dumps(base | changes)
    dock, factory = _Dock(), _Factory()
    uri = QgsMimeDataUtils.Uri()
    uri.uri = payload
    browser.GeoLensDropHandler(lambda: dock, factory).handleCustomUriDrop(uri)
    assert dock.calls == [] and factory.built == []


def test_the_default_client_carries_only_its_own_connections_auth_config(real_qgis):
    work, public = browser.client_for(WORK), browser.client_for(PUBLIC)
    assert work.base_url == WORK.url and public.base_url == PUBLIC.url
    assert isinstance(work._opener, QgisTransport) and work._opener.authcfg == "workcfg"
    assert public._opener.authcfg == ""


def test_listing_two_servers_sends_each_key_to_its_own_server_only(browse, monkeypatch):
    from test_connection import MASTER, _Server

    manager = QgsApplication.authManager()
    assert manager.setMasterPassword(MASTER, True)
    first, second = _Server(), _Server()
    try:
        for server in (first, second):
            server.routes["/api/search/datasets/"] = _json(
                {"features": [{"id": "d", "properties": {"title": server.url}}]}
            )
        other_key = TEST_KEY.replace("a", "b")
        saved = [
            SavedConnection(
                "First",
                first.url,
                store_api_key(TEST_KEY, first.url, _quiet, "", manager),
            ),
            SavedConnection(
                "Second",
                second.url,
                store_api_key(other_key, second.url, _quiet, "", manager),
            ),
        ]
        monkeypatch.setattr(browser, "saved_connections", lambda: saved)
        panel = browse(_Dock(), browser.client_for)
        listed = {name: list(panel.datasets(name)) for name in ("First", "Second")}
        assert listed == {"First": [first.url], "Second": [second.url]}
        assert first.keys_seen() == [TEST_KEY]
        assert second.keys_seen() == [other_key]
    finally:
        first.httpd.shutdown()
        second.httpd.shutdown()


def _roads_layers():
    """The Roads tile layer and feature layer, in that order."""
    layers = QgsProject.instance().mapLayersByName("Roads")
    return sorted(layers, key=lambda layer: not isinstance(layer, QgsVectorTileLayer))


def _connect_dock(dock, url):
    """Point the dock at `url` and connect, as a user would."""
    dock.use_server(url)
    dock.connect_server()


class _CountingGeoLens(_FakeGeoLens):
    def __init__(self):
        self.paths = []

    def __call__(self, request, timeout=0):
        self.paths.append(urlparse(request.full_url).path)
        return super().__call__(request, timeout)


def test_browser_layers_renew_tokens_and_sync_with_their_own_server(
    qgis_iface, clean_browser_settings, browse, connections
):
    from geolens_qgis.dialogs import GeoLensDock

    dock_server, work_server = _CountingGeoLens(), _CountingGeoLens()
    dock = GeoLensDock(qgis_iface, opener=dock_server)
    try:
        _connect_dock(dock, "https://other.example.org")
        dock.view_only.setChecked(False)
        dock_server.paths.clear()
        work = GeoLensClient(WORK.url, opener=work_server)
        tagged = []
        tag = dock._tag_layer
        dock._tag_layer = lambda layer, dataset, client=None: (
            tagged.append(client),
            tag(layer, dataset, client=client),
        )
        datasets = browse(dock, lambda connection: work).datasets("Work")
        roads = datasets["Roads"]
        roads.handleDoubleClick()
        {a.text(): a for a in roads.actions(None)}["Add Features (Editable)"].trigger()
        assert tagged == [work, work]
        # The layer-info detail read and the sync table-id reads go to the
        # Browser connection's server, not the panel's.
        assert "/api/datasets/roads" in work_server.paths
        assert dock_server.paths == []
        tiles, features = _roads_layers()
        for layer in (tiles, features):
            assert layer.customProperty("geolens/server") == WORK.url
            assert layer.customProperty("geolens/metadata_url") == work.metadata_url(
                "roads"
            )
        assert features.id() in dock.baselines
        timer = dock.refresh_timers[tiles.id()]
        work_server.paths.clear()
        timer.timeout.emit()
        assert work_server.paths == ["/api/tiles/token/roads/"]
        assert dock_server.paths == []
        assert tiles.id() in dock.refresh_timers
    finally:
        dock.cleanup()
        QgsProject.instance().clear()


def test_the_dock_buttons_still_add_from_the_dock_connection(
    qgis_iface, clean_browser_settings
):
    from geolens_qgis.dialogs import GeoLensDock

    server = _CountingGeoLens()
    dock = GeoLensDock(qgis_iface, opener=server)
    try:
        _connect_dock(dock, "https://gis.example.org")
        dock.view_only.setChecked(False)
        _select(dock, "Roads")
        dock.add_tiles()
        dock.add_features()
        tiles, features = _roads_layers()
        assert tiles.customProperty("geolens/server") == "https://gis.example.org"
        assert features.id() in dock.baselines
        server.paths.clear()
        dock.refresh_timers[tiles.id()].timeout.emit()
        assert server.paths == ["/api/tiles/token/roads/"]
    finally:
        dock.cleanup()
        QgsProject.instance().clear()


def test_saving_or_removing_a_connection_refreshes_the_browser(
    qgis_iface, clean_browser_settings, browse, monkeypatch
):
    from qgis.PyQt.QtWidgets import QMessageBox
    from test_connection import MASTER

    from geolens_qgis.dialogs import GeoLensDock

    assert QgsApplication.authManager().setMasterPassword(MASTER, True)
    panel = browse(_Dock(), _Factory())
    monkeypatch.setattr(qgis_iface, "browserModel", lambda: panel.model, raising=False)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes
    )

    def listed():
        return [(c.name(), c.connection.authcfg) for c in panel.children(panel.root)]

    assert listed() == []
    dock = GeoLensDock(qgis_iface, opener=_FakeGeoLens())
    try:
        dock.use_server("https://gis.example.org")
        assert listed() == [("gis.example.org", "")]
        dock.key_edit.setText(TEST_KEY)
        dock.save_key.setChecked(True)
        dock.connect_server()
        ((name, authcfg),) = listed()
        assert name == "gis.example.org" and authcfg
        dock.remove_connection()
        assert listed() == []
    finally:
        dock.cleanup()


@pytest.fixture
def clean_browser_settings(qgis_app):
    from qgis.PyQt.QtCore import QSettings

    QSettings().remove("geolens")
    yield
    QSettings().remove("geolens")
