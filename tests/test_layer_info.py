"""Layers added from GeoLens carry the dataset's catalogue metadata."""

import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlparse

import pytest

pytest.importorskip("pytest_qgis")

from qgis.core import QgsProject  # noqa: E402
from qgis.PyQt.QtCore import QSettings  # noqa: E402

from geolens_qgis.client import GeoLensClient, parse_dataset  # noqa: E402
from geolens_qgis.layer_info import apply_layer_info, build_metadata  # noqa: E402

SEARCH = {
    "id": "roads",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[1, 2], [1, 4], [3, 4], [3, 2], [1, 2]]],
    },
    "properties": {
        "title": "Roads",
        "description": "Centrelines",
        "keywords": ["transport", " ", "roads"],
        "license": "CC-BY-4.0",
        "language": "en",
        "geometry_type": "LINESTRING",
    },
}
DETAIL = {
    "srid": 27700,
    "extent_bbox": [-1.0, 50.0, 0.5, 51.5],
    "license": "OGL-3.0",
    "attribution": "Ordnance Survey",
    "summary": "unused when a description exists",
}


@pytest.fixture
def clean_profile_settings(qgis_app):
    QSettings().remove("geolens")
    yield
    QSettings().remove("geolens")


def _opener(detail):
    def opener(request, timeout=0):
        path = urlparse(request.full_url).path
        bodies = {
            "/api/datasets/roads": detail,
            "/api/tiles/token/roads/": {
                "kind": "vector",
                "sig": "s",
                "exp": 9,
                "scope": "roads:p1",
                "expires_in": 600,
            },
            "/api/collections/roads/items": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": 1,
                        "geometry": {"type": "Point", "coordinates": [1, 2]},
                        "properties": {},
                    }
                ],
            },
        }
        if detail is None or path not in bodies:
            raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(b"{}"))
        return BytesIO(json.dumps(bodies[path]).encode())

    return opener


def test_every_mapped_field_reaches_the_layer_metadata():
    dataset = parse_dataset(SEARCH)
    metadata = build_metadata(dataset, DETAIL, "https://gis.example.org/datasets/roads")
    assert metadata.identifier() == "roads"
    assert metadata.title() == "Roads"
    assert metadata.abstract() == "Centrelines"
    assert metadata.keywords() == {"GeoLens": ["transport", "roads"]}
    assert metadata.licenses() == ["OGL-3.0"]
    assert metadata.rights() == ["Ordnance Survey"]
    assert metadata.language() == "en"
    assert metadata.crs().authid() == "EPSG:27700"
    assert [link.url for link in metadata.links()] == [
        "https://gis.example.org/datasets/roads"
    ]
    box = metadata.extent().spatialExtents()[0]
    assert box.extentCrs.authid() == "EPSG:4326"
    assert box.bounds.xMinimum() == -1.0 and box.bounds.yMaximum() == 51.5


def test_a_sparse_record_falls_back_to_the_search_result():
    dataset = parse_dataset({"id": "bare", "geometry": SEARCH["geometry"]})
    metadata = build_metadata(dataset, {}, "https://gis.example.org/datasets/bare")
    assert metadata.title() == "bare"
    assert metadata.abstract() == ""
    assert metadata.keywords() == {}
    assert metadata.licenses() == [] and metadata.rights() == []
    assert not metadata.crs().isValid()
    box = metadata.extent().spatialExtents()[0]
    assert (box.bounds.xMinimum(), box.bounds.yMaximum()) == (1.0, 4.0)
    licensed = parse_dataset(SEARCH)
    assert build_metadata(licensed, {}, "u").licenses() == ["CC-BY-4.0"]


def test_a_failed_detail_request_still_describes_the_layer(qgis_iface):
    from qgis.core import QgsVectorLayer

    layer = QgsVectorLayer("Point?crs=EPSG:4326", "x", "memory")
    client = GeoLensClient("https://gis.example.org", opener=_opener(None))
    apply_layer_info(layer, parse_dataset(SEARCH), client)
    assert layer.metadata().title() == "Roads"
    assert layer.metadata().licenses() == ["CC-BY-4.0"]


@pytest.mark.parametrize("add", ["add_tiles", "add_features"])
def test_layers_added_from_the_dock_are_described(
    qgis_iface, clean_profile_settings, add
):
    from geolens_qgis.dialogs import GeoLensDock

    dock = GeoLensDock(qgis_iface, opener=_opener(DETAIL))
    try:
        dock.client = GeoLensClient("https://gis.example.org", opener=_opener(DETAIL))
        dock.view_only.setChecked(False)
        dataset = parse_dataset(SEARCH)
        dock._selected_dataset = lambda: dataset
        getattr(dock, add)()
        (layer,) = QgsProject.instance().mapLayers().values()
        assert layer.metadata().licenses() == ["OGL-3.0"]
        assert layer.metadata().keywords() == {"GeoLens": ["transport", "roads"]}
    finally:
        dock.cleanup()
        QgsProject.instance().clear()


def test_an_extent_across_the_antimeridian_is_split():
    dataset = parse_dataset(SEARCH)
    detail = {"extent_bbox": [178.0, -19.0, -178.0, -17.0]}
    metadata = build_metadata(dataset, detail, "u")
    boxes = [
        (b.bounds.xMinimum(), b.bounds.xMaximum())
        for b in metadata.extent().spatialExtents()
    ]
    assert boxes == [(178.0, 180.0), (-180.0, -178.0)]


def test_attribution_survives_a_failed_detail_request():
    feature = {**SEARCH, "properties": {**SEARCH["properties"]}}
    feature["properties"]["source_organization"] = "Ordnance Survey"
    metadata = build_metadata(parse_dataset(feature), {}, "u")
    assert metadata.rights() == ["Ordnance Survey"]
