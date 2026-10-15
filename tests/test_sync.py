"""Syncing layer edits: partial writes, and refusal after a table replacement."""

import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("pytest_qgis")

from qgis.core import (  # noqa: E402
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)
from qgis.PyQt.QtCore import QSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QMessageBox  # noqa: E402

TABLE_ID = "4242"
FEATURES = [
    {
        "type": "Feature",
        "id": gid,
        "geometry": {"type": "Point", "coordinates": [gid, gid]},
        # QGIS reads the timestamp back as 2024-01-02T10:11:12.000.
        "properties": {"name": name, "seen": "2024-01-02T10:11:12", "score": 1.5},
    }
    for gid, name in ((1, "A"), (2, "C"))
]


FAIL = object()


class _Server:
    """An opener serving one editable dataset and recording every write.

    Single-feature reads answer with `table_ids` in turn, repeating the last;
    None leaves the id out, as older servers do, and FAIL answers 500.
    """

    def __init__(
        self,
        table_ids=(TABLE_ID,),
        replaced=False,
        created_in=TABLE_ID,
    ):
        self.table_ids = list(table_ids)
        self.replaced = replaced
        self.created_in = created_in
        self.writes = []

    def __call__(self, request, timeout=0):
        url = urlparse(request.full_url)
        method = request.get_method()
        if method != "GET":
            body = json.loads(request.data) if request.data else None
            self.writes.append((method, url.path, parse_qs(url.query), body))
            if self.replaced and method in ("PATCH", "DELETE"):
                detail = {"code": "dataset_replaced", "message": "Read it again."}
                return _error(request, 409, {"detail": detail})
            reply = {}
            if method == "POST":
                reply = {"id": 99}
                if self.created_in is not None:
                    reply["table_id"] = self.created_in
            return BytesIO(json.dumps(reply).encode())
        if url.path.startswith("/api/datasets/roads/features/"):
            table_id = self.table_ids[0]
            if len(self.table_ids) > 1:
                self.table_ids.pop(0)
            if table_id is FAIL:
                return _error(request, 500, {"detail": "Database unavailable"})
            single = {**FEATURES[0]}
            if table_id is not None:
                single["table_id"] = table_id
            return BytesIO(json.dumps(single).encode())
        bodies = {
            "/api/search/datasets/": {
                "features": [
                    {
                        "id": "roads",
                        "properties": {"title": "Roads", "geometry_type": "POINT"},
                    }
                ]
            },
            "/api/capabilities": {"flags": {"enable_dataset_editing": True}},
            "/api/collections/roads/items": {
                "type": "FeatureCollection",
                "features": FEATURES,
            },
        }
        if url.path not in bodies:
            return _error(request, 404, {})
        return BytesIO(json.dumps(bodies[url.path]).encode())


def _error(request, status, body):
    payload = BytesIO(json.dumps(body).encode())
    raise HTTPError(request.full_url, status, "Error", {}, payload)


@pytest.fixture
def profile(qgis_app):
    QSettings().remove("geolens")
    yield
    QSettings().remove("geolens")


@pytest.fixture
def loaded(qgis_iface, profile):
    """Load roads into a dock, then hand back (dock, layer) for a given server."""
    from geolens_qgis.dialogs import GeoLensDock

    docks = []

    def load(server):
        dock = GeoLensDock(qgis_iface, opener=server)
        docks.append(dock)
        dock.use_server("https://gis.example.org")
        dock.connect_server()
        dock.view_only.setChecked(False)
        dock.results.setCurrentItem(dock.results.topLevelItem(0))
        dock.add_features()
        (layer,) = QgsProject.instance().mapLayersByName("Roads")
        qgis_iface.setActiveLayer(layer)
        layer.startEditing()
        return dock, layer

    yield load
    for dock in docks:
        dock.cleanup()
    QgsProject.instance().clear()


def _shown(dock):
    """Every message pushed to the QGIS message bar so far."""
    return " ".join(
        text for texts in dock.iface.messageBar().messages.values() for text in texts
    )


def _fid(layer, gid):
    return next(
        f.id() for f in layer.getFeatures() if f.attribute("_geolens_gid") == gid
    )


def _rename(layer, gid, name):
    layer.changeAttributeValue(_fid(layer, gid), layer.fields().indexOf("name"), name)


def _add(layer):
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(7, 7)))
    feature.setAttribute("name", "new")
    assert layer.addFeature(feature)


def test_an_attribute_edit_sends_only_that_field(loaded):
    dock, layer = loaded(server := _Server())
    _rename(layer, 1, "B")
    dock.sync_active_layer()
    assert server.writes == [
        (
            "PATCH",
            "/api/datasets/roads/features/1",
            {"table_id": [TABLE_ID]},
            {"properties": {"name": "B"}},
        )
    ]
    assert "Synchronized 1 change" in _shown(dock)


