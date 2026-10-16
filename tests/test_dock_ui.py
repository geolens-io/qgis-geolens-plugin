"""The dock's list, filters, states and messages against a real QGIS."""

import contextlib
import json
import threading
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("pytest_qgis")

from qgis.core import Qgis, QgsProject  # noqa: E402
from qgis.PyQt.QtCore import QEvent, QLocale, QPoint, Qt  # noqa: E402
from qgis.PyQt.QtGui import QHelpEvent, QImage  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QCheckBox  # noqa: E402

from dock_tasks import settle  # noqa: E402
from geolens_qgis.client import GeoLensError  # noqa: E402
from geolens_qgis.dialogs import geolens_dock  # noqa: E402
from geolens_qgis.dialogs.geolens_dock import GeoLensDock, plain_error  # noqa: E402

SERVER = "https://gis.example.org"
KEY = "glk_only_for_tests_bbbbbbbbbbbbbbbb"  # pragma: allowlist secret


def _png():
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    from qgis.PyQt.QtCore import QBuffer, QByteArray

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def _feature(ident, title, **props):
    return {"id": ident, "properties": {"title": title, **props}}


DATASETS = [
    _feature(
        "roads",
        "Roads",
        record_type="vector_dataset",
        geometry_type="MULTILINESTRING",
        feature_count=1200,
        updated="2026-10-01T00:00:00Z",
        has_quicklook=True,
        quicklook_version="abc123",
    ),
    _feature(
        "parcels",
        "parcels",
        record_type="vector_dataset",
        geometry_type="POLYGON",
        feature_count=1,
        updated="2026-10-10T00:00:00Z",
    ),
    _feature(
        "dem",
        "Elevation",
        record_type="raster_dataset",
        band_count=3,
        updated="2026-09-01T00:00:00Z",
        has_quicklook=True,
        quicklook_version="v9",
    ),
]


class FakeServer:
    """An opener that serves `datasets` and records every request it sees.

    A request for a path in `gates`, or for a (path, search text) pair, waits
    until that event is set.
    """

    def __init__(self, datasets=DATASETS):
        self.datasets = datasets
        self.seen = []
        self.timeouts = {}
        self.quicklook_threads = []
        self.gate = None
        self.version_gates = {}
        self.gates = {}

    def __call__(self, request, timeout=0):
        url = urlparse(request.full_url)
        self.timeouts[url.path] = timeout
        self.seen.append((url.netloc, url.path, request.get_header("X-api-key")))
        path = url.path
        query = parse_qs(url.query).get("q", [""])[0]
        gate = self.gates.get(path) or self.gates.get((path, query))
        if gate is not None:
            gate.wait(10)
        if path == "/api/search/datasets/":
            features = [] if query == "nothing" else self.datasets
            return BytesIO(json.dumps({"features": features}).encode())
        if path == "/api/capabilities":
            return BytesIO(b'{"flags": {"enable_dataset_editing": true}}')
        if path == "/api/datasets/roads/quicklook":
            self.quicklook_threads.append(threading.get_ident())
            if self.gate is not None:
                self.gate.wait(10)
            gate = self.version_gates.get(parse_qs(url.query).get("v", [""])[0])
            if gate is not None:
                gate.wait(10)
            return BytesIO(_png())
        if path == "/api/tiles/token/dem/":
            body = {
                "kind": "raster",
                "tile_url": "/raster-tiles/dem/{z}/{x}/{y}.png?sig=s",
                "expires_in": 600,
            }
            return BytesIO(json.dumps(body).encode())
        raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(b"{}"))

    def thumbnail_requests(self):
        return [seen for seen in self.seen if seen[1].endswith("/quicklook")]


def _connect(dock, url, key=""):
    dock.use_server(url)
    dock.key_edit.setText(key)
    dock.connect_server()
    settle(dock)


@pytest.fixture(autouse=True)
def english_numbers():
    previous = QLocale()
    QLocale.setDefault(QLocale("en_US"))
    yield
    QLocale.setDefault(previous)


