"""Layer metadata mapping against a real QGIS."""

import pytest

pytest.importorskip("pytest_qgis")

from qgis.core import (  # noqa: E402
    QgsField,
    QgsLayerMetadata,
    QgsProjectMetadata,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant  # noqa: E402

from geolens_qgis.publish.metadata import dataset_metadata  # noqa: E402


@pytest.fixture(autouse=True)
def _qgis(real_qgis, qgis_app):
    return qgis_app


def string_field(name, comment=""):
    # QVariant types are what QGIS 3 expects; QGIS 4 takes them as well.
    return QgsField(name, QVariant.String, "string", 0, 0, comment)


def layer_with(fields, name="parks"):
    layer = QgsVectorLayer("Point?crs=epsg:4326", name, "memory")
    layer.dataProvider().addAttributes(fields)
    layer.updateFields()
    return layer


def full_metadata():
    metadata = QgsLayerMetadata()
    metadata.setTitle("City parks")
    metadata.setAbstract("City parks maintained by Parks & Recreation.")
    metadata.setKeywords({"gmd:topic": ["parks", "recreation"], "free": ["parks"]})
    metadata.setLicenses(["CC-BY-4.0", "ODbL"])
    metadata.setRights(["City of Springfield", "Parks Dept"])
    return metadata


def test_full_metadata_produces_the_exact_dataset_entry():
    layer = layer_with(
        [string_field("PARK_NAME", "Official name"), string_field("ACRES")]
    )
    layer.setFieldAlias(layer.fields().indexFromName("PARK_NAME"), "Park name")
    layer.setMetadata(full_metadata())
    assert dataset_metadata(layer, QgsProjectMetadata()) == {
        "title": "City parks",
        "description": "City parks maintained by Parks & Recreation.",
        "metadata": {
            "tags": ["parks", "recreation"],
            "license": "CC-BY-4.0",
            "attribution": "City of Springfield; Parks Dept",
        },
        "attributes": [
            {
                "source_name": "PARK_NAME",
                "title": "Park name",
                "description": "Official name",
            }
        ],
    }


def test_title_falls_back_to_the_layer_name():
    layer = layer_with([])
    assert dataset_metadata(layer, QgsProjectMetadata()) == {"title": "parks"}


def test_a_layer_without_metadata_takes_the_project_abstract_and_keywords():
    project = QgsProjectMetadata()
    project.setTitle("Springfield")
    project.setAbstract("Everything about Springfield.")
    project.setKeywords({"free": ["city", "city", " springfield "]})
    assert dataset_metadata(layer_with([]), project) == {
        "title": "parks",
        "description": "Everything about Springfield.",
        "metadata": {"tags": ["city", "springfield"]},
    }


def test_layer_metadata_wins_over_the_project():
    project = QgsProjectMetadata()
    project.setAbstract("project text")
    layer = layer_with([])
    metadata = QgsLayerMetadata()
    metadata.setAbstract("layer text")
    layer.setMetadata(metadata)
    assert dataset_metadata(layer, project)["description"] == "layer text"


def test_long_tags_are_cut_to_the_server_limit():
    layer = layer_with([])
    metadata = QgsLayerMetadata()
    metadata.setKeywords({"free": ["x" * 150]})
    layer.setMetadata(metadata)
    (tag,) = dataset_metadata(layer, QgsProjectMetadata())["metadata"]["tags"]
    assert tag == "x" * 100


def test_attributes_are_limited_to_the_server_maximum():
    layer = layer_with([string_field(f"f{i}", "c") for i in range(600)])
    assert len(dataset_metadata(layer, QgsProjectMetadata())["attributes"]) == 500


def test_a_raster_has_no_attributes():
    raster = QgsRasterLayer()
    entry = dataset_metadata(raster, QgsProjectMetadata())
    assert "attributes" not in entry


def test_blank_entries_count_as_no_metadata():
    project = QgsProjectMetadata()
    project.setAbstract("project text")
    layer = layer_with([])
    metadata = QgsLayerMetadata()
    metadata.setLicenses([" "])
    metadata.setRights([""])
    metadata.setKeywords({"free": [""]})
    layer.setMetadata(metadata)
    assert dataset_metadata(layer, project) == {
        "title": "parks",
        "description": "project text",
    }


def test_the_first_nonblank_license_is_used():
    layer = layer_with([])
    metadata = QgsLayerMetadata()
    metadata.setLicenses(["", "ODbL"])
    layer.setMetadata(metadata)
    entry = dataset_metadata(layer, QgsProjectMetadata())
    assert entry["metadata"] == {"license": "ODbL"}