def test_a_geometry_edit_sends_only_the_geometry(loaded):
    dock, layer = loaded(server := _Server())
    layer.changeGeometry(_fid(layer, 2), QgsGeometry.fromPointXY(QgsPointXY(5, 6)))
    dock.sync_active_layer()
    assert server.writes == [
        (
            "PATCH",
            "/api/datasets/roads/features/2",
            {"table_id": [TABLE_ID]},
            {"geometry": {"type": "Point", "coordinates": [5.0, 6.0]}},
        )
    ]
    # Synced, so the next sync has nothing to send.
    dock.sync_active_layer()
    assert len(server.writes) == 1


def test_guarded_writes_carry_the_table_id_and_precede_creates(loaded, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes
    )
    dock, layer = loaded(server := _Server())
    _rename(layer, 1, "B")
    layer.deleteFeature(_fid(layer, 2))
    _add(layer)
    dock.sync_active_layer()
    assert [(method, path, query) for method, path, query, _ in server.writes] == [
        ("PATCH", "/api/datasets/roads/features/1", {"table_id": [TABLE_ID]}),
        ("DELETE", "/api/datasets/roads/features/2", {"table_id": [TABLE_ID]}),
        # GeoLens takes no table id on a create.
        ("POST", "/api/datasets/roads/features/", {}),
    ]


def test_a_replaced_dataset_stops_the_sync_and_keeps_the_edits(loaded):
    dock, layer = loaded(server := _Server(replaced=True))
    _rename(layer, 1, "B")
    _rename(layer, 2, "D")
    _add(layer)
    dock.sync_active_layer()
    assert [method for method, *_ in server.writes] == ["PATCH"]
    assert layer.id() not in dock.baselines
    assert not dock.sync_button.isEnabled()
    assert layer.isEditable()
    names = sorted(str(f.attribute("name")) for f in layer.getFeatures())
    assert names == ["B", "D", "new"]
    assert "replaced on the GeoLens server" in _shown(dock)


def test_a_server_without_table_ids_syncs_without_one(loaded):
    dock, layer = loaded(server := _Server(table_ids=[None], created_in=None))
    _rename(layer, 1, "B")
    dock.sync_active_layer()
    assert [(method, query) for method, _, query, _ in server.writes] == [("PATCH", {})]


def test_a_replaced_dataset_gets_no_new_features(loaded):
    dock, layer = loaded(server := _Server(table_ids=[TABLE_ID, TABLE_ID, "777"]))
    _add(layer)
    dock.sync_active_layer()
    assert server.writes == []
    assert layer.id() not in dock.baselines
    assert layer.isEditable()
    assert "replaced on the GeoLens server" in _shown(dock)


def test_creates_wait_until_the_table_can_be_confirmed(loaded):
    dock, layer = loaded(server := _Server(table_ids=[TABLE_ID, TABLE_ID, FAIL]))
    _add(layer)
    dock.sync_active_layer()
    assert server.writes == []
    assert layer.id() in dock.baselines
    assert "couldn't confirm" in _shown(dock)


def test_a_create_into_a_replacement_stops_the_sync(loaded):
    dock, layer = loaded(server := _Server(created_in="777"))
    _add(layer)
    _add(layer)
    dock.sync_active_layer()
    assert [method for method, *_ in server.writes] == ["POST"]
    assert layer.id() not in dock.baselines
    assert "One new feature was added" in _shown(dock)


@pytest.mark.parametrize(
    "table_ids, shown",
    [
        ([TABLE_ID, "777"], "replaced on the server while it was loading"),
        ([FAIL, TABLE_ID], "couldn't confirm which version"),
        ([TABLE_ID, FAIL], "couldn't confirm which version"),
    ],
    ids=["replaced-during-load", "read-before-fails", "read-after-fails"],
)
def test_a_load_that_cant_be_pinned_to_one_table_is_not_syncable(
    loaded, table_ids, shown
):
    dock, layer = loaded(_Server(table_ids=table_ids))
    assert layer.id() not in dock.baselines
    assert layer.customProperty("geolens/editable") is False
    assert not dock.sync_button.isEnabled()
    assert shown in _shown(dock)


@pytest.mark.parametrize("edit", [False, True], ids=["field-only", "with-an-edit"])
def test_a_removed_field_is_reported_not_cleared(loaded, edit):
    dock, layer = loaded(server := _Server())
    layer.deleteAttribute(layer.fields().indexOf("score"))
    if edit:
        _rename(layer, 1, "B")
    dock.sync_active_layer()
    expected = [{"properties": {"name": "B"}}] if edit else []
    assert [body for *_, body in server.writes] == expected
    assert "keeps their values: score." in _shown(dock)