@pytest.fixture
def make_dock(qgis_iface, qgis_app):
    from qgis.PyQt.QtCore import QSettings

    QSettings().remove("geolens")
    docks = []

    def make(server=None, connect=True):
        server = server or FakeServer()
        dock = GeoLensDock(qgis_iface, opener=server)
        server.dock = dock
        docks.append(dock)
        dock.setFloating(True)
        dock.resize(300, 700)
        dock.show()
        if connect:
            _connect(dock, SERVER, KEY)
        return dock, server

    yield make
    for dock in docks:
        # One test cleans its dock up itself, and cleanup disconnects a signal.
        with contextlib.suppress(TypeError):
            dock.cleanup()
    QgsProject.instance().clear()
    QSettings().remove("geolens")


def _fetched(dock, dataset):
    """The dataset's preview once any background fetch has finished."""
    dock._thumbnail(dataset)
    for _ in range(100):
        if not dock._pending:
            break
        QTest.qWait(50)
    return next(
        (data for key, data in dock._thumbnails.items() if key[1] == dataset.id), None
    )


def _titles(dock):
    return [
        dock.results.topLevelItem(i).text(0)
        for i in range(dock.results.topLevelItemCount())
    ]


def _row(dock, title):
    for i in range(dock.results.topLevelItemCount()):
        if dock.results.topLevelItem(i).text(0) == title:
            return dock.results.topLevelItem(i)
    raise AssertionError(title)


def test_before_connecting_the_list_says_how_to_start(make_dock):
    dock, _ = make_dock(connect=False)
    assert dock.status.text() == "Not connected."
    assert dock.results.placeholder == (
        "Connect to a GeoLens server to browse its datasets."
    )
    assert not dock.type_filter.isEnabled()


def test_rows_show_a_type_icon_and_a_size_that_fits_a_narrow_dock(make_dock):
    dock, _ = make_dock()
    dock.resize(250, 700)
    assert _row(dock, "Roads").text(1) == "1,200 features"
    assert _row(dock, "parcels").text(1) == "1 feature"
    assert _row(dock, "Elevation").text(1) == "3 bands"
    assert not _row(dock, "Roads").icon(0).isNull()
    assert (
        _row(dock, "Roads").icon(0).cacheKey()
        != _row(dock, "Elevation").icon(0).cacheKey()
    )
    # The title column takes the leftover width instead of the type and size.
    assert dock.results.columnWidth(0) > dock.results.columnWidth(1)
    assert "Line layer" in _row(dock, "Roads").toolTip(0)
    assert "Double-click or press Enter" in _row(dock, "Roads").toolTip(0)


def test_a_raster_with_no_band_count_shows_no_size(make_dock):
    server = FakeServer([_feature("r", "Bare", record_type="raster_dataset")])
    dock, _ = make_dock(server)
    assert _row(dock, "Bare").text(1) == ""


def test_the_type_filter_narrows_the_loaded_results(make_dock):
    dock, server = make_dock()
    seen = len(server.seen)
    dock.type_filter.setCurrentIndex(dock.type_filter.findData("raster"))
    assert _titles(dock) == ["Elevation"]
    dock.type_filter.setCurrentIndex(dock.type_filter.findData("vector"))
    assert sorted(_titles(dock)) == ["Roads", "parcels"]
    assert len(server.seen) == seen
    assert "2 of 3 datasets shown" in dock.status.text()


def test_a_filter_that_hides_everything_says_so(make_dock):
    server = FakeServer([DATASETS[0]])
    dock, _ = make_dock(server)
    dock.type_filter.setCurrentIndex(dock.type_filter.findData("raster"))
    assert dock.results.topLevelItemCount() == 0
    assert dock.results.placeholder == "No datasets match this filter."


def test_sorting_by_title_ignores_case_and_newest_uses_the_date(make_dock):
    dock, _ = make_dock()
    assert _titles(dock) == ["Roads", "parcels", "Elevation"]
    dock.sort_combo.setCurrentIndex(dock.sort_combo.findData("title"))
    assert _titles(dock) == ["Elevation", "parcels", "Roads"]
    dock.sort_combo.setCurrentIndex(dock.sort_combo.findData("newest"))
    assert _titles(dock) == ["parcels", "Roads", "Elevation"]


def test_newest_first_is_unavailable_when_no_dataset_has_a_date(make_dock):
    server = FakeServer([_feature("a", "A"), _feature("b", "B")])
    dock, _ = make_dock(server)
    model = dock.sort_combo.model()
    assert not model.item(dock.sort_combo.findData("newest")).isEnabled()
    assert model.item(dock.sort_combo.findData("title")).isEnabled()


