"""QGIS user interface and layer integration for GeoLens."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import webbrowser

from qgis.PyQt.QtCore import Qt, QSettings, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDataSourceUri,
    QgsJsonExporter,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsVectorTileLayer,
)
from qgis.gui import QgsAuthConfigSelect

from ..client import Dataset, GeoLensError, refresh_delay_seconds
from ..connection import (
    AUTHCFG_SETTING,
    LEGACY_KEY_SETTING,
    SERVER_SETTING,
    GeoLensConnection,
    migrate_plaintext_key,
    store_api_key,
)

ROLE_DATASET = Qt.ItemDataRole.UserRole
# QgsJsonExporter writes six decimal places by default (RFC 7946).
EXPORT_PRECISION = 6
SAMPLE_SERVERS = (("GeoLibre datasets", "https://datasets.geolibre.app"),)


class GeoLensDock(QDockWidget):
    def __init__(self, iface, opener=None):
        super().__init__("GeoLens", iface.mainWindow())
        self.setObjectName("GeoLensDock")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.iface = iface
        self._opener = opener
        self.connection: GeoLensConnection | None = None
        self.client = None
        self.datasets: dict[str, Dataset] = {}
        self.baselines: dict[str, dict[int, dict]] = {}
        self.temp_files: list[str] = []
        self.refresh_timers: dict[str, QTimer] = {}
        self.editing_enabled = False
        self._build_ui()
        self._restore_settings()

    def _build_ui(self):
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        intro = QLabel("Browse and add datasets from a self-hosted GeoLens server.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.samples = QComboBox()
        self.samples.addItem("Sample server…", "")
        for label, url in SAMPLE_SERVERS:
            self.samples.addItem(label, url)
        self.samples.currentIndexChanged.connect(self._choose_sample)
        layout.addWidget(self.samples)

        form = QFormLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://datasets.geolibre.app")
        self.auth_select = QgsAuthConfigSelect(self)
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText(
            "Optional; used instead of the authentication above"
        )
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.save_key = QCheckBox("Save API key in the QGIS authentication database")
        self.save_key.setToolTip(
            "The key is encrypted with the QGIS master password. Unchecked, "
            "it is kept in memory for this session only."
        )
        form.addRow("Server", self.url_edit)
        form.addRow("Authentication", self.auth_select)
        form.addRow("API key", self.key_edit)
        form.addRow(self.save_key)
        layout.addLayout(form)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_server)
        layout.addWidget(self.connect_button)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search the catalog")
        self.search_edit.returnPressed.connect(self.search)
        search_button = QPushButton("Search")
        search_button.clicked.connect(self.search)
        search_row.addWidget(self.search_edit)
        search_row.addWidget(search_button)
        layout.addLayout(search_row)

        self.results = QTreeWidget()
        self.results.setHeaderLabels(["Dataset", "Type", "Features"])
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.itemSelectionChanged.connect(self._update_actions)
        layout.addWidget(self.results, 1)

        options = QFormLayout()
        self.limit = QSpinBox()
        self.limit.setRange(1, 1_000_000)
        self.limit.setValue(10_000)
        self.view_only = QPushButton("Current map extent: On")
        self.view_only.setCheckable(True)
        self.view_only.setChecked(True)
        self.view_only.toggled.connect(
            lambda value: self.view_only.setText(
                f"Current map extent: {'On' if value else 'Off'}"
            )
        )
        options.addRow("Feature limit", self.limit)
        options.addRow(self.view_only)
        layout.addLayout(options)

        first_actions = QHBoxLayout()
        self.tiles_button = QPushButton("Add tiles")
        self.features_button = QPushButton("Add features")
        self.metadata_button = QPushButton("Metadata")
        self.tiles_button.clicked.connect(self.add_tiles)
        self.features_button.clicked.connect(self.add_features)
        self.metadata_button.clicked.connect(self.open_metadata)
        first_actions.addWidget(self.tiles_button)
        first_actions.addWidget(self.features_button)
        first_actions.addWidget(self.metadata_button)
        layout.addLayout(first_actions)

        self.sync_button = QPushButton("Sync selected GeoLens layer edits")
        self.sync_button.clicked.connect(self.sync_active_layer)
        layout.addWidget(self.sync_button)
        self.status = QLabel("Not connected")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.setWidget(root)
        self._update_actions()

    def _restore_settings(self):
        settings = QSettings()
        self.url_edit.setText(settings.value(SERVER_SETTING, "", str))
        migrate_plaintext_key(settings, self._message)
        self.auth_select.setConfigId(settings.value(AUTHCFG_SETTING, "", str))
        # A key that could not be moved yet still works for this session.
        self.key_edit.setText(settings.value(LEGACY_KEY_SETTING, "", str))
        self.limit.setValue(settings.value("geolens/feature_limit", 10000, int))

    def _choose_sample(self, index):
        url = self.samples.itemData(index)
        if url:
            self.url_edit.setText(url)
            self.auth_select.setConfigId("")
            self.key_edit.clear()

    def _selected_dataset(self) -> Dataset | None:
        items = self.results.selectedItems()
        return items[0].data(0, ROLE_DATASET) if items else None

    def _update_actions(self):
        dataset = self._selected_dataset()
        connected = self.client is not None
        self.tiles_button.setEnabled(connected and dataset is not None)
        self.features_button.setEnabled(
            connected and dataset is not None and not dataset.is_raster
        )
        self.metadata_button.setEnabled(connected and dataset is not None)
        layer = self.iface.activeLayer()
        # Only layers with an in-session baseline can be diffed against the server.
        self.sync_button.setEnabled(
            bool(
                connected
                and self.editing_enabled
                and layer
                and layer.id() in self.baselines
            )
        )

    def _message(self, text, level=Qgis.MessageLevel.Info):
        self.status.setText(text)
        self.iface.messageBar().pushMessage("GeoLens", text, level=level, duration=6)

    def connect_server(self):
        key = self.key_edit.text().strip()
        try:
            connection = GeoLensConnection(
                self.url_edit.text(),
                authcfg=self.auth_select.configId(),
                api_key=key,
                opener=self._opener,
            )
            datasets = connection.connect()
        except (ValueError, GeoLensError) as error:
            self._disconnect()
            self._message(str(error), Qgis.MessageLevel.Critical)
            return
        self.connection = connection
        self.client = connection.client
        self.editing_enabled = connection.capabilities["dataset_editing"]
        if key and self.save_key.isChecked():
            self._save_key(key, connection.base_url)
        settings = QSettings()
        settings.setValue(SERVER_SETTING, connection.base_url)
        settings.setValue(AUTHCFG_SETTING, self.auth_select.configId())
        self._show_datasets(datasets)
        suffix = (
            " Editing is enabled."
            if self.editing_enabled
            else " Editing is disabled by this server."
        )
        self._message(f"Connected — {len(datasets)} datasets.{suffix}")

    def _disconnect(self):
        """Forget the current connection and the catalog it returned."""
        self.connection = None
        self.client = None
        self.editing_enabled = False
        self._show_datasets([])

    def _save_key(self, key, server_url):
        authcfg = store_api_key(
            key,
            server_url,
            self._message,
            "Saving the API key in the QGIS authentication database.",
        )
        if not authcfg:
            self._message(
                "The API key could not be saved in the QGIS authentication "
                "database. It is kept in memory for this session only.",
                Qgis.MessageLevel.Warning,
            )
            return
        settings = QSettings()
        if settings.value(LEGACY_KEY_SETTING, "", str).strip() == key:
            settings.remove(LEGACY_KEY_SETTING)
        self.auth_select.setConfigId(authcfg)
        self.key_edit.clear()
        self.save_key.setChecked(False)

    def search(self):
        if not self.client:
            self.connect_server()
            return
        try:
            datasets = self.client.search(self.search_edit.text(), 50)
            self._show_datasets(datasets)
            self.status.setText(f"{len(datasets)} dataset(s)")
        except GeoLensError as error:
            self._message(str(error), Qgis.MessageLevel.Critical)

    def _show_datasets(self, datasets):
        self.results.clear()
        self.datasets = {dataset.id: dataset for dataset in datasets}
        for dataset in datasets:
            count = (
                "" if dataset.feature_count is None else f"{dataset.feature_count:,}"
            )
            item = QTreeWidgetItem(
                [dataset.title, "raster" if dataset.is_raster else "vector", count]
            )
            item.setData(0, ROLE_DATASET, dataset)
            item.setToolTip(0, dataset.description)
            self.results.addTopLevelItem(item)
        self.results.resizeColumnToContents(0)
        self._update_actions()

    def add_tiles(self):
        dataset = self._selected_dataset()
        if not self.client or not dataset:
            return
        try:
            source = self.client.tile_source(dataset.id)
            if source.get("kind") == "raster":
                self._add_raster(dataset, source)
            else:
                self._add_vector_tiles(dataset, source)
        except GeoLensError as error:
            self._message(str(error), Qgis.MessageLevel.Critical)

    def _uri(self, source):
        # Never put an API key in a layer URI: QGIS persists layer sources in
        # project files. The URL carries a short-lived signed tile token
        # instead, which _schedule_refresh keeps renewed.
        uri = QgsDataSourceUri()
        uri.setParam("type", "xyz")
        uri.setParam("url", source["url"])
        if source.get("minzoom") is not None:
            uri.setParam("zmin", str(source["minzoom"]))
        if source.get("maxzoom") is not None:
            uri.setParam("zmax", str(source["maxzoom"]))
        return bytes(uri.encodedUri()).decode()

    def _tag_layer(self, layer, dataset):
        layer.setCustomProperty("geolens/server", self.client.base_url)
        layer.setCustomProperty("geolens/dataset_id", dataset.id)
        layer.setCustomProperty(
            "geolens/metadata_url", self.client.metadata_url(dataset.id)
        )

    def _add_raster(self, dataset, source):
        layer = QgsRasterLayer(self._uri(source), dataset.title, "wms")
        if not layer.isValid():
            raise GeoLensError("QGIS could not create the GeoLens raster tile layer")
        self._tag_layer(layer, dataset)
        QgsProject.instance().addMapLayer(layer)
        self._schedule_refresh(layer, dataset, source, "wms")
        self._zoom_to(dataset)
        self._message(f"Added raster tiles: {dataset.title}")

    def _add_vector_tiles(self, dataset, source):
        layer = QgsVectorTileLayer(self._uri(source), dataset.title)
        if not layer.isValid():
            raise GeoLensError("QGIS could not create the GeoLens vector tile layer")
        self._tag_layer(layer, dataset)
        layer.setCustomProperty("geolens/source_layer", source.get("source_layer", ""))
        QgsProject.instance().addMapLayer(layer)
        self._schedule_refresh(layer, dataset, source, "vectortile")
        self._zoom_to(dataset)
        self._message(f"Added vector tiles: {dataset.title}")

    def _schedule_refresh(self, layer, dataset, source, provider, client=None):
        # The dock can reconnect to another server while this layer is open;
        # renewals must keep asking the server that issued the layer.
        client = client or self.client
        seconds = refresh_delay_seconds(source.get("expires_in"))
        layer_id = layer.id()
        timer = QTimer(self)
        timer.setSingleShot(True)

        def refresh():
            # Removing a layer deletes the object behind `layer`, so look it
            # up again instead of touching the stale wrapper.
            live = QgsProject.instance().mapLayer(layer_id)
            if not live:
                self._drop_refresh_timer(layer_id)
                return
            try:
                renewed = client.tile_source(dataset.id)
                live.setDataSource(self._uri(renewed), live.name(), provider)
                self._schedule_refresh(live, dataset, renewed, provider, client)
            except GeoLensError:
                timer.start(60_000)

        timer.timeout.connect(refresh)
        timer.start(seconds * 1000)
        self._drop_refresh_timer(layer_id)
        self.refresh_timers[layer_id] = timer

    def _drop_refresh_timer(self, layer_id):
        timer = self.refresh_timers.pop(layer_id, None)
        if timer:
            timer.stop()
            timer.deleteLater()

    def _current_bbox(self):
        extent = self.iface.mapCanvas().extent()
        crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        if crs != QgsCoordinateReferenceSystem("EPSG:4326"):
            extent = QgsCoordinateTransform(
                crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()
            ).transformBoundingBox(extent)
        return (
            extent.xMinimum(),
            extent.yMinimum(),
            extent.xMaximum(),
            extent.yMaximum(),
        )

    def add_features(self):
        dataset = self._selected_dataset()
        if not self.client or not dataset:
            return
        try:
            bbox = self._current_bbox() if self.view_only.isChecked() else None
            collection = self.client.features(dataset.id, self.limit.value(), bbox)
            baseline = {}
            unidentified = 0
            for feature in collection["features"]:
                # GeoJSON permits "properties": null, which setdefault keeps.
                props = feature.get("properties")
                if not isinstance(props, dict):
                    props = {}
                    feature["properties"] = props
                gid = feature.get("id", props.get("gid"))
                if isinstance(gid, int) and not isinstance(gid, bool):
                    props["_geolens_gid"] = gid
                    baseline[gid] = _clean_feature(feature)
                else:
                    unidentified += 1
            handle, path = tempfile.mkstemp(prefix="geolens_", suffix=".geojson")
            os.close(handle)
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(collection, stream)
            self.temp_files.append(path)
            layer = QgsVectorLayer(path, dataset.title, "ogr")
            if not layer.isValid():
                raise GeoLensError("QGIS could not create the GeoLens feature layer")
            self._tag_layer(layer, dataset)
            # Synchronization matches features by integer server ID. A dataset
            # that uses string or UUID identifiers would look entirely new and
            # be duplicated onto the server, so it is loaded read-only instead.
            syncable = unidentified == 0
            layer.setCustomProperty("geolens/editable", syncable)
            if syncable:
                self.baselines[layer.id()] = baseline
            QgsProject.instance().addMapLayer(layer)
            self._zoom_to(dataset)
            QSettings().setValue("geolens/feature_limit", self.limit.value())
            if syncable:
                self._message(
                    f"Added {layer.featureCount():,} editable features: {dataset.title}"
                )
            else:
                self._message(
                    f"Added {layer.featureCount():,} features: {dataset.title}. "
                    f"{unidentified:,} feature(s) have no integer server ID, so "
                    "synchronization is disabled for this layer.",
                    Qgis.MessageLevel.Warning,
                )
        except (GeoLensError, OSError) as error:
            self._message(str(error), Qgis.MessageLevel.Critical)

    def _zoom_to(self, dataset):
        if dataset.bbox:
            rect = QgsRectangle(*dataset.bbox)
            transform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"),
                self.iface.mapCanvas().mapSettings().destinationCrs(),
                QgsProject.instance(),
            )
            self.iface.mapCanvas().setExtent(transform.transformBoundingBox(rect))
            self.iface.mapCanvas().refresh()

    def open_metadata(self):
        dataset = self._selected_dataset()
        if self.client and dataset:
            webbrowser.open(self.client.metadata_url(dataset.id))

    def sync_active_layer(self):
        layer = self.iface.activeLayer()
        if not self.client or not isinstance(layer, QgsVectorLayer):
            self._message(
                "Select an editable feature layer loaded from GeoLens.",
                Qgis.MessageLevel.Warning,
            )
            return
        dataset_id = layer.customProperty("geolens/dataset_id", "")
        baseline = self.baselines.get(layer.id())
        if not dataset_id or baseline is None:
            self._message(
                "The selected layer has no GeoLens edit baseline in this session. "
                "Reload it with Add features; datasets without integer feature "
                "IDs cannot be synchronized.",
                Qgis.MessageLevel.Warning,
            )
            return
        if layer.isEditable() and not layer.commitChanges():
            self._message(
                "Commit the QGIS layer edits before synchronizing.",
                Qgis.MessageLevel.Warning,
            )
            return
        current = {}
        creates = []
        exporter = QgsJsonExporter(layer)
        exporter.setIncludeGeometry(True)
        for qfeature in layer.getFeatures():
            feature = json.loads(exporter.exportFeature(qfeature))
            gid = feature.get("properties", {}).get("_geolens_gid")
            cleaned = _clean_feature(feature)
            if isinstance(gid, int):
                current[gid] = cleaned
            else:
                creates.append((qfeature.id(), cleaned))
        updates = []
        for gid, feature in current.items():
            previous = baseline.get(gid)
            if previous is None:
                updates.append((gid, feature))
                continue
            # QgsJsonExporter rounds coordinates, so an untouched server
            # geometry can differ from its export. Compare at export precision
            # and keep the server geometry when only attributes changed.
            same_geometry = _rounded(previous.get("geometry")) == _rounded(
                feature.get("geometry")
            )
            if same_geometry:
                if previous.get("properties") == feature.get("properties"):
                    continue
                feature = {**feature, "geometry": previous.get("geometry")}
            updates.append((gid, feature))
        deletes = sorted(set(baseline) - set(current))
        total = len(creates) + len(updates) + len(deletes)
        if not total:
            self._message("No local changes to synchronize.")
            return
        if (
            deletes
            and QMessageBox.question(
                self,
                "Delete GeoLens features",
                f"Synchronizing will delete {len(deletes)} server feature(s). Continue?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        errors = []
        for gid, feature in updates:
            try:
                self.client.update_feature(dataset_id, gid, feature)
                baseline[gid] = feature
            except GeoLensError as error:
                errors.append(str(error))
        created = []
        unaddressable = 0
        for fid, feature in creates:
            try:
                gid = self.client.create_feature(dataset_id, feature)
            except GeoLensError as error:
                errors.append(str(error))
                continue
            if gid is None:
                # The feature exists on the server but cannot be addressed, so
                # a second sync would create it again. The baseline is dropped
                # below and the layer has to be reloaded first.
                unaddressable += 1
                errors.append(
                    "A feature was created, but GeoLens returned no integer ID "
                    "for it. Reload the layer before synchronizing again."
                )
                continue
            baseline[gid] = feature
            created.append((fid, gid))
        # One edit session for every new ID: each commit rewrites the layer file.
        gid_index = layer.fields().indexOf("_geolens_gid")
        if created and gid_index >= 0:
            layer.startEditing()
            for fid, gid in created:
                layer.changeAttributeValue(fid, gid_index, gid)
            if not layer.commitChanges():
                unaddressable += len(created)
                errors.append(
                    f"{len(created)} feature(s) were created, but their server "
                    "IDs could not be stored locally. Reload the layer before "
                    "synchronizing again."
                )
        for gid in deletes:
            try:
                self.client.delete_feature(dataset_id, gid)
                baseline.pop(gid, None)
            except GeoLensError as error:
                errors.append(str(error))
        if unaddressable:
            # Features live on the server that this layer cannot match again.
            self.baselines.pop(layer.id(), None)
            self._update_actions()
        if errors:
            self._message(
                f"Synchronized {total - len(errors)}/{total}; {len(errors)} failed. {errors[0]}",
                Qgis.MessageLevel.Warning,
            )
        else:
            self._message(f"Synchronized {total} change(s) to GeoLens.")

    def cleanup(self):
        """Stop tile refresh timers and delete downloaded feature files.

        Called by the plugin on unload; hiding the dock deliberately keeps
        everything alive so a reopened dock resumes where it left off.
        """
        for timer in self.refresh_timers.values():
            timer.stop()
        self.refresh_timers.clear()
        for path in self.temp_files:
            with contextlib.suppress(OSError):
                os.unlink(path)
        self.temp_files.clear()

    def closeEvent(self, event):
        # Hiding retains in-memory API keys, baselines, and tile refresh timers.
        event.ignore()
        self.hide()


def _clean_feature(feature):
    properties = dict(feature.get("properties") or {})
    properties.pop("_geolens_gid", None)
    return {
        "type": "Feature",
        "geometry": feature.get("geometry"),
        "properties": properties,
    }


def _rounded(node, ndigits=EXPORT_PRECISION):
    """Round every coordinate in a GeoJSON geometry to `ndigits` decimals."""
    if isinstance(node, float):
        return round(node, ndigits)
    if isinstance(node, list):
        return [_rounded(value, ndigits) for value in node]
    if isinstance(node, dict):
        return {key: _rounded(value, ndigits) for key, value in node.items()}
    return node
