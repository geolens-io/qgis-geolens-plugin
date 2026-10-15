"""Main QGIS plugin lifecycle and menu integration."""

from __future__ import annotations

import configparser
import os

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QCoreApplication, QLocale, Qt, QTranslator
from qgis.PyQt.QtGui import QGuiApplication, QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from .i18n import tr

DOCS_URL = "https://docs.getgeolens.com/guides/api/qgis/"
REPOSITORY_URL = "https://github.com/geolens-io/qgis-geolens-plugin"
ISSUES_URL = f"{REPOSITORY_URL}/issues"


def user_language():
    """Language code of the QGIS interface locale, e.g. "es" for "es_ES".

    Traditional Chinese locales return "" because only Simplified is translated.
    """
    settings = QgsSettings()
    locale = ""
    if settings.value("locale/overrideFlag", False, type=bool):
        locale = settings.value("locale/userLocale", "", type=str)
    locale = locale or QLocale().name()
    language, *subtags = locale.replace("-", "_").lower().split("_")
    if language == "zh" and {"tw", "hk", "mo", "hant"} & set(subtags):
        return ""
    return language


def uses_dark_palette():
    window = QGuiApplication.palette().window().color()
    return window.lightness() < 128


class GeoLensPlugin:
    """Register GeoLens actions and manage its dock widget."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.toolbar = None
        self.dock = None
        self.translator = None

    def _install_translator(self):
        path = os.path.join(self.plugin_dir, "i18n", f"geolens_{user_language()}.qm")
        if not os.path.isfile(path):
            return
        translator = QTranslator()
        if translator.load(path):
            QCoreApplication.installTranslator(translator)
            self.translator = translator

    def _icon(self, name):
        """Brand icon in the variant that suits the current light or dark palette."""
        suffix = "-dark" if uses_dark_palette() else ""
        return QIcon(os.path.join(self.plugin_dir, "icons", f"{name}{suffix}.svg"))

    def version(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(os.path.join(self.plugin_dir, "metadata.txt"), encoding="utf-8")
        return parser.get("general", "version", fallback="")

    def initGui(self):
        self._install_translator()
        self.toolbar = self.iface.addToolBar("GeoLens")
        self.toolbar.setObjectName("GeoLensToolbar")
        self._add_action(
            "toolbar",
            tr("GeoLens Catalog"),
            self.toggle_dock,
            tr("Browse datasets hosted by GeoLens"),
            toolbar=True,
        )
        self._add_action(
            "toolbar",
            tr("About GeoLens"),
            self.show_about,
            tr("About the GeoLens QGIS plugin"),
        )

    def _add_action(self, icon_name, text, callback, status_tip, toolbar=False):
        action = QAction(self._icon(icon_name), text, self.iface.mainWindow())
        action.setStatusTip(status_tip)
        action.setToolTip(status_tip)
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
        if self.translator:
            QCoreApplication.removeTranslator(self.translator)
            self.translator = None

    def toggle_dock(self):
        if self.dock is None:
            from .dialogs import GeoLensDock

            self.dock = GeoLensDock(self.iface)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        elif self.dock.isVisible():
            self.dock.hide()
            return
        self.dock.show()
        self.dock.raise_()

    def about_box(self):
        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle(tr("About GeoLens"))
        box.setIconPixmap(self._icon("icon").pixmap(64, 64))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "<h3>{title}</h3><p>{version}</p><p>{description}</p>"
            '<p><a href="{docs}">{docs_text}</a><br>'
            '<a href="{repo}">{repo_text}</a><br>'
            '<a href="{issues}">{issues_text}</a></p>'.format(
                title=tr("GeoLens for QGIS"),
                version=tr("Version {version}").format(version=self.version()),
                description=tr(
                    "Browse, visualize, and edit datasets hosted by a GeoLens server."
                ),
                docs=DOCS_URL,
                docs_text=tr("Documentation"),
                repo=REPOSITORY_URL,
                repo_text=tr("Source code"),
                issues=ISSUES_URL,
                issues_text=tr("Report an issue"),
            )
        )
        return box

    def show_about(self):
        self.about_box().exec()
