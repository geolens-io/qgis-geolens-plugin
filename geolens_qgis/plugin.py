"""Main QGIS plugin lifecycle and menu integration."""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox


class GeoLensPlugin:
    """Register GeoLens actions and manage its dock widget."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.toolbar = None
        self.dock = None

    def initGui(self):
        self.toolbar = self.iface.addToolBar("GeoLens")
        self.toolbar.setObjectName("GeoLensToolbar")
        self._add_action(
            "icons/icon.svg",
            "GeoLens Catalog",
            self.toggle_dock,
            "Browse datasets hosted by GeoLens",
            toolbar=True,
        )
        self._add_action(
            "icons/about.svg",
            "About GeoLens",
            self.show_about,
            "About the GeoLens QGIS plugin",
        )

    def _add_action(self, icon_path, text, callback, status_tip, toolbar=False):
        icon = QIcon(os.path.join(self.plugin_dir, icon_path))
        action = QAction(icon, text, self.iface.mainWindow())
        action.setStatusTip(status_tip)
        action.triggered.connect(callback)
        self.iface.addPluginToWebMenu("GeoLens", action)
        if toolbar:
            self.toolbar.addAction(action)
        self.actions.append(action)
        return action

    def unload(self):
        for action in self.actions:
            self.iface.removePluginWebMenu("GeoLens", action)
            self.iface.removeToolBarIcon(action)
        self.actions.clear()
        if self.toolbar:
            self.toolbar.deleteLater()
            self.toolbar = None
        if self.dock:
            self.dock.cleanup()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def toggle_dock(self):
        if self.dock is None:
            from .dialogs import GeoLensDock

            self.dock = GeoLensDock(self.iface)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.setVisible(not self.dock.isVisible())
        if self.dock.isVisible():
            self.dock.raise_()

    def show_about(self):
        QMessageBox.about(
            self.iface.mainWindow(),
            "About GeoLens",
            "<h3>GeoLens for QGIS</h3>"
            "<p>Browse, visualize, and edit datasets hosted by a GeoLens server.</p>"
            '<p><a href="https://github.com/geolens-io/qgis-geolens-plugin">'
            "Source code and documentation</a></p>",
        )
