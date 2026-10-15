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
from qgis.gui import QgsCollapsibleGroupBox

from ..browser import ROOT_PATH as BROWSER_ROOT_PATH
from ..client import (
    Dataset,
    GeoLensError,
    refresh_delay_seconds,
    same_origin,
    same_server,
)
from ..connection import (
    LEGACY_KEY_SETTING,
    GeoLensConnection,
    forget_legacy_key,
    legacy_key_server,
    migrate_plaintext_key,
    origin_label,
    store_api_key,
)
from ..connections import (
    SavedConnection,
    adopt_auth_config,
    current_connection_name,
    host_label,
    normalized_url,
    remove_connection,
    save_connection,
    saved_connections,
    set_current_connection,
    unique_name,
)
from ..i18n import tr
from ..layer_info import apply_layer_info
from .connection_dialog import ConnectionDialog

ROLE_DATASET = Qt.ItemDataRole.UserRole
SAMPLE_SERVERS = (
    ("GeoLens demo", "https://demo.getgeolens.com"),
    ("GeoLibre datasets", "https://datasets.geolibre.app"),
)


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
        # The saved connection that `connection` was opened from.
        self._connected_to: SavedConnection | None = None
        self.client = None
        self.datasets: dict[str, Dataset] = {}
        self.baselines: dict[str, dict[int, dict]] = {}
        self.temp_files: list[str] = []
        self.refresh_timers: dict[str, QTimer] = {}
        self.editing_enabled = False
        # A plain-text key from an older version and its server, kept while
        # the user hasn't typed over it so a return to that server refills it.
        self._legacy_key = ""
        self._legacy_key_server = ""
        # Keys pasted for this session only: connection name to (URL, key).
        # A key comes back only while its connection keeps the same origin.
        self._session_keys: dict[str, tuple[str, str]] = {}
        # The connection whose key the API key field holds.
        self._key_owner: SavedConnection | None = None
        self._build_ui()
        self._restore_settings()

    def _build_ui(self):
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        intro = QLabel("Browse and add datasets from a self-hosted GeoLens server.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.connection_box = QgsCollapsibleGroupBox(tr("Connection"))
        self.connection_box.setAccessibleName(tr("GeoLens connection"))
        box = QVBoxLayout(self.connection_box)
        self.connection_combo = QComboBox()
        self.connection_combo.setToolTip(tr("Saved GeoLens connections"))
        self.connection_combo.setAccessibleName(tr("Saved connection"))
        self.connection_combo.currentIndexChanged.connect(self._connection_chosen)
        box.addWidget(self.connection_combo)
        chooser = QHBoxLayout()
        self.new_button = QPushButton(tr("New"))
        self.new_button.setToolTip(tr("Add a GeoLens connection"))
        self.new_button.setAccessibleName(tr("New connection"))
        self.new_button.clicked.connect(self.new_connection)
        self.edit_button = QPushButton(tr("Edit"))
        self.edit_button.setToolTip(tr("Edit the selected connection"))
        self.edit_button.setAccessibleName(tr("Edit connection"))
        self.edit_button.clicked.connect(self.edit_connection)
        self.remove_button = QPushButton(tr("Remove"))
        self.remove_button.setToolTip(tr("Remove the selected connection"))
        self.remove_button.setAccessibleName(tr("Remove connection"))
        self.remove_button.clicked.connect(self.remove_connection)
        for button in (self.new_button, self.edit_button, self.remove_button):
            chooser.addWidget(button)
        box.addLayout(chooser)

        self.samples = QComboBox()
        self.samples.addItem(tr("Add a sample server…"), "")
        for label, url in SAMPLE_SERVERS:
            self.samples.addItem(label, url)
        self.samples.setToolTip(
            tr("Add a public GeoLens server as an anonymous connection")
        )
        self.samples.setAccessibleName(tr("Sample server"))
        self.samples.currentIndexChanged.connect(self._choose_sample)
        box.addWidget(self.samples)

        form = QFormLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText(tr("Optional; kept for this session"))
        self.key_edit.setToolTip(
            tr(
                "An API key for the selected connection. Unless you save it, "
                "it is kept in memory for this session only."
            )
        )
        self.key_edit.setAccessibleName(tr("API key"))
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.textEdited.connect(self._key_typed)
        self.save_key = QCheckBox(
            tr("Save API key in the QGIS authentication database")
        )
        self.save_key.setToolTip(
            tr(
                "The key is encrypted with the QGIS master password and saved "
                "with the selected connection. Unchecked, it is kept in memory "
                "for this session only."
            )
        )
        form.addRow(tr("API key"), self.key_edit)
        form.addRow(self.save_key)
        box.addLayout(form)

        self.connect_button = QPushButton(tr("Connect"))
        self.connect_button.setToolTip(tr("Connect to the selected GeoLens server"))
        self.connect_button.clicked.connect(self.connect_server)
        box.addWidget(self.connect_button)
        layout.addWidget(self.connection_box)

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
        self.iface.currentLayerChanged.connect(self._update_actions)
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
        moved = migrate_plaintext_key(settings, self._message)
        saved_connections(settings)
        # A key moved while another config was selected still needs a home.
        adopt_auth_config(moved, settings)
        self._restore_legacy_key(settings)
        self._refresh_connections(current_connection_name(settings))
        self.limit.setValue(settings.value("geolens/feature_limit", 10000, int))

    def _restore_legacy_key(self, settings):
        """Remember a key that could not be moved yet, for its own server only."""
        key = settings.value(LEGACY_KEY_SETTING, "", str).strip()
        if not key:
            return
        server = legacy_key_server(settings)
        if not server:
            self._message(
                tr(
                    "An API key saved by an older version of GeoLens doesn't "
                    "record which server it belongs to, so it isn't filled in. "
                    "Paste it again for its server."
                ),
                Qgis.MessageLevel.Warning,
            )
            return
        self._legacy_key, self._legacy_key_server = key, server

    def _key_typed(self, *_):
        # A key the user typed belongs to the selected connection only.
        self._legacy_key = ""
        self._key_owner = self._current_connection()

    def _current_connection(self) -> SavedConnection | None:
        name = self.connection_combo.currentData()
        return next((c for c in saved_connections() if c.name == name), None)

    def _refresh_connections(self, select=""):
        """List the saved connections again and select `select` if it exists."""
        self._stash_session_key()
        self.connection_combo.blockSignals(True)
        self.connection_combo.clear()
        for connection in saved_connections():
            self.connection_combo.addItem(connection.name, connection.name)
            self.connection_combo.setItemData(
                self.connection_combo.count() - 1,
                connection.url,
                Qt.ItemDataRole.ToolTipRole,
            )
        index = self.connection_combo.findData(select) if select else -1
        self.connection_combo.setCurrentIndex(
            index if index >= 0 else min(0, self.connection_combo.count() - 1)
        )
        self.connection_combo.blockSignals(False)
        self._connection_chosen()
        self._refresh_browser()

    def _refresh_browser(self):
        """List the saved connections again in the QGIS Browser panel."""
        self.iface.browserModel().refresh(BROWSER_ROOT_PATH)

    def _stash_session_key(self):
        """Keep the key typed for the connection being left, for that connection."""
        owner, key = self._key_owner, self.key_edit.text().strip()
        if owner is None:
            return
        if key:
            self._session_keys[owner.name] = (owner.url, key)
        else:
            self._session_keys.pop(owner.name, None)

    def _connection_chosen(self, *_):
        self._stash_session_key()
        connection = self._current_connection()
        set_current_connection(connection.name if connection else "")
        connected = self._connected_to
        if connected is not None and (
            connection is None
            or connection.name != connected.name
            or not same_server(connection.url, connected.url)
            or connection.authcfg != connected.authcfg
        ):
            # Search and the dataset actions would still go to the old server,
            # or with credentials the connection no longer names.
            self._disconnect()
        self._key_owner = connection
        self.key_edit.setText(self._key_for(connection))
        for widget in (self.edit_button, self.remove_button, self.connect_button):
            widget.setEnabled(connection is not None)
        self.connection_box.setTitle(
            tr("Connection: {name}").format(name=connection.name)
            if connection
            else tr("Connection")
        )

    def _key_for(self, connection) -> str:
        """The session or legacy key saved for `connection`'s origin, or ""."""
        if connection is None:
            return ""
        url, key = self._session_keys.get(connection.name, ("", ""))
        if key and same_origin(url, connection.url):
            return key
        if self._legacy_key and same_origin(self._legacy_key_server, connection.url):
            return self._legacy_key
        return ""

    def use_server(self, url, name=""):
        """Select an anonymous saved connection for `url`, adding one if needed.

        A connection with an auth config is never chosen. A new connection
        is called `name`, or the server's host, made unique. Returns the
        selected connection. Raises ValueError when `url` isn't an HTTP(S)
        server URL.
        """
        url = normalized_url(url)
        connection = next(
            (
                c
                for c in saved_connections()
                if not c.authcfg and same_server(c.url, url)
            ),
            None,
        )
        if connection is None:
            connection = SavedConnection(unique_name(name or host_label(url)), url, "")
            save_connection(connection)
        self._refresh_connections(connection.name)
        return connection

    def new_connection(self):
        dialog = ConnectionDialog(
            self, [c.name for c in saved_connections()], notify=self._message
        )
        if dialog.exec():
            save_connection(dialog.connection())
            self._refresh_connections(dialog.connection().name)

    def edit_connection(self):
        current = self._current_connection()
        if current is None:
            return
        self._stash_session_key()
        others = [c.name for c in saved_connections() if c.name != current.name]
        dialog = ConnectionDialog(self, others, current, self._message)
        if not dialog.exec():
            return
        edited = dialog.connection()
        save_connection(edited, previous_name=current.name)
        # The stashed key keeps the URL it was typed for, so a new origin
        # leaves it out.
        if current.name in self._session_keys:
            self._session_keys[edited.name] = self._session_keys.pop(current.name)
        self._key_owner = None
        self._refresh_connections(edited.name)

    def remove_connection(self):
        current = self._current_connection()
        if current is None:
            return
        answer = QMessageBox.question(
            self,
            tr("Remove GeoLens connection"),
            tr(
                "Remove the connection {name}? Any API key it uses stays in the "
                "QGIS authentication database."
            ).format(name=current.name),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        remove_connection(current.name)
        self._session_keys.pop(current.name, None)
        self._key_owner = None
        self._refresh_connections()

    def _choose_sample(self, index):
        url = self.samples.itemData(index)
        if url:
            connection = self.use_server(url, self.samples.itemText(index))
            # A public sample is for browsing anonymously, so a session that
            # carries a pasted key ends, and an older key for the same server
            # isn't filled in.
            self._disconnect()
            self._session_keys.pop(connection.name, None)
            self.key_edit.clear()
            self.samples.setCurrentIndex(0)

    def _selected_dataset(self) -> Dataset | None:
        items = self.results.selectedItems()
        return items[0].data(0, ROLE_DATASET) if items else None

    def _update_actions(self, *_):
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
        saved = self._current_connection()
        if saved is None:
            self._disconnect()
            self.connection_box.setCollapsed(False)
            self._message(
                tr("Choose a GeoLens connection, or add one with New."),
                Qgis.MessageLevel.Warning,
            )
            return
        key = self.key_edit.text().strip()
        owner = self._key_owner
        if key and (
            owner is None
            or owner.name != saved.name
            or not same_origin(owner.url, saved.url)
        ):
            # The field is refilled whenever the selection changes, so this
            # only guards against a key left over for another connection.
            self.key_edit.clear()
            self._disconnect()
            self.connection_box.setCollapsed(False)
            self._message(
                tr(
                    "The API key in the panel wasn't entered for {name}, so "
                    "GeoLens won't send it to {target}. Paste it again."
                ).format(name=saved.name, target=origin_label(saved.url)),
                Qgis.MessageLevel.Warning,
            )
            return
        try:
            connection = GeoLensConnection(
                saved.url,
                authcfg=saved.authcfg,
                api_key=key,
                opener=self._opener,
            )
            datasets = connection.connect()
        except (ValueError, GeoLensError) as error:
            self._disconnect()
            self.connection_box.setCollapsed(False)
            self._message(str(error), Qgis.MessageLevel.Critical)
            return
        self.connection = connection
        self._connected_to = saved
        self.client = connection.client
        self.editing_enabled = connection.capabilities["dataset_editing"]
        if key and self.save_key.isChecked():
            self._save_key(key, saved)
        self._show_datasets(datasets)
        self.connection_box.setCollapsed(True)
        suffix = (
            " Editing is enabled."
            if self.editing_enabled
            else " Editing is disabled by this server."
        )
        self._message(f"Connected — {len(datasets)} datasets.{suffix}")

    def _disconnect(self):
        """Forget the current connection and the catalog it returned."""
        self.connection = None
        self._connected_to = None
        self.client = None
        self.editing_enabled = False
        self._show_datasets([])

    def _save_key(self, key, saved):
        authcfg = store_api_key(
            key,
            saved.url,
            self._message,
            tr("Saving the API key in the QGIS authentication database."),
        )
        if not authcfg:
            self._message(
                tr(
                    "The API key could not be saved in the QGIS authentication "
                    "database. It is kept in memory for this session only."
                ),
                Qgis.MessageLevel.Warning,
            )
            return
        settings = QSettings()
        if settings.value(LEGACY_KEY_SETTING, "", str).strip() == key:
            forget_legacy_key(settings)
            self._legacy_key = ""
        saved = SavedConnection(saved.name, saved.url, authcfg)
        save_connection(saved)
        self._refresh_browser()
        # The live client already sends this key.
        if self._connected_to is not None and self._connected_to.name == saved.name:
            self._connected_to = saved
        self._session_keys.pop(saved.name, None)
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
        if self.client and dataset:
            self.add_dataset_tiles(self.client, dataset)

    def add_dataset_tiles(self, client, dataset):
        """Add `dataset` as a tile layer served, and kept renewed, by `client`."""
        try:
            source = client.tile_source(dataset.id)
            if source.get("kind") == "raster":
                self._add_raster(client, dataset, source)
            else:
                self._add_vector_tiles(client, dataset, source)
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

    def _tag_layer(self, layer, dataset, client=None):
        client = client or self.client
        layer.setCustomProperty("geolens/server", client.base_url)
        layer.setCustomProperty("geolens/dataset_id", dataset.id)
        layer.setCustomProperty("geolens/metadata_url", client.metadata_url(dataset.id))
        apply_layer_info(layer, dataset, client)

    def _add_raster(self, client, dataset, source):
        layer = QgsRasterLayer(self._uri(source), dataset.title, "wms")
        if not layer.isValid():
            raise GeoLensError("QGIS could not create the GeoLens raster tile layer")
        self._tag_layer(layer, dataset, client=client)
        QgsProject.instance().addMapLayer(layer)
        self._schedule_refresh(layer, dataset, source, "wms", client)
        self._zoom_to(dataset)
        self._message(f"Added raster tiles: {dataset.title}")

    def _add_vector_tiles(self, client, dataset, source):
        layer = QgsVectorTileLayer(self._uri(source), dataset.title)
        if not layer.isValid():
            raise GeoLensError("QGIS could not create the GeoLens vector tile layer")
        self._tag_layer(layer, dataset, client=client)
        layer.setCustomProperty("geolens/source_layer", source.get("source_layer", ""))
        QgsProject.instance().addMapLayer(layer)
        self._schedule_refresh(layer, dataset, source, "vectortile", client)
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
        if self.client and dataset:
            self.add_dataset_features(self.client, dataset)

    def add_dataset_features(self, client, dataset):
        """Download `dataset` from `client` as an editable layer with a sync baseline."""
        try:
            bbox = self._current_bbox() if self.view_only.isChecked() else None
            before = _table_id_probe(client, dataset.id)
            collection = client.features(dataset.id, self.limit.value(), bbox)
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
            self._tag_layer(layer, dataset, client=client)
            # Synchronization matches features by integer server ID. A dataset
            # that uses string or UUID identifiers would look entirely new and
            # be duplicated onto the server, so it is loaded read-only instead.
            syncable = unidentified == 0
            refusal = ""
            if syncable:
                self.baselines[layer.id()] = baseline
                refusal = self._start_sync_baseline(client, layer, dataset.id, before)
                syncable = not refusal
            layer.setCustomProperty("geolens/editable", syncable)
            QgsProject.instance().addMapLayer(layer)
            self._zoom_to(dataset)
            QSettings().setValue("geolens/feature_limit", self.limit.value())
            if syncable:
                self._message(
                    f"Added {layer.featureCount():,} editable features: {dataset.title}"
                )
            elif refusal:
                self._message(
                    tr("Added {count} features: {title}. {reason}").format(
                        count=f"{layer.featureCount():,}",
                        title=dataset.title,
                        reason=refusal,
                    ),
                    Qgis.MessageLevel.Warning,
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
        origin = str(layer.customProperty("geolens/server", "") or "")
        if not same_server(origin, self.client.base_url):
            # The dataset id and the baseline belong to the server that
            # issued the layer, so its edits go to no other server.
            self._message(
                (
                    f"This layer came from {origin}. Connect to that server to "
                    "synchronize its edits."
                    if origin
                    else "This layer doesn't record which GeoLens server it came "
                    "from, so its edits can't be synchronized."
                ),
                Qgis.MessageLevel.Warning,
            )
            return
        editing = layer.isEditable()
        if editing and not layer.commitChanges():
            self._message(
                "Commit the QGIS layer edits before synchronizing.",
                Qgis.MessageLevel.Warning,
            )
            return
        current = {}
        creates = []
        for fid, gid, feature in _exported_features(layer):
            if isinstance(gid, int):
                current[gid] = feature
            else:
                creates.append((fid, feature))
        updates = []
        for gid, feature in current.items():
            changes = _changes(baseline.get(gid), feature)
            if changes:
                updates.append((gid, changes, feature))
        deletes = sorted(set(baseline) - set(current))
        total = len(creates) + len(updates) + len(deletes)
        # The feature API can't drop a column, and sending nulls would wipe
        # the column on every feature, so a removed field is only reported.
        removed = set().union(
            *(feature["properties"] for feature in baseline.values())
        ) - set(layer.fields().names())
        unsynced = (
            tr(
                "Removed fields aren't synchronized, and GeoLens keeps their "
                "values: {fields}."
            ).format(fields=", ".join(sorted(removed)))
            if removed
            else ""
        )
        if not total:
            if unsynced:
                self._message(unsynced, Qgis.MessageLevel.Warning)
            else:
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
        table_id = str(layer.customProperty("geolens/table_id", "") or "")
        errors = []
        # Creates carry no table id, so they go last, after a guarded write
        # or a fresh read has shown the table is still the one loaded.
        confirmed = False
        for gid, changes, feature in updates:
            if "geometry" in changes and changes["geometry"] is None:
                errors.append(
                    tr(
                        "Feature {gid} lost its geometry, which GeoLens can't "
                        "store. Undo that edit or delete the feature."
                    ).format(gid=gid)
                )
                continue
            try:
                self.client.patch_feature(dataset_id, gid, changes, table_id)
                baseline[gid] = feature
                confirmed = True
            except GeoLensError as error:
                if _dataset_replaced(error):
                    self._stop_replaced_sync(layer, editing)
                    return
                errors.append(str(error))
        for gid in deletes:
            try:
                self.client.delete_feature(dataset_id, gid, table_id)
                baseline.pop(gid, None)
                confirmed = True
            except GeoLensError as error:
                if _dataset_replaced(error):
                    self._stop_replaced_sync(layer, editing)
                    return
                errors.append(str(error))
        if creates and table_id and not confirmed:
            gid = min(baseline) if baseline else None
            current = _current_table_id(self.client, dataset_id, gid)
            if current is None:
                # One message per refused create keeps the failure count right.
                errors += [
                    tr(
                        "GeoLens couldn't confirm that the dataset is still the "
                        "one this layer was loaded from, so new features weren't "
                        "sent. Try again, or load the dataset again with Add "
                        "features."
                    )
                ] * len(creates)
                creates = []
            elif current != table_id:
                self._stop_replaced_sync(layer, editing)
                return
        created = []
        unaddressable = 0
        for fid, feature in creates:
            try:
                gid, created_in = self.client.create_feature(dataset_id, feature)
            except GeoLensError as error:
                errors.append(str(error))
                continue
            if created_in and table_id and created_in != table_id:
                self._stop_replaced_sync(layer, editing, created_one=True)
                return
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
        if unaddressable:
            # Features live on the server that this layer cannot match again.
            self.baselines.pop(layer.id(), None)
            self._update_actions()
        if errors:
            self._message(
                f"Synchronized {total - len(errors)}/{total}; {len(errors)} failed. {errors[0]}",
                Qgis.MessageLevel.Warning,
            )
        elif unsynced:
            self._message(
                f"Synchronized {total} change(s) to GeoLens. {unsynced}",
                Qgis.MessageLevel.Warning,
            )
        else:
            self._message(f"Synchronized {total} change(s) to GeoLens.")

    def _start_sync_baseline(self, client, layer, dataset_id, before):
        """Record what later edits are diffed against; return why sync is refused.

        QGIS rewrites some values it loads (a datetime gains milliseconds), so
        edits are compared with its export of the layer, not the server's JSON.
        `before` is `_table_id_probe` from just before the load; the table id
        read now must match it, or the layer's features may come from two
        tables. An empty id means the server reports none, and writes then go
        unguarded. Returns "" when sync is enabled.
        """
        baseline = {
            gid: feature
            for _, gid, feature in _exported_features(layer)
            if isinstance(gid, int)
        }
        self.baselines[layer.id()] = baseline
        probe_gid, table_before = before
        gid = min(baseline) if baseline else probe_gid
        table_after = "" if gid is None else _current_table_id(client, dataset_id, gid)
        if table_before is None or table_after is None:
            refusal = tr(
                "GeoLens couldn't confirm which version of the dataset was "
                "loaded, so synchronization is disabled for this layer. Add it "
                "again to edit it."
            )
        elif table_before != table_after:
            refusal = tr(
                "The dataset was replaced on the server while it was loading, "
                "so synchronization is disabled for this layer. Add it again "
                "to edit it."
            )
        else:
            layer.setCustomProperty("geolens/table_id", table_after)
            return ""
        self.baselines.pop(layer.id(), None)
        return refusal

    def _stop_replaced_sync(self, layer, editing, created_one=False):
        # Feature ids may now name other features, so nothing may be written
        # from this layer again.
        self.baselines.pop(layer.id(), None)
        self._update_actions()
        if editing:
            layer.startEditing()
        if created_one:
            text = tr(
                "This dataset was replaced on the GeoLens server after the "
                "layer was loaded. One new feature was added to the replacement "
                "before that showed, and nothing more was sent. Load the dataset "
                "again with Add features, check that feature, and redo your "
                "other edits there. This layer keeps them for reference."
            )
        else:
            text = tr(
                "This dataset was replaced on the GeoLens server after the layer "
                "was loaded, so your edits weren't sent. Load the dataset again "
                "with Add features and redo your edits there. This layer keeps "
                "them for reference."
            )
        self._message(text, Qgis.MessageLevel.Critical)

    def cleanup(self):
        """Stop following the active layer and tile refresh timers, and delete
        downloaded feature files.

        Called by the plugin on unload; hiding the dock deliberately keeps
        everything alive so a reopened dock resumes where it left off.
        """
        self.iface.currentLayerChanged.disconnect(self._update_actions)
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


def _exported_features(layer):
    """Yield each feature's QGIS id, server gid (None when new) and GeoJSON."""
    exporter = QgsJsonExporter(layer)
    exporter.setIncludeGeometry(True)
    for qfeature in layer.getFeatures():
        feature = json.loads(exporter.exportFeature(qfeature))
        gid = (feature.get("properties") or {}).get("_geolens_gid")
        yield qfeature.id(), gid, _clean_feature(feature)


def _changes(previous, feature):
    """The PATCH body that turns `previous` into `feature`; empty when equal."""
    previous = previous or {}
    before = previous.get("properties") or {}
    changes = {}
    properties = {
        key: value
        for key, value in feature["properties"].items()
        if key not in before or before[key] != value
    }
    if properties:
        changes["properties"] = properties
    if previous.get("geometry") != feature.get("geometry"):
        changes["geometry"] = feature.get("geometry")
    return changes


def _table_id_probe(client, dataset_id):
    """(a gid, the table id) read before a load, to bracket it.

    The gid is None for an empty dataset; the table id is None when it
    couldn't be read.
    """
    try:
        first = client.features(dataset_id, 1)["features"]
    except (GeoLensError, ValueError):
        return None, None
    if not first:
        return None, ""
    props = first[0].get("properties")
    gid = first[0].get("id", props.get("gid") if isinstance(props, dict) else None)
    if not isinstance(gid, int) or isinstance(gid, bool):
        return None, ""
    return gid, _current_table_id(client, dataset_id, gid)


def _current_table_id(client, dataset_id, gid):
    """The dataset's table id, read through feature `gid`.

    "" when the server reports none; None when there is no gid or the read
    fails.
    """
    if gid is None:
        return None
    try:
        return client.feature_table_id(dataset_id, gid)
    except (GeoLensError, ValueError):
        return None


def _dataset_replaced(error):
    return error.status == 409 and error.code == "dataset_replaced"
