"""QGIS test setup: a PyQt6 stub unless a real QGIS is importable."""

import pathlib
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    import qgis.core  # noqa: F401

    HAS_REAL_QGIS = True
except ImportError:
    HAS_REAL_QGIS = False

try:
    from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets

    HAS_PYQT6 = True
except ImportError:  # Qt-independent tests still run without PyQt6 installed.
    HAS_PYQT6 = False


class _MessageLevel:
    Info = 0
    Warning = 1
    Critical = 2
    Success = 3


class _Qgis:
    MessageLevel = _MessageLevel


class _Placeholder:
    def __init__(self, *args, **kwargs):
        pass


def _install_qgis_stub():
    """Register a `qgis` package backed by real PyQt6 modules and light stubs."""
    qgis = types.ModuleType("qgis")
    qgis.__path__ = []
    pyqt = types.ModuleType("qgis.PyQt")
    pyqt.__path__ = []
    sys.modules.update({"qgis": qgis, "qgis.PyQt": pyqt})
    qgis.PyQt = pyqt
    for name, module in (
        ("QtCore", QtCore),
        ("QtGui", QtGui),
        ("QtNetwork", QtNetwork),
        ("QtWidgets", QtWidgets),
    ):
        alias = types.ModuleType(f"qgis.PyQt.{name}")
        for attribute in dir(module):
            if not attribute.startswith("_"):
                setattr(alias, attribute, getattr(module, attribute))
        sys.modules[f"qgis.PyQt.{name}"] = alias
        setattr(pyqt, name, alias)
    widgets = sys.modules["qgis.PyQt.QtWidgets"]
    widgets.QAction = QtGui.QAction

    core = types.ModuleType("qgis.core")
    core.Qgis = _Qgis
    for name in (
        "QgsApplication",
        "QgsAuthMethodConfig",
        "QgsBlockingNetworkRequest",
        "QgsCoordinateReferenceSystem",
        "QgsCoordinateTransform",
        "QgsDataSourceUri",
        "QgsExpression",
        "QgsFeatureRequest",
        "QgsFeedback",
        "QgsJsonExporter",
        "QgsProject",
        "QgsRasterLayer",
        "QgsRectangle",
        "QgsRenderContext",
        "QgsSimpleMarkerSymbolLayerBase",
        "QgsVectorLayer",
        "QgsVectorTileLayer",
    ):
        setattr(core, name, _Placeholder)
    sys.modules["qgis.core"] = core
    qgis.core = core

    gui = types.ModuleType("qgis.gui")
    gui.QgsAuthConfigSelect = _Placeholder
    sys.modules["qgis.gui"] = gui
    qgis.gui = gui


if HAS_PYQT6 and not HAS_REAL_QGIS:
    _install_qgis_stub()


@pytest.fixture
def real_qgis():
    """Skip tests that need the QGIS libraries when only the stub is present."""
    if not HAS_REAL_QGIS:
        pytest.skip("requires a real QGIS")