def test_the_selection_survives_a_filter_change(make_dock):
    dock, _ = make_dock()
    dock.results.setCurrentItem(_row(dock, "Roads"))
    dock.sort_combo.setCurrentIndex(dock.sort_combo.findData("title"))
    assert dock._selected_dataset().id == "roads"


def test_a_search_with_no_results_names_the_query(make_dock):
    dock, _ = make_dock()
    dock.search_edit.setText("nothing")
    dock.search()
    settle(dock)
    assert dock.results.topLevelItemCount() == 0
    assert dock.results.placeholder == (
        'No datasets found for "nothing". Try different words.'
    )
    assert dock.status.text().startswith("No datasets on")


def test_counts_are_plural_safe(make_dock):
    one, _ = make_dock(FakeServer([_feature("a", "A")]))
    assert one.status.text().startswith("1 dataset on https://gis.example.org.")
    many, _ = make_dock()
    assert many.status.text().startswith("3 datasets on")


def test_a_search_runs_in_the_background_and_the_status_line_says_so(make_dock):
    dock, server = make_dock()
    server.gates["/api/search/datasets/"] = gate = threading.Event()
    dock.search_edit.setText("roads")
    try:
        dock.search()
        # The server hasn't answered, yet the call has returned.
        assert dock.status.text() == "Searching…"
        assert not dock.search_button.isEnabled()
        assert QApplication.overrideCursor() is None
    finally:
        gate.set()
    settle(dock)
    assert dock.search_button.isEnabled()
    assert dock.status.text().startswith("3 datasets on")


def test_the_message_bar_reports_results_and_the_status_line_reports_state(
    make_dock, qgis_iface
):
    dock, _ = make_dock(connect=False)
    bar = qgis_iface.messageBar()
    bar.clear_messages()
    _connect(dock, SERVER)
    (shown,) = bar.get_messages(Qgis.MessageLevel.Success)
    assert shown.endswith("Connected to https://gis.example.org.")
    assert "Connected" not in dock.status.text()
    assert dock.status.text() == (
        "3 datasets on https://gis.example.org. Editing is enabled."
    )


@pytest.mark.parametrize(
    "raw, plain",
    [
        ("GeoLens request failed (HTTP 401): Not authenticated", "refused"),
        ("GeoLens request failed (HTTP 404)", "couldn't find"),
        ("GeoLens request failed (HTTP 429)", "busy"),
        ("GeoLens request failed (HTTP 503): upstream", "ran into a problem"),
        ("Could not reach GeoLens: [Errno 111] refused", "Couldn't reach"),
        ("GeoLens items response contained no features", "couldn't read"),
    ],
)
def test_raw_failures_become_plain_language_and_the_detail_goes_to_the_log(
    make_dock, qgis_iface, monkeypatch, raw, plain
):
    dock, _ = make_dock(connect=False)
    bar = qgis_iface.messageBar()
    bar.clear_messages()
    logged = []
    monkeypatch.setattr(
        geolens_dock.QgsMessageLog,
        "logMessage",
        lambda message, tag, level: logged.append((message, tag)),
    )
    dock._message(raw, Qgis.MessageLevel.Critical)
    (shown,) = bar.get_messages(Qgis.MessageLevel.Critical)
    assert plain in shown
    assert "HTTP" not in shown and "Errno" not in shown
    assert (raw, "GeoLens") in logged


def test_a_message_that_is_already_plain_is_shown_unchanged():
    text = "The selected authentication configuration no longer exists."
    assert plain_error(text) == text


def test_a_failed_add_reports_plainly_through_the_message_bar(make_dock, qgis_iface):
    dock, server = make_dock()
    dock.results.setCurrentItem(_row(dock, "Roads"))
    qgis_iface.messageBar().clear_messages()
    dock.add_tiles()
    settle(dock)
    (shown,) = qgis_iface.messageBar().get_messages(Qgis.MessageLevel.Critical)
    assert "couldn't find" in shown and "HTTP" not in shown


