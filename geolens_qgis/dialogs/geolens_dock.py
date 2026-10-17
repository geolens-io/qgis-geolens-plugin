"""QGIS user interface and layer integration for GeoLens."""

from __future__ import annotations

import base64
import contextlib
import html
import json
import os
import re
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone

from qgis.PyQt.QtCore import (
    QEvent,
    QLocale,
    QPoint,
    QRectF,
    Qt,
    QSettings,
    QTimer,
    QUrl,
)
from qgis.PyQt.QtGui import (
    QCursor,
    QDesktopServices,
    QImage,
    QPainter,
    QPalette,
    QTextOption,
)
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDataSourceUri,
    QgsJsonExporter,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsTask,
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
from ..layer_info import build_metadata, fetch_detail
from .connection_dialog import ConnectionDialog

ROLE_DATASET = Qt.ItemDataRole.UserRole
SAMPLE_SERVERS = (
    ("GeoLens demo", "https://demo.getgeolens.com"),
    ("GeoLibre datasets", "https://datasets.geolibre.app"),
)
HELP_URL = "https://docs.getgeolens.com/guides/api/qgis/"
INSTALL_URL = "https://docs.getgeolens.com/guides/quickstart/install/"
PILOT_URL = "https://getgeolens.com/pilot/"
SETTING_VIEW_ONLY = "geolens/view_only"
MAX_THUMBNAIL_BYTES = 1_000_000
LOG_TAG = "GeoLens"

_HTTP_ERROR = re.compile(r"GeoLens request failed \(HTTP (\d+)\)(?::.*)?", re.DOTALL)
_UNREACHABLE = re.compile(r"Could not reach GeoLens:.*", re.DOTALL)
_UNREADABLE = re.compile(r"GeoLens [a-z ]*(?:malformed|returned no|contained no)[^.]*")


def _instant(text):
    """`text` as an aware UTC-comparable datetime, or None when it isn't a date.

    Python 3.10's `fromisoformat` rejects `Z` and fractions that aren't 3 or 6
    digits, so those are normalized first. A date with no offset counts as UTC.
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    text = re.sub(
        r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1
    )
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _http_failure(code):
    if code in (401, 403):
        return tr(
            "The server refused the request. Check your API key or authentication."
        )
    if code == 404:
        return tr("The server couldn't find that. It may have been removed.")
    if code == 429:
        return tr("The server is busy. Try again in a moment.")
    if code >= 500:
        return tr("The server ran into a problem. Try again later.")
    return tr("The server couldn't complete the request.")


def plain_error(text):
    """`text` with transport and HTTP failure details replaced by plain language."""
    text = _HTTP_ERROR.sub(lambda match: _http_failure(int(match.group(1))), text)
    text = _UNREACHABLE.sub(
        tr("Couldn't reach the server. Check the address and your connection."), text
    )
    return _UNREADABLE.sub(
        tr(
            "The server sent a response GeoLens couldn't read. It may run an "
            "incompatible version."
        ),
        text,
    )


def count_text(count, one, many, **fields):
    """`one` or `many` (translated, with a {count} field) for a count."""
    return (one if count == 1 else many).format(
        count=QLocale().toString(count), **fields
    )


def _datasets_text(count):
    if count == 0:
        return tr("No datasets")
    return count_text(count, tr("{count} dataset"), tr("{count} datasets"))


def _kind(dataset):
    """The theme icon file and the plain name for a dataset's type."""
    if dataset.is_raster:
        return "/mIconRaster.svg", tr("Raster")
    if dataset.record_type == "table":
        return "/mIconTableLayer.svg", tr("Table")
    geometry = (dataset.geometry_type or "").upper()
    if "POINT" in geometry:
        return "/mIconPointLayer.svg", tr("Point layer")
    if "LINE" in geometry:
        return "/mIconLineLayer.svg", tr("Line layer")
    if "POLYGON" in geometry:
        return "/mIconPolygonLayer.svg", tr("Polygon layer")
    return "/mIconVectorLayer.svg", tr("Vector layer")


def _size_text(dataset):
    if dataset.is_raster:
        if not dataset.band_count:
            return ""
        return count_text(dataset.band_count, tr("{count} band"), tr("{count} bands"))
    if dataset.feature_count is None:
        return ""
    return count_text(
        dataset.feature_count, tr("{count} feature"), tr("{count} features")
    )


