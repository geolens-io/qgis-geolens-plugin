"""GeoLens in the QGIS Browser panel: saved connections and their datasets."""

from __future__ import annotations

import html
import json
import webbrowser
from dataclasses import asdict
from typing import Callable
from urllib.parse import quote

from qgis.core import (
    Qgis,
    QgsDataCollectionItem,
    QgsDataItem,
    QgsDataItemProvider,
    QgsErrorItem,
    QgsMimeDataUtils,
)
from qgis.gui import QgsCustomDropHandler
from qgis.PyQt.QtCore import QLocale
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .client import Dataset, GeoLensError, same_server
from .connection import GeoLensConnection
from .connections import SavedConnection, saved_connections
from .i18n import tr

PROVIDER_KEY = "geolens"
ROOT_PATH = "geolens:"
# The most datasets one GeoLens search returns.
BROWSER_LIMIT = 1000
TOOLTIP_CHARS = 500


def client_for(connection: SavedConnection):
    """A client that sends `connection`'s credentials to its server only."""
    return GeoLensConnection(connection.url, authcfg=connection.authcfg).client


class GeoLensDataItemProvider(QgsDataItemProvider):
    """Adds a GeoLens root to the Browser panel.

    `dock()` returns the plugin's dock, created hidden if needed, whose add
    methods load the layers. `client_factory(connection)` builds a client
    for one saved connection; tests replace it.
    """

    def __init__(
        self,
        dock: Callable,
        icon: QIcon,
        client_factory: Callable = client_for,
    ):
        super().__init__()
        self._dock = dock
        self._icon = icon
        self._client_factory = client_factory

    def name(self):
        return "GeoLens"

    def capabilities(self):
        return Qgis.DataItemProviderCapability.NetworkSources

    def createDataItem(self, path, parentItem):
        if path:
            return None
        return GeoLensRootItem(parentItem, self._dock, self._icon, self._client_factory)


class GeoLensRootItem(QgsDataCollectionItem):
    def __init__(self, parent, dock, icon, client_factory):
        super().__init__(parent, "GeoLens", ROOT_PATH, PROVIDER_KEY)
        self._dock = dock
        self._client_factory = client_factory
        self.setIcon(icon)
        self.setToolTip(tr("Datasets on your saved GeoLens servers"))
        # Listing connections reads (and may migrate) settings, which must
        # happen on the main thread.
        self.setCapabilitiesV2(
            Qgis.BrowserItemCapability.Fast | Qgis.BrowserItemCapability.Fertile
        )

    def createChildren(self):
        return [
            GeoLensConnectionItem(self, connection, self._dock, self._client_factory)
            for connection in saved_connections()
        ]

    def actions(self, parent):
        return [
            _action(
                parent, tr("Refresh"), tr("Reload the saved connections"), self.refresh
            ),
            _action(
                parent,
                tr("Open GeoLens Panel"),
                tr("Show the GeoLens panel to connect to a server"),
                lambda: _show(self._dock()),
            ),
        ]


class GeoLensConnectionItem(QgsDataCollectionItem):
    """One saved connection; lists its datasets in a background thread."""

    def __init__(self, parent, connection, dock, client_factory):
        super().__init__(
            parent,
            connection.name,
            f"{ROOT_PATH}/{quote(connection.name, safe='')}",
            PROVIDER_KEY,
        )
        self.connection = connection
        self._dock = dock
        self._client_factory = client_factory
        self.setIconName("/mIconConnect.svg")
        self.setToolTip(connection.url)

    def equal(self, other):
        # A refresh keeps only items it considers equal, so a connection
        # saved again with another server or credential must not match.
        return super().equal(other) and (
            getattr(other, "connection", None) == self.connection
        )

    def createChildren(self):
        # Each listing gets a new client, so a refresh picks up changed
        # credentials and no client is shared between connections.
        try:
            client = self._client_factory(self.connection)
            datasets = client.search(limit=BROWSER_LIMIT, sort_by="title")
            children = [
                GeoLensDatasetItem(self, self.connection, client, dataset, self._dock)
                for dataset in datasets
            ]
        except GeoLensError as error:
            return [QgsErrorItem(self, str(error), self.path() + "/error")]
        except Exception as error:
            # This runs on a worker thread, where an escaping exception
            # aborts QGIS; a malformed catalog must only fail this listing.
            message = tr("GeoLens returned a catalog QGIS can't list: {error}")
            return [
                QgsErrorItem(self, message.format(error=error), self.path() + "/error")
            ]
        if len(datasets) >= BROWSER_LIMIT:
            children.append(GeoLensMoreItem(self, self._dock))
        return children

    def actions(self, parent):
        return [
            _action(
                parent,
                tr("Refresh"),
                tr("Reload the datasets on this server"),
                self.refresh,
            )
        ]