def test_double_click_adds_the_dataset_as_tiles(make_dock):
    dock, server = make_dock()
    item = _row(dock, "Elevation")
    rect = dock.results.visualItemRect(item)
    QTest.mouseClick(
        dock.results.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )
    QTest.mouseDClick(
        dock.results.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )
    settle(dock)
    assert [layer.name() for layer in QgsProject.instance().mapLayers().values()] == [
        "Elevation"
    ]
    assert ("gis.example.org", "/api/tiles/token/dem/", KEY) in server.seen


def test_enter_adds_the_current_dataset(make_dock):
    dock, _ = make_dock()
    dock.results.setCurrentItem(_row(dock, "Elevation"))
    dock.results.setFocus()
    QTest.keyClick(dock.results, Qt.Key.Key_Return)
    settle(dock)
    assert len(QgsProject.instance().mapLayers()) == 1


def test_enter_with_nothing_connected_adds_nothing(make_dock):
    dock, _ = make_dock(connect=False)
    QTest.keyClick(dock.results, Qt.Key.Key_Return)
    assert not QgsProject.instance().mapLayers()


def test_the_extent_option_is_a_checkbox_that_is_on_by_default_and_remembered(
    make_dock, qgis_iface
):
    dock, _ = make_dock(connect=False)
    assert isinstance(dock.view_only, QCheckBox) and dock.view_only.isChecked()
    assert dock.view_only.text() == "Only features in the current map extent"
    dock.view_only.setChecked(False)
    again = GeoLensDock(qgis_iface)
    try:
        assert not again.view_only.isChecked()
    finally:
        again.cleanup()


def _hover(dock, title, monkeypatch=None):
    viewport = dock.results.viewport()
    pos = dock.results.visualItemRect(_row(dock, title)).center()
    event = QHelpEvent(QEvent.Type.ToolTip, pos, viewport.mapToGlobal(pos))
    QApplication.sendEvent(viewport, event)


def test_hovering_a_row_fetches_its_thumbnail_once_from_the_connected_server(
    make_dock,
):
    dock, server = make_dock()
    assert server.thumbnail_requests() == []
    _hover(dock, "Roads")
    _hover(dock, "Roads")
    assert _fetched(dock, dock.datasets["roads"]) is not None
    _hover(dock, "Roads")
    assert server.thumbnail_requests() == [
        ("gis.example.org", "/api/datasets/roads/quicklook", KEY)
    ]


def test_a_slow_preview_never_blocks_the_hover_or_runs_on_the_gui_thread(make_dock):
    dock, server = make_dock()
    server.gate = threading.Event()
    try:
        _hover(dock, "Roads")
        # The fetch is still held up, yet the hover has already returned.
        assert dock._pending and not dock._thumbnails
    finally:
        server.gate.set()
    assert _fetched(dock, dock.datasets["roads"]) is not None
    assert server.quicklook_threads
    assert threading.get_ident() not in server.quicklook_threads


def test_the_preview_appears_in_the_tooltip_when_it_arrives(make_dock, monkeypatch):
    dock, server = make_dock()
    shown = []
    monkeypatch.setattr(
        geolens_dock.QToolTip,
        "showText",
        lambda pos, text, widget=None: shown.append(text),
    )
    monkeypatch.setattr(dock, "_hovered_dataset_id", lambda: "roads")
    _hover(dock, "Roads")
    assert "<img" not in shown[0]
    _fetched(dock, dock.datasets["roads"])
    assert "<img" in shown[-1]


def test_the_preview_is_not_shown_once_the_pointer_has_moved_on(make_dock, monkeypatch):
    dock, _ = make_dock()
    shown = []
    monkeypatch.setattr(
        geolens_dock.QToolTip,
        "showText",
        lambda pos, text, widget=None: shown.append(text),
    )
    monkeypatch.setattr(dock, "_hovered_dataset_id", lambda: "parcels")
    _hover(dock, "Roads")
    _fetched(dock, dock.datasets["roads"])
    assert len(shown) == 1 and "<img" not in shown[0]
    assert dock._thumbnail(dock.datasets["roads"]) is not None


def test_a_dataset_without_a_preview_costs_no_request(make_dock):
    dock, server = make_dock()
    assert _fetched(dock, dock.datasets["parcels"]) is None
    assert server.thumbnail_requests() == []


def test_a_failed_preview_is_not_cached_so_a_later_hover_retries(make_dock):
    dock, server = make_dock()
    # The fake has no quicklook for the raster, so the server answers 404.
    assert _fetched(dock, dock.datasets["dem"]) is None
    assert not dock._pending and not dock._thumbnails
    assert _fetched(dock, dock.datasets["dem"]) is None
    assert len(server.thumbnail_requests()) == 2


