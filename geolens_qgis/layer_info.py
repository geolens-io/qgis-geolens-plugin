"""Describe a layer added from GeoLens with the dataset's catalogue record."""

from __future__ import annotations

from qgis.core import (
    QgsBox3D,
    QgsCoordinateReferenceSystem,
    QgsLayerMetadata,
    QgsRectangle,
)

from .client import GeoLensError
from .i18n import tr

KEYWORD_VOCABULARY = "GeoLens"


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bbox(detail, dataset):
    box = detail.get("extent_bbox")
    if isinstance(box, list) and len(box) == 4:
        with_numbers = [v for v in box if isinstance(v, (int, float))]
        if len(with_numbers) == 4:
            return tuple(float(v) for v in with_numbers)
    return dataset.bbox


def _rectangles(bbox):
    """The boxes covering `bbox`; one that crosses the antimeridian splits in two."""
    west, south, east, north = bbox
    if west > east:
        return [(west, south, 180.0, north), (-180.0, south, east, north)]
    return [bbox]


def _spatial(west, south, east, north):
    spatial = QgsLayerMetadata.SpatialExtent()
    spatial.extentCrs = QgsCoordinateReferenceSystem("EPSG:4326")
    spatial.bounds = QgsBox3D(QgsRectangle(west, south, east, north))
    return spatial


def build_metadata(dataset, detail, page_url) -> QgsLayerMetadata:
    """Layer metadata from a search result and, when known, the detail record.

    `licenses` and `rights` carry the dataset's licence and attribution, the
    same fields `publish/metadata.py` reads back from QGIS.
    """
    metadata = QgsLayerMetadata()
    metadata.setIdentifier(dataset.id)
    metadata.setTitle(dataset.title)
    metadata.setType("dataset")
    abstract = dataset.description or _text(detail.get("summary"))
    if abstract:
        metadata.setAbstract(abstract)
    if dataset.keywords:
        metadata.setKeywords({KEYWORD_VOCABULARY: list(dataset.keywords)})
    license_text = _text(detail.get("license")) or _text(dataset.license)
    if license_text:
        metadata.setLicenses([license_text])
    # Search results carry no attribution; the source organisation is the
    # nearest credit line until the detail record is available.
    attribution = _text(detail.get("attribution")) or _text(dataset.source_organization)
    if attribution:
        metadata.setRights([attribution])
    language = _text(detail.get("language")) or _text(dataset.language)
    if language:
        metadata.setLanguage(language)
    link = QgsLayerMetadata.Link(tr("GeoLens dataset page"), "WWW:LINK", page_url)
    metadata.setLinks([link])
    srid = detail.get("srid")
    if isinstance(srid, int) and not isinstance(srid, bool):
        crs = QgsCoordinateReferenceSystem(f"EPSG:{srid}")
        if crs.isValid():
            metadata.setCrs(crs)
    bbox = _bbox(detail, dataset)
    if bbox:
        # The catalogue reports extents in longitude and latitude.
        extent = QgsLayerMetadata.Extent()
        extent.setSpatialExtents([_spatial(*rect) for rect in _rectangles(bbox)])
        metadata.setExtent(extent)
    return metadata


def fetch_detail(client, dataset_id) -> dict:
    """The dataset's detail record, or {} when the request fails.

    Call it from a background task: it waits for the server.
    """
    try:
        return client.dataset_detail(dataset_id)
    except (GeoLensError, ValueError):
        return {}