class GeoLensDatasetItem(QgsDataItem):
    """A dataset; the client that listed it also loads and renews its layers."""

    def __init__(self, parent, connection, client, dataset, dock):
        super().__init__(
            Qgis.BrowserItemType.Custom,
            parent,
            dataset.title,
            f"{parent.path()}/{quote(dataset.id, safe='')}",
            PROVIDER_KEY,
        )
        self.connection = connection
        self.client = client
        self.dataset = dataset
        self._dock = dock
        self.setState(Qgis.BrowserItemState.Populated)
        # By name, so QGIS loads the icon on the main thread: this item is
        # created on a worker thread, where pixmaps are unsafe.
        self.setIconName(_dataset_icon_name(dataset))
        self.setToolTip(_tooltip(dataset))

    def equal(self, other):
        return super().equal(other) and getattr(other, "dataset", None) == self.dataset

    def hasDragEnabled(self):
        return True

    def mimeUris(self):
        uri = QgsMimeDataUtils.Uri()
        uri.layerType = "custom"
        uri.providerKey = PROVIDER_KEY
        uri.name = self.dataset.title
        uri.uri = drop_payload(self.connection, self.dataset)
        return [uri]

    def handleDoubleClick(self):
        self.add_tiles()
        return True

    def add_tiles(self):
        self._dock().add_dataset_tiles(self.client, self.dataset)

    def add_features(self):
        self._dock().add_dataset_features(self.client, self.dataset)

    def open_metadata(self):
        webbrowser.open(self.client.metadata_url(self.dataset.id))

    def actions(self, parent):
        if self.dataset.is_raster:
            actions = [
                _action(
                    parent,
                    tr("Add Raster Tiles"),
                    tr("Add this dataset to the map as raster tiles"),
                    self.add_tiles,
                )
            ]
        else:
            actions = [
                _action(
                    parent,
                    tr("Add Vector Tiles"),
                    tr("Add this dataset to the map as vector tiles"),
                    self.add_tiles,
                ),
                _action(
                    parent,
                    tr("Add Features (Editable)"),
                    tr(
                        "Download the features so you can edit them and "
                        "sync the changes back to GeoLens"
                    ),
                    self.add_features,
                ),
            ]
        actions.append(
            _action(
                parent,
                tr("Open Metadata"),
                tr("Open the dataset's GeoLens page in your web browser"),
                self.open_metadata,
            )
        )
        return actions


class GeoLensMoreItem(QgsDataItem):
    """Says the list stops at BROWSER_LIMIT; double-click opens the panel."""

    def __init__(self, parent, dock):
        super().__init__(
            Qgis.BrowserItemType.Custom,
            parent,
            tr("More datasets in the GeoLens panel"),
            parent.path() + "/more",
            PROVIDER_KEY,
        )
        self._dock = dock
        self.setState(Qgis.BrowserItemState.Populated)
        self.setIconName("/search.svg")
        # After every dataset title.
        self.setSortKey("\uffff")
        self.setToolTip(
            tr(
                "This list shows the first {count} datasets by title. Search in "
                "the GeoLens panel to find the others."
            ).format(count=QLocale().toString(BROWSER_LIMIT))
        )

    def handleDoubleClick(self):
        _show(self._dock())
        return True


class GeoLensDropHandler(QgsCustomDropHandler):
    """Adds a dataset dragged from the Browser onto the map or the Layers panel."""

    def __init__(self, dock: Callable, client_factory: Callable = client_for):
        super().__init__()
        self._dock = dock
        self._client_factory = client_factory

    def customUriProviderKey(self):
        return PROVIDER_KEY

    def handleCustomUriDrop(self, uri):
        found = parse_drop_payload(uri.uri)
        if found is None:
            return
        connection, dataset = found
        self._dock().add_dataset_tiles(self._client_factory(connection), dataset)


def drop_payload(connection: SavedConnection, dataset: Dataset) -> str:
    return json.dumps(
        {
            "connection": connection.name,
            "url": connection.url,
            "dataset": asdict(dataset),
        }
    )


def parse_drop_payload(payload: str) -> tuple[SavedConnection, Dataset] | None:
    """The saved connection and dataset a drag names, or None.

    Drags can come from other applications, so the server comes from the
    saved connection, never from the payload, and a payload whose URL no
    longer matches that connection is refused.
    """
    try:
        data = json.loads(payload)
        name, url, fields = data["connection"], data["url"], data["dataset"]
        bbox = fields["bbox"]
        if bbox is not None:
            bbox = tuple(float(value) for value in bbox)
        keywords = fields.get("keywords", [])
        if not isinstance(keywords, list):
            return None
        dataset = Dataset(**{**fields, "bbox": bbox, "keywords": tuple(keywords)})
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    texts = (dataset.id, dataset.title, dataset.description, *dataset.keywords)
    optional = (
        dataset.record_type,
        dataset.geometry_type,
        dataset.license,
        dataset.language,
        dataset.source_organization,
    )
    if (
        not all(isinstance(value, str) for value in texts)
        or not all(value is None or isinstance(value, str) for value in optional)
        or (bbox is not None and len(bbox) != 4)
    ):
        return None
    for connection in saved_connections():
        if connection.name == name and same_server(connection.url, str(url)):
            return connection, dataset
    return None


def _action(parent, text, tip, callback):
    action = QAction(text, parent)
    action.setToolTip(tip)
    action.setStatusTip(tip)
    action.triggered.connect(lambda *_: callback())
    return action


def _show(dock):
    dock.show()
    dock.raise_()


def _dataset_icon_name(dataset: Dataset) -> str:
    if dataset.is_raster:
        return "/mIconRasterLayer.svg"
    geometry = (dataset.geometry_type or "").upper()
    for kind, name in (("POINT", "Point"), ("LINE", "Line"), ("POLYGON", "Polygon")):
        if kind in geometry:
            return f"/mIcon{name}Layer.svg"
    return "/mIconVectorTileLayer.svg"


def _tooltip(dataset: Dataset) -> str:
    # Escaped and wrapped in a paragraph so a description that contains
    # markup shows as text.
    text = dataset.description.strip() or dataset.title
    if len(text) > TOOLTIP_CHARS:
        text = text[: TOOLTIP_CHARS - 1].rstrip() + "…"
    return f"<p>{html.escape(text)}</p>"