def test_the_thumbnail_is_not_shared_between_servers(make_dock):
    dock, server = make_dock()
    _fetched(dock, dock.datasets["roads"])
    dock.client.base_url = "https://other.example.org"
    _fetched(dock, dock.datasets["roads"])
    assert len(server.thumbnail_requests()) == 2


def test_reconnecting_refetches_previews_under_the_new_credentials(make_dock):
    dock, server = make_dock()
    assert _fetched(dock, dock.datasets["roads"]) is not None
    other = "glk_other_key_cccccccccccccccc"  # pragma: allowlist secret
    _connect(dock, SERVER, other)
    assert not dock._thumbnails
    assert _fetched(dock, dock.datasets["roads"]) is not None
    assert [seen[2] for seen in server.thumbnail_requests()] == [
        KEY,
        other,
    ]


def test_a_preview_from_the_old_client_cannot_fill_the_new_cache(make_dock):
    dock, server = make_dock()
    server.gate = threading.Event()
    _hover(dock, "Roads")
    old = dict(dock._pending)
    _connect(dock, SERVER)
    server.gate.set()
    QTest.qWait(300)
    assert old and not dock._pending and not dock._thumbnails


def test_a_preview_finishing_after_cleanup_touches_nothing(make_dock):
    dock, server = make_dock()
    server.gate = threading.Event()
    _hover(dock, "Roads")
    dock.cleanup()
    server.gate.set()
    QTest.qWait(300)
    assert not dock._thumbnails and not dock._pending


def test_metadata_opens_in_the_desktop_browser(make_dock, monkeypatch):
    opened = []
    monkeypatch.setattr(
        geolens_dock.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()),
    )
    dock, _ = make_dock()
    dock.results.setCurrentItem(_row(dock, "Roads"))
    dock.open_metadata()
    assert opened == [f"{SERVER}/datasets/roads"]


def test_footer_links_open_help_and_the_ways_to_get_a_server(make_dock, monkeypatch):
    opened = []
    monkeypatch.setattr(
        geolens_dock.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()),
    )
    dock, _ = make_dock(connect=False)
    dock.help_button.click()
    install, pilot = dock.no_server_button.menu().actions()
    install.trigger()
    pilot.trigger()
    assert opened == [
        geolens_dock.HELP_URL,
        geolens_dock.INSTALL_URL,
        geolens_dock.PILOT_URL,
    ]


def test_every_control_has_a_tooltip_and_an_accessible_name(make_dock):
    dock, _ = make_dock(connect=False)
    controls = [
        dock.search_edit,
        dock.search_button,
        dock.type_filter,
        dock.sort_combo,
        dock.results,
        dock.limit,
        dock.view_only,
        dock.tiles_button,
        dock.features_button,
        dock.metadata_button,
        dock.sync_button,
        dock.help_button,
        dock.no_server_button,
    ]
    for control in controls:
        assert control.accessibleName(), control
        if control is not dock.results:
            assert control.toolTip(), control
    assert "this QGIS session" in dock.sync_button.toolTip()
    assert "Tiles are not limited" in dock.limit.toolTip()


def test_the_intro_no_longer_says_self_hosted(make_dock):
    dock, _ = make_dock(connect=False)
    labels = [label.text() for label in dock.findChildren(type(dock.status))]
    assert "Browse and add datasets from a GeoLens server." in labels


def test_a_failed_search_leaves_the_list_and_reports_plainly(make_dock, qgis_iface):
    dock, server = make_dock()

    def refuse(request, timeout=0):
        raise HTTPError(request.full_url, 503, "Unavailable", {}, BytesIO(b"{}"))

    dock.client._opener = refuse
    qgis_iface.messageBar().clear_messages()
    dock.search_edit.setText("x")
    dock.search()
    settle(dock)
    assert dock.results.topLevelItemCount() == 3
    (shown,) = qgis_iface.messageBar().get_messages(Qgis.MessageLevel.Critical)
    assert "ran into a problem" in shown
    assert isinstance(GeoLensError("x"), RuntimeError)


