"""Translation for user-facing strings; the plugin installs the translator."""

from qgis.PyQt.QtCore import QCoreApplication


def tr(text):
    return QCoreApplication.translate("GeoLens", text)