class _ResultsTree(QTreeWidget):
    """A tree that shows `placeholder` in the empty viewport."""

    def __init__(self):
        super().__init__()
        self.placeholder = ""

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.topLevelItemCount() or not self.placeholder:
            return
        painter = QPainter(self.viewport())
        painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
        layout = QTextOption(Qt.AlignmentFlag.AlignCenter)
        layout.setWrapMode(QTextOption.WrapMode.WordWrap)
        painter.drawText(
            QRectF(self.viewport().rect().adjusted(12, 12, -12, -12)),
            self.placeholder,
            layout,
        )
        painter.end()


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
        self._loaded: list[Dataset] = []
        self._query = ""
        self._thumbnails: dict[tuple, bytes] = {}
        self._pending: dict[tuple, QgsTask] = {}
        # Replaced tasks by id until they finish: QGIS calls a task's Python
        # callback only while Python still holds a reference to the task.
        self._dropped: dict[int, QgsTask] = {}
        self._work_client = None
        self._hover_pos = QPoint()
        self._build_ui()
        self._restore_settings()

    def _build_ui(self):
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        intro = QLabel(tr("Browse and add datasets from a GeoLens server."))
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
        self.connect_button.clicked.connect(self._connect_clicked)
        box.addWidget(self.connect_button)
        layout.addWidget(self.connection_box)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(tr("Search the catalog"))
        self.search_edit.setToolTip(
            tr("Type words from a dataset's title or description.")
        )
        self.search_edit.setAccessibleName(tr("Search the catalog"))
        self.search_edit.returnPressed.connect(self.search)
        self.search_button = QPushButton(tr("Search"))
        self.search_button.setToolTip(tr("Search the connected server's catalog."))
        self.search_button.setAccessibleName(tr("Search"))
        self.search_button.clicked.connect(self.search)
        search_row.addWidget(self.search_edit)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        view_row = QHBoxLayout()
        self.type_filter = QComboBox()
        self.type_filter.addItem(tr("All types"), "all")
        self.type_filter.addItem(tr("Vector"), "vector")
        self.type_filter.addItem(tr("Raster"), "raster")
        self.type_filter.setToolTip(tr("Show only vector or only raster datasets."))
        self.type_filter.setAccessibleName(tr("Filter by type"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem(tr("Best match"), "relevance")
        self.sort_combo.addItem(tr("Title A-Z"), "title")
        self.sort_combo.addItem(tr("Newest first"), "newest")
        self.sort_combo.setToolTip(tr("Order the datasets in the list."))
        self.sort_combo.setAccessibleName(tr("Sort datasets"))
        self.type_filter.currentIndexChanged.connect(self._populate)
        self.sort_combo.currentIndexChanged.connect(self._populate)
        view_row.addWidget(self.type_filter)
        view_row.addWidget(self.sort_combo)
        layout.addLayout(view_row)

        self.results = _ResultsTree()
        self.results.setColumnCount(2)
        self.results.setHeaderHidden(True)
        self.results.setRootIsDecorated(False)
        self.results.setUniformRowHeights(True)
        self.results.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.results.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setAccessibleName(tr("Datasets"))
        self.results.setAccessibleDescription(
            tr("Press Enter on a dataset to add it to the map as tiles.")
        )
        self.results.itemSelectionChanged.connect(self._update_actions)
        self.iface.currentLayerChanged.connect(self._update_actions)
        self.results.itemDoubleClicked.connect(self._add_item)
        self.results.installEventFilter(self)
        self.results.viewport().installEventFilter(self)
        layout.addWidget(self.results, 1)

        options = QFormLayout()
        self.limit = QSpinBox()
        self.limit.setRange(1, 1_000_000)
        self.limit.setValue(10_000)
        self.limit.setToolTip(
            tr(
                "The most features Add features downloads. Tiles are not "
                "limited by this."
            )
        )
        self.limit.setAccessibleName(tr("Feature limit"))
        self.view_only = QCheckBox(tr("Only features in the current map extent"))
        self.view_only.setToolTip(
            tr("Add features downloads only what is visible on the map canvas.")
        )
        self.view_only.setAccessibleName(tr("Only features in the current map extent"))
        self.view_only.setChecked(QSettings().value(SETTING_VIEW_ONLY, True, bool))
        self.view_only.toggled.connect(
            lambda value: QSettings().setValue(SETTING_VIEW_ONLY, value)
        )
        options.addRow(tr("Feature limit"), self.limit)
        options.addRow(self.view_only)
        layout.addLayout(options)

        first_actions = QHBoxLayout()
        self.tiles_button = QPushButton(tr("Add tiles"))
        self.features_button = QPushButton(tr("Add features"))
        self.metadata_button = QPushButton(tr("Metadata"))
        self.tiles_button.setToolTip(
            tr("Add the selected dataset to the map as a tile layer.")
        )
        self.features_button.setToolTip(
            tr("Download the selected vector dataset's features as an editable layer.")
        )
        self.metadata_button.setToolTip(
            tr("Open the selected dataset's page on the server in your browser.")
        )
        for button in (self.tiles_button, self.features_button, self.metadata_button):
            button.setAccessibleName(button.text())
        self.tiles_button.clicked.connect(lambda *_: self._add(self.add_tiles))
        self.features_button.clicked.connect(lambda *_: self._add(self.add_features))
        self.metadata_button.clicked.connect(self.open_metadata)
        first_actions.addWidget(self.tiles_button)
        first_actions.addWidget(self.features_button)
        first_actions.addWidget(self.metadata_button)
        layout.addLayout(first_actions)

        self.sync_button = QPushButton(tr("Sync selected GeoLens layer edits"))
        self.sync_button.setToolTip(
            tr(
                "Send edits to the selected layer back to GeoLens. Works for "
                "feature layers added with Add features in this QGIS session."
            )
        )
        self.sync_button.setAccessibleName(tr("Sync selected GeoLens layer edits"))
        self.sync_button.clicked.connect(self.sync_active_layer)
        layout.addWidget(self.sync_button)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName(tr("Status"))
        layout.addWidget(self.status)

        footer = QHBoxLayout()
        self.help_button = QPushButton(tr("Help"))
        self.help_button.setToolTip(tr("Open the GeoLens documentation."))
        self.help_button.setAccessibleName(tr("Help"))
        self.help_button.clicked.connect(lambda *_: self._open_url(HELP_URL))
        self.no_server_button = QPushButton(tr("No server yet?"))
        self.no_server_button.setToolTip(
            tr("Find out how to run your own GeoLens server or join the hosted pilot.")
        )
        self.no_server_button.setAccessibleName(tr("No server yet?"))
        menu = QMenu(self.no_server_button)
        menu.addAction(tr("Install GeoLens yourself")).triggered.connect(
            lambda *_: self._open_url(INSTALL_URL)
        )
        menu.addAction(tr("Join the hosted pilot")).triggered.connect(
            lambda *_: self._open_url(PILOT_URL)
        )
        self.no_server_button.setMenu(menu)
        for button in (self.help_button, self.no_server_button):
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        footer.addWidget(self.help_button)
        footer.addStretch(1)
        footer.addWidget(self.no_server_button)
        layout.addLayout(footer)
        self.setWidget(root)
        self._update_actions()
        self._populate()

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
        # A connect still running was asked for the connection being left.
        self._drop(("connect",))
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
        for widget in (self.edit_button, self.remove_button):
            widget.setEnabled(connection is not None)
        self.connection_box.setTitle(
            tr("Connection: {name}").format(name=connection.name)
            if connection
            else tr("Connection")
        )
        self._update_actions()

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
        running = {key[0] for key in self._pending}
        adding = self._adding()
        self.connect_button.setEnabled(
            self.connection_combo.currentIndex() >= 0 and "connect" not in running
        )
        # A connect uses the key and save choice it started with.
        for widget in (self.key_edit, self.save_key):
            widget.setEnabled("connect" not in running)
        self.search_button.setEnabled(not running & {"connect", "search"})
        self.tiles_button.setEnabled(connected and dataset is not None and not adding)
        self.features_button.setEnabled(
            connected and dataset is not None and not dataset.is_raster and not adding
        )
        self.metadata_button.setEnabled(connected and dataset is not None)
        self.type_filter.setEnabled(connected)
        self.sort_combo.setEnabled(connected)
        layer = self.iface.activeLayer()
        # Only layers with an in-session baseline can be diffed against the server.
        self.sync_button.setEnabled(
            bool(
                connected
                and self.editing_enabled
                and layer
                and layer.id() in self.baselines
                and "sync" not in running
            )
        )

    def _adding(self):
        """Whether an add started from the panel's buttons is still running."""
        return any(
            key[0] in ("tiles", "features") and key[1] is self.client
            for key in self._pending
        )

    def _message(self, text, level=Qgis.MessageLevel.Info):
        """Show the result of an action; the status line shows the panel's state."""
        shown = plain_error(text)
        if shown != text:
            QgsMessageLog.logMessage(text, LOG_TAG, Qgis.MessageLevel.Warning)
        self.iface.messageBar().pushMessage("GeoLens", shown, level=level, duration=6)

    def _report(self, error):
        """Show why a background task failed, logging the traceback of a bug."""
        if not isinstance(error, (GeoLensError, ValueError, OSError)):
            QgsMessageLog.logMessage(
                "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
                LOG_TAG,
                Qgis.MessageLevel.Critical,
            )
        self._message(str(error), Qgis.MessageLevel.Critical)

    def _run(
        self, key, description, work, done, failed=None, quiet=False, follows=False
    ):
        """Run `work()` in a QGIS background task and pass its result to `done`.

        `done(result)`, or `failed(error)` when `work` raises, runs on the GUI
        thread; `failed` defaults to reporting the error. A task started later
        under the same `key` replaces this one. With `follows`, the task is
        also dropped when the dock's client changes. A replaced or dropped
        task's result is ignored, as is every result after `cleanup`. Unless
        `quiet`, the status line shows `description` while the task runs.
        """

        def run(_task):
            # QgsTask hands on_finished nothing at all for a falsy result.
            return (work(),)

        def finished(exception, value=None):
            self._dropped.pop(id(task), None)
            if self._pending.get(key) is not task:
                return
            del self._pending[key]
            self._update_actions()
            self._refresh_status()
            if exception is None:
                done(value[0])
            else:
                (failed or self._report)(exception)

        # Without CanCancel: a cancel would not stop the request, only lose
        # its result.
        task = QgsTask.fromFunction(
            description, run, on_finished=finished, flags=QgsTask.Flag.Silent
        )
        task.follows_client = follows
        task.status_text = "" if quiet else description
        self._drop(key)
        self._pending[key] = task
        QgsApplication.taskManager().addTask(task)
        self._update_actions()
        self._refresh_status()

    def _drop(self, *keys):
        """Forget the tasks under `keys`; whatever they return is ignored."""
        for key in keys:
            task = self._pending.pop(key, None)
            if task is not None:
                task.cancel()
                self._dropped[id(task)] = task
        self._update_actions()
        self._refresh_status()

    def _refresh_status(self):
        running = [task.status_text for task in self._pending.values()]
        running = [text for text in running if text]
        if running:
            self.status.setText(running[-1])
            return
        if not self.client:
            self.status.setText(tr("Not connected."))
            return
        total = len(self.datasets)
        shown = self.results.topLevelItemCount()
        server = origin_label(self.client.base_url)
        if shown == total:
            text = tr("{datasets} on {server}.").format(
                datasets=_datasets_text(total), server=server
            )
        else:
            text = tr("{shown} of {datasets} shown on {server}.").format(
                shown=QLocale().toString(shown),
                datasets=_datasets_text(total),
                server=server,
            )
        editing = (
            tr("Editing is enabled.")
            if self.editing_enabled
            else tr("Editing is disabled by this server.")
        )
        self.status.setText(f"{text} {editing}")

    def _connect_clicked(self, *_):
        self.connect_server()

    def _add(self, action):
        if self._selected_dataset() and not self._adding():
            action()

    def _add_item(self, item, *_):
        # Tiles suit every dataset type, so a double-click or Enter uses them.
        if self.client and item.data(0, ROLE_DATASET):
            self.results.setCurrentItem(item)
            self._add(self.add_tiles)

    def _open_url(self, url):
        QDesktopServices.openUrl(QUrl(url))

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
        except (ValueError, GeoLensError) as error:
            self._connect_failed(error)
            return
        save = key if self.save_key.isChecked() else ""
        self._run(
            ("connect",),
            tr("Connecting…"),
            connection.connect,
            lambda datasets: self._connected(connection, saved, save, datasets),
            failed=self._connect_failed,
        )

    def _connect_failed(self, error):
        self._disconnect()
        self.connection_box.setCollapsed(False)
        self._report(error)

    def _connected(self, connection, saved, save_key, datasets):
        self.connection = connection
        self._connected_to = saved
        self.client = connection.client
        self.editing_enabled = connection.capabilities["dataset_editing"]
        if save_key:
            self._save_key(save_key, saved)
        self._show_datasets(datasets)
        if self.search_edit.text().strip():
            # The box still holds a query, so the list has to answer it.
            self.search()
        self.connection_box.setCollapsed(True)
        self._message(
            tr("Connected to {server}.").format(
                server=origin_label(connection.base_url)
            ),
            Qgis.MessageLevel.Success,
        )

    def _disconnect(self):
        """Forget the current connection and the catalog it returned."""
        self._drop(("connect",))
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
            self._connect_clicked()
            return
        query = self.search_edit.text()
        client = self.client
        self._run(
            ("search",),
            tr("Searching…"),
            lambda: client.search(query, 50),
            lambda datasets: self._show_datasets(datasets, query),
            follows=True,
        )

    def _show_datasets(self, datasets, query=""):
        self._forget_work_for_other_client()
        self._loaded = list(datasets)
        self._query = query.strip()
        self.datasets = {dataset.id: dataset for dataset in datasets}
        dated = any(_instant(dataset.updated) for dataset in datasets)
        newest = self.sort_combo.model().item(self.sort_combo.findData("newest"))
        newest.setEnabled(dated)
        if not dated and self.sort_combo.currentData() == "newest":
            self.sort_combo.setCurrentIndex(0)
        self._populate()

    def _forget_work_for_other_client(self):
        """Drop previews and tasks started under a client since replaced.

        The same server may answer differently to other credentials.
        """
        if self.client is self._work_client:
            return
        self._work_client = self.client
        self._thumbnails.clear()
        self._drop(*[key for key, task in self._pending.items() if task.follows_client])

    def _populate(self, *_):
        """Fill the list from the loaded datasets, filtered and sorted here."""
        keep = self._selected_dataset()
        kind = self.type_filter.currentData()
        rows = [
            dataset
            for dataset in self._loaded
            if kind == "all" or dataset.is_raster == (kind == "raster")
        ]
        order = self.sort_combo.currentData()
        if order == "title":
            rows.sort(key=lambda dataset: dataset.title.casefold())
        elif order == "newest":
            dated = [d for d in rows if _instant(d.updated)]
            undated = [d for d in rows if not _instant(d.updated)]
            dated.sort(key=lambda d: _instant(d.updated), reverse=True)
            rows = dated + undated
        self.results.clear()
        for dataset in rows:
            item = self._row(dataset)
            self.results.addTopLevelItem(item)
            if keep and dataset.id == keep.id:
                self.results.setCurrentItem(item)
        self.results.placeholder = self._placeholder()
        self.results.viewport().update()
        self._refresh_status()
        self._update_actions()

    def _placeholder(self):
        if not self.client:
            return tr("Connect to a GeoLens server to browse its datasets.")
        if self._loaded:
            return tr("No datasets match this filter.")
        if self._query:
            return tr('No datasets found for "{query}". Try different words.').format(
                query=self._query
            )
        return tr("This server has no datasets you can see yet.")

    def _row(self, dataset):
        icon, kind = _kind(dataset)
        size = _size_text(dataset)
        item = QTreeWidgetItem([dataset.title, size])
        item.setIcon(0, QgsApplication.getThemeIcon(icon))
        item.setData(0, ROLE_DATASET, dataset)
        item.setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        item.setForeground(
            1, self.results.palette().color(QPalette.ColorRole.PlaceholderText)
        )
        label = ", ".join(part for part in (dataset.title, kind, size) if part)
        for column in (0, 1):
            item.setToolTip(column, self._tooltip(dataset, kind))
            item.setData(column, Qt.ItemDataRole.AccessibleTextRole, label)
        return item

    def _tooltip(self, dataset, kind, thumbnail=None):
        parts = [f"<b>{html.escape(dataset.title)}</b> ({html.escape(kind)})"]
        if dataset.description and dataset.description != dataset.title:
            parts.append(html.escape(dataset.description[:300]))
        if thumbnail:
            encoded = base64.b64encode(thumbnail).decode("ascii")
            parts.append(f'<img src="data:image/png;base64,{encoded}">')
        parts.append(
            "<i>"
            + html.escape(tr("Double-click or press Enter to add it as tiles."))
            + "</i>"
        )
        return "<p>" + "</p><p>".join(parts) + "</p>"

    def _thumbnail(self, dataset):
        """The cached preview image, starting one background fetch when absent."""
        if not (self.client and dataset.has_quicklook):
            return None
        key = (self.client.base_url, dataset.id, dataset.quicklook_version)
        if key not in self._thumbnails and ("preview", *key) not in self._pending:
            self._fetch_thumbnail(key, dataset, self.client)
        return self._thumbnails.get(key)

    def _fetch_thumbnail(self, key, dataset, client):
        def done(data):
            if (
                data
                and len(data) <= MAX_THUMBNAIL_BYTES
                and not QImage.fromData(data).isNull()
            ):
                self._thumbnails[key] = data
                self._show_thumbnail(key)

        self._run(
            ("preview", *key),
            tr("Loading a dataset preview"),
            lambda: client.quicklook(dataset.id, dataset.quicklook_version),
            done,
            failed=lambda error: QgsMessageLog.logMessage(
                str(error), LOG_TAG, Qgis.MessageLevel.Info
            ),
            quiet=True,
            follows=True,
        )

    def _hovered_dataset_id(self):
        viewport = self.results.viewport()
        item = self.results.itemAt(viewport.mapFromGlobal(QCursor.pos()))
        dataset = item.data(0, ROLE_DATASET) if item else None
        return dataset.id if dataset else None

    def _show_thumbnail(self, key):
        """Show a finished preview if it still matches the row under the pointer.

        The row may have been refreshed since the fetch began, so the text and
        the version come from the current record, not the one captured then.
        """
        current = self.datasets.get(key[1])
        if (
            not self.client
            or key[0] != self.client.base_url
            or current is None
            or not current.has_quicklook
            or current.quicklook_version != key[2]
            or self._hovered_dataset_id() != current.id
        ):
            return
        QToolTip.showText(
            self._hover_pos,
            self._tooltip(current, _kind(current)[1], self._thumbnails[key]),
            self.results.viewport(),
        )

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self.results.viewport() and kind == QEvent.Type.ToolTip:
            item = self.results.itemAt(event.pos())
            dataset = item.data(0, ROLE_DATASET) if item else None
            if dataset:
                self._hover_pos = event.globalPos()
                text = self._tooltip(
                    dataset, _kind(dataset)[1], self._thumbnail(dataset)
                )
                QToolTip.showText(event.globalPos(), text, self.results.viewport())
            else:
                QToolTip.hideText()
            return True
        if watched is self.results and kind == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.results.currentItem()
                if item:
                    self._add_item(item)
                return True
        return super().eventFilter(watched, event)

    def add_tiles(self):
        dataset = self._selected_dataset()
        if self.client and dataset:
            self.add_dataset_tiles(self.client, dataset)

    def add_dataset_tiles(self, client, dataset):
        """Add `dataset` as a tile layer served, and kept renewed, by `client`."""

        def done(result):
            source, detail = result
            try:
                if source.get("kind") == "raster":
                    self._add_raster(client, dataset, source, detail)
                else:
                    self._add_vector_tiles(client, dataset, source, detail)
            except GeoLensError as error:
                self._message(str(error), Qgis.MessageLevel.Critical)

        self._run(
            ("tiles", client, dataset.id),
            tr("Adding {title}…").format(title=dataset.title),
            lambda: (client.tile_source(dataset.id), fetch_detail(client, dataset.id)),
            done,
            follows=client is self.client,
        )

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

    def _tag_layer(self, layer, dataset, client, detail):
        """Record where the layer came from and describe it with `detail`."""
        page = client.metadata_url(dataset.id)
        layer.setCustomProperty("geolens/server", client.base_url)
        layer.setCustomProperty("geolens/dataset_id", dataset.id)
        layer.setCustomProperty("geolens/metadata_url", page)
        layer.setMetadata(build_metadata(dataset, detail, page))

    def _add_raster(self, client, dataset, source, detail):
        layer = QgsRasterLayer(self._uri(source), dataset.title, "wms")
        if not layer.isValid():
            raise GeoLensError(
                tr("QGIS could not create the GeoLens raster tile layer")
            )
        self._tag_layer(layer, dataset, client, detail)
        QgsProject.instance().addMapLayer(layer)
        self._schedule_refresh(layer, dataset, source, "wms", client)
        self._zoom_to(dataset)
        self._message(tr("Added raster tiles: {title}").format(title=dataset.title))

    def _add_vector_tiles(self, client, dataset, source, detail):
        layer = QgsVectorTileLayer(self._uri(source), dataset.title)
        if not layer.isValid():
            raise GeoLensError(
                tr("QGIS could not create the GeoLens vector tile layer")
            )
        self._tag_layer(layer, dataset, client, detail)
        layer.setCustomProperty("geolens/source_layer", source.get("source_layer", ""))
        QgsProject.instance().addMapLayer(layer)
        self._schedule_refresh(layer, dataset, source, "vectortile", client)
        self._zoom_to(dataset)
        self._message(tr("Added vector tiles: {title}").format(title=dataset.title))

    def _schedule_refresh(self, layer, dataset, source, provider, client=None):
        # The dock can reconnect to another server while this layer is open;
        # renewals must keep asking the server that issued the layer.
        client = client or self.client
        seconds = refresh_delay_seconds(source.get("expires_in"))
        layer_id = layer.id()
        timer = QTimer(self)
        timer.setSingleShot(True)

        def renewed(renewal):
            # Removing a layer deletes the object behind `layer`, so look it
            # up again instead of touching the stale wrapper.
            live = QgsProject.instance().mapLayer(layer_id)
            if not live:
                self._drop_refresh_timer(layer_id)
                return
            live.setDataSource(self._uri(renewal), live.name(), provider)
            self._schedule_refresh(live, dataset, renewal, provider, client)

        def retry(_error):
            if self.refresh_timers.get(layer_id) is timer:
                timer.start(60_000)

        def refresh():
            if not QgsProject.instance().mapLayer(layer_id):
                self._drop_refresh_timer(layer_id)
                return
            self._run(
                ("renew", layer_id),
                tr("Renewing tile access: {title}").format(title=dataset.title),
                lambda: client.tile_source(dataset.id),
                renewed,
                failed=retry,
                quiet=True,
            )

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
        bbox = self._current_bbox() if self.view_only.isChecked() else None
        limit = self.limit.value()
        self._run(
            ("features", client, dataset.id),
            tr("Adding {title}…").format(title=dataset.title),
            lambda: _download_features(client, dataset.id, limit, bbox),
            lambda download: self._add_feature_layer(client, dataset, limit, download),
            follows=client is self.client,
        )

    def _add_feature_layer(self, client, dataset, limit, download):
        collection, unidentified, table_before, table_after, detail = download
        try:
            handle, path = tempfile.mkstemp(prefix="geolens_", suffix=".geojson")
            os.close(handle)
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(collection, stream)
            self.temp_files.append(path)
            layer = QgsVectorLayer(path, dataset.title, "ogr")
            if not layer.isValid():
                raise GeoLensError(
                    tr("QGIS could not create the GeoLens feature layer")
                )
            self._tag_layer(layer, dataset, client, detail)
            # Synchronization matches features by integer server ID. A dataset
            # that uses string or UUID identifiers would look entirely new and
            # be duplicated onto the server, so it is loaded read-only instead.
            syncable = unidentified == 0
            refusal = ""
            if syncable:
                refusal = self._start_sync_baseline(layer, table_before, table_after)
                syncable = not refusal
            layer.setCustomProperty("geolens/editable", syncable)
            QgsProject.instance().addMapLayer(layer)
            self._zoom_to(dataset)
            QSettings().setValue("geolens/feature_limit", limit)
            if syncable:
                self._message(
                    count_text(
                        layer.featureCount(),
                        tr("Added {count} editable feature: {title}"),
                        tr("Added {count} editable features: {title}"),
                        title=dataset.title,
                    )
                )
            elif refusal:
                self._message(
                    tr("Added {count} features: {title}. {reason}").format(
                        count=QLocale().toString(layer.featureCount()),
                        title=dataset.title,
                        reason=refusal,
                    ),
                    Qgis.MessageLevel.Warning,
                )
            else:
                reason = count_text(
                    unidentified,
                    tr(
                        "{count} feature has no integer server ID, so "
                        "synchronization is disabled for this layer."
                    ),
                    tr(
                        "{count} features have no integer server ID, so "
                        "synchronization is disabled for this layer."
                    ),
                )
                self._message(
                    tr("Added {count} features: {title}. {reason}").format(
                        count=QLocale().toString(layer.featureCount()),
                        title=dataset.title,
                        reason=reason,
                    ),
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
            self._open_url(self.client.metadata_url(dataset.id))

    def sync_active_layer(self):
        if any(key[0] == "sync" for key in self._pending):
            return
        layer = self.iface.activeLayer()
        if not self.client or not isinstance(layer, QgsVectorLayer):
            self._message(
                tr("Select an editable feature layer loaded from GeoLens."),
                Qgis.MessageLevel.Warning,
            )
            return
        dataset_id = layer.customProperty("geolens/dataset_id", "")
        baseline = self.baselines.get(layer.id())
        if not dataset_id or baseline is None:
            self._message(
                tr(
                    "The selected layer has no GeoLens edit baseline in this "
                    "session. Reload it with Add features; datasets without "
                    "integer feature IDs cannot be synchronized."
                ),
                Qgis.MessageLevel.Warning,
            )
            return
        origin = str(layer.customProperty("geolens/server", "") or "")
        if not same_server(origin, self.client.base_url):
            # The dataset id and the baseline belong to the server that
            # issued the layer, so its edits go to no other server.
            self._message(
                (
                    tr(
                        "This layer came from {origin}. Connect to that server "
                        "to synchronize its edits."
                    ).format(origin=origin)
                    if origin
                    else tr(
                        "This layer doesn't record which GeoLens server it came "
                        "from, so its edits can't be synchronized."
                    )
                ),
                Qgis.MessageLevel.Warning,
            )
            return
        editing = layer.isEditable()
        if editing and not layer.commitChanges():
            self._message(
                tr("Commit the QGIS layer edits before synchronizing."),
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
                self._message(tr("No local changes to synchronize."))
            return
        if (
            deletes
            and QMessageBox.question(
                self,
                tr("Delete GeoLens features"),
                count_text(
                    len(deletes),
                    tr("Synchronizing will delete {count} server feature. Continue?"),
                    tr("Synchronizing will delete {count} server features. Continue?"),
                ),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        table_id = str(layer.customProperty("geolens/table_id", "") or "")
        layer_id = layer.id()
        client = self.client
        gids = set(baseline)
        outcome = _SyncOutcome()

        def finish(_=None, error=None):
            self._synced(layer_id, editing, total, unsynced, outcome, error)

        # Edits made while the writes are in flight would join the edit
        # session that stores the new server IDs.
        layer.setReadOnly(True)
        self._run(
            ("sync", layer_id),
            tr("Synchronizing {title}…").format(title=layer.name()),
            lambda: _send_edits(
                outcome, client, dataset_id, table_id, updates, deletes, creates, gids
            ),
            finish,
            failed=lambda error: finish(error=error),
        )

    def _synced(self, layer_id, editing, total, unsynced, outcome, error=None):
        """Apply what `_send_edits` got done to the layer and its baseline.

        `error` is what stopped the writes early, if anything.
        """
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is not None:
            layer.setReadOnly(False)
        if outcome.replaced:
            self._stop_replaced_sync(layer_id, editing, outcome.created_one)
            return
        baseline = self.baselines.get(layer_id, {})
        for gid, feature in outcome.written:
            baseline[gid] = feature
        for gid in outcome.deleted:
            baseline.pop(gid, None)
        for _, gid, feature in outcome.created:
            baseline[gid] = feature
        errors = list(outcome.errors)
        unaddressable = outcome.unaddressable
        created = [(fid, gid) for fid, gid, _ in outcome.created]
        # One edit session for every new ID: each commit rewrites the layer file.
        gid_index = layer.fields().indexOf("_geolens_gid") if layer else -1
        if created and gid_index >= 0:
            layer.startEditing()
            for fid, gid in created:
                layer.changeAttributeValue(fid, gid_index, gid)
            if not layer.commitChanges():
                unaddressable += len(created)
                errors.append(
                    count_text(
                        len(created),
                        tr(
                            "{count} feature was created, but its server ID "
                            "could not be stored locally. Reload the layer "
                            "before synchronizing again."
                        ),
                        tr(
                            "{count} features were created, but their server "
                            "IDs could not be stored locally. Reload the layer "
                            "before synchronizing again."
                        ),
                    )
                )
        if unaddressable:
            # Features live on the server that this layer cannot match again.
            self.baselines.pop(layer_id, None)
            self._update_actions()
        if error is not None:
            self._report(error)
            return
        if errors:
            self._message(
                tr("Synchronized {done}/{total}; {failed} failed. {error}").format(
                    done=total - len(errors),
                    total=total,
                    failed=len(errors),
                    error=errors[0],
                ),
                Qgis.MessageLevel.Warning,
            )
        elif unsynced:
            self._message(
                count_text(
                    total,
                    tr("Synchronized {count} change to GeoLens. {unsynced}"),
                    tr("Synchronized {count} changes to GeoLens. {unsynced}"),
                    unsynced=unsynced,
                ),
                Qgis.MessageLevel.Warning,
            )
        else:
            self._message(
                count_text(
                    total,
                    tr("Synchronized {count} change to GeoLens."),
                    tr("Synchronized {count} changes to GeoLens."),
                )
            )

    def _start_sync_baseline(self, layer, table_before, table_after):
        """Record what later edits are diffed against; return why sync is refused.

        QGIS rewrites some values it loads (a datetime gains milliseconds), so
        edits are compared with its export of the layer, not the server's JSON.
        The table ids read just before and just after the download must match,
        or the layer's features may come from two tables. An empty id means
        the server reports none, and writes then go unguarded; None means it
        couldn't be read. Returns "" when sync is enabled.
        """
        baseline = {
            gid: feature
            for _, gid, feature in _exported_features(layer)
            if isinstance(gid, int)
        }
        self.baselines[layer.id()] = baseline
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

    def _stop_replaced_sync(self, layer_id, editing, created_one=False):
        # Feature ids may now name other features, so nothing may be written
        # from this layer again.
        self.baselines.pop(layer_id, None)
        self._update_actions()
        layer = QgsProject.instance().mapLayer(layer_id)
        if editing and layer is not None:
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
        """Stop following the active layer, tile refresh timers and background
        tasks, and delete downloaded feature files.

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
        # A task that finishes later finds nothing pending and does nothing,
        # so a layer locked for a sync is unlocked here.
        pending, self._pending = self._pending, {}
        for key, task in pending.items():
            task.cancel()
            self._dropped[id(task)] = task
            layer = QgsProject.instance().mapLayer(key[1]) if key[0] == "sync" else None
            if layer is not None:
                layer.setReadOnly(False)

    def closeEvent(self, event):
        # Hiding retains in-memory API keys, baselines, and tile refresh timers.
        event.ignore()
        self.hide()


@dataclass
class _SyncOutcome:
    """What `_send_edits` got done, for the dock to apply on the GUI thread."""

    written: list[tuple[int, dict]] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    # (QGIS feature id, server gid, feature)
    created: list[tuple[int, int, dict]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unaddressable: int = 0
    replaced: bool = False
    created_one: bool = False


def _send_edits(outcome, client, dataset_id, table_id, updates, deletes, creates, gids):
    """Send a sync's writes, recording in `outcome` what each one did.

    It runs in a background task, so it reads no layer and changes no
    baseline. `gids` are the server IDs in the layer's baseline. Creates
    carry no table id, so they go last, after a guarded write or a fresh
    read has shown the table is still the one loaded.
    """
    confirmed = False
    for gid, changes, feature in updates:
        if "geometry" in changes and changes["geometry"] is None:
            outcome.errors.append(
                tr(
                    "Feature {gid} lost its geometry, which GeoLens can't "
                    "store. Undo that edit or delete the feature."
                ).format(gid=gid)
            )
            continue
        try:
            client.patch_feature(dataset_id, gid, changes, table_id)
        except GeoLensError as error:
            if _dataset_replaced(error):
                outcome.replaced = True
                return outcome
            outcome.errors.append(str(error))
            continue
        outcome.written.append((gid, feature))
        confirmed = True
    for gid in deletes:
        try:
            client.delete_feature(dataset_id, gid, table_id)
        except GeoLensError as error:
            if _dataset_replaced(error):
                outcome.replaced = True
                return outcome
            outcome.errors.append(str(error))
            continue
        outcome.deleted.append(gid)
        confirmed = True
    if creates and table_id and not confirmed:
        current = _current_table_id(client, dataset_id, min(gids) if gids else None)
        if current is None:
            # One message per refused create keeps the failure count right.
            outcome.errors += [
                tr(
                    "GeoLens couldn't confirm that the dataset is still the "
                    "one this layer was loaded from, so new features weren't "
                    "sent. Try again, or load the dataset again with Add "
                    "features."
                )
            ] * len(creates)
            return outcome
        if current != table_id:
            outcome.replaced = True
            return outcome
    for fid, feature in creates:
        try:
            gid, created_in = client.create_feature(dataset_id, feature)
        except GeoLensError as error:
            outcome.errors.append(str(error))
            continue
        if created_in and table_id and created_in != table_id:
            outcome.replaced = outcome.created_one = True
            return outcome
        if gid is None:
            # The feature exists on the server but cannot be addressed, so
            # a second sync would create it again. The dock drops the
            # baseline, and the layer has to be reloaded first.
            outcome.unaddressable += 1
            outcome.errors.append(
                tr(
                    "A feature was created, but GeoLens returned no integer "
                    "ID for it. Reload the layer before synchronizing again."
                )
            )
            continue
        outcome.created.append((fid, gid, feature))
    return outcome


def _download_features(client, dataset_id, limit, bbox):
    """Download a dataset for Add features, between two reads of its table id.

    Returns the collection, with each feature's integer server ID copied to
    `_geolens_gid`; how many features have none; the table ids read before
    and after the download (after is None when sync is off anyway); and the
    detail record.
    """
    probe_gid, table_before = _table_id_probe(client, dataset_id)
    collection = client.features(dataset_id, limit, bbox)
    gids = []
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
            gids.append(gid)
        else:
            unidentified += 1
    table_after = None
    if not unidentified:
        gid = min(gids) if gids else probe_gid
        table_after = "" if gid is None else _current_table_id(client, dataset_id, gid)
    detail = fetch_detail(client, dataset_id)
    return collection, unidentified, table_before, table_after, detail


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