def test_the_thumbnail_request_gives_up_after_five_seconds(make_dock):
    dock, server = make_dock()
    _fetched(dock, dock.datasets["roads"])
    assert server.timeouts["/api/datasets/roads/quicklook"] == 5
    assert server.timeouts["/api/search/datasets/"] == 30


def test_the_qgis_transport_gives_up_on_a_server_that_never_answers():
    import socket
    import time
    from urllib.request import Request

    from geolens_qgis.connection import QgisTransport

    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        url = f"http://127.0.0.1:{silent.getsockname()[1]}/quicklook"
        started = time.monotonic()
        with pytest.raises(OSError):
            QgisTransport()(Request(url), timeout=0.5)
        assert time.monotonic() - started < 10


def _roads(**props):
    base = dict(DATASETS[0]["properties"])
    return [_feature("roads", base.pop("title"), **{**base, **props})]


def _record_tooltips(monkeypatch, dock):
    shown = []
    monkeypatch.setattr(
        geolens_dock.QToolTip,
        "showText",
        lambda pos, text, widget=None: shown.append(text),
    )
    monkeypatch.setattr(dock, "_hovered_dataset_id", lambda: "roads")
    return shown


def _wait_idle(dock):
    for _ in range(100):
        if not dock._pending:
            return
        QTest.qWait(50)


@pytest.mark.parametrize("v1_first", [True, False], ids=["v1-first", "v2-first"])
def test_a_preview_for_a_replaced_version_never_shows(make_dock, monkeypatch, v1_first):
    server = FakeServer(_roads())
    server.version_gates = {"abc123": threading.Event(), "v2": threading.Event()}
    dock, _ = make_dock(server)
    shown = _record_tooltips(monkeypatch, dock)
    _hover(dock, "Roads")
    server.datasets = _roads(quicklook_version="v2", description="Resurfaced")
    dock.search()
    settle(dock)
    _hover(dock, "Roads")
    shown.clear()
    first, second = ("abc123", "v2") if v1_first else ("v2", "abc123")
    server.version_gates[first].set()
    QTest.qWait(300)
    server.version_gates[second].set()
    _wait_idle(dock)
    images = [text for text in shown if "<img" in text]
    assert len(images) == 1 and "Resurfaced" in images[0]
    # The old image still serves its own version.
    assert {key[2] for key in dock._thumbnails} == {"abc123", "v2"}


def test_a_description_only_change_shows_the_new_text(make_dock, monkeypatch):
    server = FakeServer(_roads())
    server.version_gates = {"abc123": threading.Event()}
    dock, _ = make_dock(server)
    shown = _record_tooltips(monkeypatch, dock)
    _hover(dock, "Roads")
    server.datasets = _roads(description="Brand new words")
    dock.search()
    settle(dock)
    shown.clear()
    server.version_gates["abc123"].set()
    _wait_idle(dock)
    assert "Brand new words" in shown[-1] and "<img" in shown[-1]


def test_a_removed_preview_is_not_shown_when_the_fetch_finishes(make_dock, monkeypatch):
    server = FakeServer(_roads())
    server.version_gates = {"abc123": threading.Event()}
    dock, _ = make_dock(server)
    shown = _record_tooltips(monkeypatch, dock)
    _hover(dock, "Roads")
    server.datasets = _roads(has_quicklook=False, quicklook_version=None)
    dock.search()
    settle(dock)
    shown.clear()
    server.version_gates["abc123"].set()
    _wait_idle(dock)
    assert shown == []


@pytest.mark.parametrize(
    "first, second, expected",
    [
        ("2026-10-01T12:00:00Z", "2026-10-01T12:00:00.500Z", "second"),
        ("2026-10-01T12:00:00+02:00", "2026-10-01T11:00:00Z", "second"),
        ("2026-10-01T12:00:00Z", "2026-10-01T12:00:00+00:00", "tie"),
        ("2026-10-01T12:00:00.5Z", "2026-10-01T12:00:00.250000Z", "first"),
        ("2026-10-01", "not a date", "first"),
    ],
)
def test_instants_compare_by_time_not_by_text(first, second, expected):
    one, two = geolens_dock._instant(first), geolens_dock._instant(second)
    if expected == "second":
        assert two > one
    elif expected == "first":
        assert one is not None and (two is None or one > two)
    else:
        assert one == two


def test_newest_first_orders_by_instant_and_puts_bad_dates_last(make_dock):
    dates = {
        "a": "2026-10-01T12:00:00Z",
        "b": "2026-10-01T12:00:00.500Z",
        "c": "2026-10-01T12:00:00+02:00",
        "d": "garbage",
        "e": "2026-10-01T11:00:00+00:00",
    }
    server = FakeServer([_feature(i, i, updated=d) for i, d in dates.items()])
    dock, _ = make_dock(server)
    dock.sort_combo.setCurrentIndex(dock.sort_combo.findData("newest"))
    assert _titles(dock) == ["b", "a", "e", "c", "d"]


def test_newest_first_needs_at_least_one_valid_date(make_dock):
    server = FakeServer([_feature("a", "A", updated="garbage")])
    dock, _ = make_dock(server)
    item = dock.sort_combo.model().item(dock.sort_combo.findData("newest"))
    assert not item.isEnabled()


def test_connecting_with_search_text_lists_only_its_matches(make_dock):
    class Filtering(FakeServer):
        def __call__(self, request, timeout=0):
            query = parse_qs(urlparse(request.full_url).query).get("q", [""])[0]
            if query:
                self.datasets = [
                    d
                    for d in DATASETS
                    if query.lower() in d["properties"]["title"].lower()
                ]
            return super().__call__(request, timeout)

    server = Filtering()
    dock, _ = make_dock(connect=False)
    dock._opener = server
    server.dock = dock
    dock.search_edit.setText("roads")
    _connect(dock, SERVER)
    assert _titles(dock) == ["Roads"]
    assert dock._query == "roads"
    searches = [s for s in server.seen if s[1] == "/api/search/datasets/"]
    assert len(searches) == 2


def test_connecting_with_a_blank_search_box_asks_once(make_dock):
    dock, server = make_dock()
    searches = [s for s in server.seen if s[1] == "/api/search/datasets/"]
    assert len(searches) == 1 and dock._query == ""


def test_counts_follow_the_default_locale(make_dock):
    QLocale.setDefault(QLocale("de_DE"))
    dock, _ = make_dock()
    assert _row(dock, "Roads").text(1) == "1.200 features"
    assert geolens_dock.count_text(1200, "{count} a", "{count} b") == "1.200 b"


def _messages(qgis_iface, level=Qgis.MessageLevel.Critical):
    return qgis_iface.messageBar().get_messages(level)


def _reached(server, *paths):
    """Wait until the server has seen a request for each path."""
    for _ in range(500):
        if set(paths) <= {seen[1] for seen in server.seen}:
            return
        QTest.qWait(10)
    raise AssertionError(f"no request for {paths}")


def _quiet(qgis_iface):
    return not any(
        _messages(qgis_iface, level)
        for level in (Qgis.MessageLevel.Info, Qgis.MessageLevel.Critical)
    )


def test_a_newer_search_replaces_one_the_server_has_not_answered(make_dock, qgis_iface):
    dock, server = make_dock()
    server.gates[("/api/search/datasets/", "slow")] = gate = threading.Event()
    qgis_iface.messageBar().clear_messages()
    try:
        dock.search_edit.setText("slow")
        server.seen.clear()
        dock.search()
        _reached(server, "/api/search/datasets/")
        dock.search_edit.setText("nothing")
        dock.search()
        settle(dock)
        assert dock._query == "nothing"
    finally:
        gate.set()
    QTest.qWait(300)
    assert dock._query == "nothing" and dock.results.topLevelItemCount() == 0
    assert _quiet(qgis_iface)


@pytest.mark.parametrize("path", ["/api/search/datasets/", "/api/tiles/token/dem/"])
def test_a_result_for_a_connection_left_behind_is_dropped(make_dock, qgis_iface, path):
    dock, server = make_dock()
    server.gates[path] = gate = threading.Event()
    dock.results.setCurrentItem(_row(dock, "Elevation"))
    qgis_iface.messageBar().clear_messages()
    try:
        dock.search() if "search" in path else dock.add_tiles()
        _reached(server, path)
        dock.use_server("https://other.example.org")
        assert dock.client is None and not dock._pending
    finally:
        gate.set()
    QTest.qWait(300)
    assert dock.results.topLevelItemCount() == 0
    assert not QgsProject.instance().mapLayers()
    assert _quiet(qgis_iface)


def test_a_connect_answered_after_choosing_another_connection_is_dropped(
    make_dock, qgis_iface
):
    dock, server = make_dock(connect=False)
    server.gates["/api/capabilities"] = gate = threading.Event()
    dock.use_server(SERVER)
    qgis_iface.messageBar().clear_messages()
    try:
        dock.connect_server()
        assert not dock.connect_button.isEnabled()
        _reached(server, "/api/capabilities")
        dock.use_server("https://other.example.org")
        assert dock.connect_button.isEnabled()
    finally:
        gate.set()
    QTest.qWait(300)
    assert dock.client is None and dock.results.topLevelItemCount() == 0
    assert _messages(qgis_iface, Qgis.MessageLevel.Success) == []
    assert _quiet(qgis_iface)


def test_results_arriving_after_cleanup_touch_nothing(make_dock, qgis_iface):
    dock, server = make_dock()
    server.gates["/api/search/datasets/"] = searched = threading.Event()
    server.gates["/api/tiles/token/dem/"] = added = threading.Event()
    dock.results.setCurrentItem(_row(dock, "Elevation"))
    dock.search_edit.setText("nothing")
    qgis_iface.messageBar().clear_messages()
    try:
        dock.search()
        dock.add_tiles()
        _reached(server, "/api/search/datasets/", "/api/tiles/token/dem/")
        dock.cleanup()
    finally:
        searched.set()
        added.set()
    QTest.qWait(300)
    assert not dock._pending
    assert dock.results.topLevelItemCount() == 3
    assert not QgsProject.instance().mapLayers()
    assert _quiet(qgis_iface)


@pytest.mark.parametrize("title, added", [("Elevation", 1), ("Roads", 0)])
def test_the_add_buttons_wait_for_the_server_then_come_back(
    make_dock, qgis_iface, title, added
):
    # The fake serves a tile token for the raster only, so Roads fails.
    dock, server = make_dock()
    ident = "dem" if added else "roads"
    server.gates[f"/api/tiles/token/{ident}/"] = gate = threading.Event()
    dock.results.setCurrentItem(_row(dock, title))
    qgis_iface.messageBar().clear_messages()
    try:
        dock.tiles_button.click()
        assert dock.status.text() == f"Adding {title}…"
        assert not dock.tiles_button.isEnabled()
        assert not dock.features_button.isEnabled()
        # A double-click waits too, so the dataset isn't added twice.
        dock._add_item(_row(dock, title))
    finally:
        gate.set()
    settle(dock)
    assert len(QgsProject.instance().mapLayers()) == added
    assert dock.tiles_button.isEnabled()
    assert dock.features_button.isEnabled() == (title == "Roads")
    assert dock.status.text().startswith("3 datasets on")
    assert len(_messages(qgis_iface)) == 1 - added


def test_the_connect_button_comes_back_after_a_failed_connect(make_dock, qgis_iface):
    dock, server = make_dock(connect=False)
    server.gates["/api/search/datasets/"] = gate = threading.Event()
    server.datasets = None
    dock.use_server(SERVER)
    qgis_iface.messageBar().clear_messages()
    try:
        dock.connect_button.click()
        assert dock.status.text() == "Connecting…"
        assert not dock.connect_button.isEnabled()
    finally:
        gate.set()
    settle(dock)
    assert dock.connect_button.isEnabled() and dock.client is None
    assert dock.status.text() == "Not connected."
    assert len(_messages(qgis_iface)) == 1


def test_a_failed_tile_renewal_tries_again_in_a_minute(make_dock):
    dock, server = make_dock()
    dock.results.setCurrentItem(_row(dock, "Elevation"))
    dock.add_tiles()
    settle(dock)
    (layer,) = QgsProject.instance().mapLayers().values()
    source = layer.source()
    timer = dock.refresh_timers[layer.id()]
    timer.stop()

    def refuse(request, timeout=0):
        raise HTTPError(request.full_url, 503, "Unavailable", {}, BytesIO(b"{}"))

    # The layer renews through the client that added it.
    dock.client._opener = refuse
    timer.timeout.emit()
    settle(dock)
    assert dock.refresh_timers[layer.id()] is timer
    assert timer.isActive() and timer.interval() == 60_000
    assert layer.source() == source
