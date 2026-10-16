"""QGIS test setup: a PyQt6 stub unless a real QGIS is importable."""

import pathlib
import sys
import threading
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
        "QgsBox3D",
        "QgsCoordinateReferenceSystem",
        "QgsCoordinateTransform",
        "QgsDataCollectionItem",
        "QgsDataItem",
        "QgsDataItemProvider",
        "QgsDataSourceUri",
        "QgsErrorItem",
        "QgsExpression",
        "QgsFeatureRequest",
        "QgsFeedback",
        "QgsJsonExporter",
        "QgsLayerMetadata",
        "QgsMimeDataUtils",
        "QgsNetworkAccessManager",
        "QgsNetworkReplyContent",
        "QgsMessageLog",
        "QgsProject",
        "QgsRasterLayer",
        "QgsRectangle",
        "QgsRenderContext",
        "QgsSettings",
        "QgsSimpleMarkerSymbolLayerBase",
        "QgsTask",
        "QgsVectorFileWriter",
        "QgsVectorLayer",
        "QgsVectorLayerFeatureSource",
        "QgsVectorTileLayer",
    ):
        setattr(core, name, _Placeholder)
    sys.modules["qgis.core"] = core
    qgis.core = core

    gui = types.ModuleType("qgis.gui")
    gui.QgsAuthConfigSelect = _Placeholder
    gui.QgsCollapsibleGroupBox = _Placeholder
    gui.QgsCustomDropHandler = _Placeholder
    sys.modules["qgis.gui"] = gui
    qgis.gui = gui


if HAS_PYQT6 and not HAS_REAL_QGIS:
    _install_qgis_stub()


PLUGIN_ROOT = REPO_ROOT / "geolens_qgis"
# The transport layer itself, which tests call directly.
TRANSPORT = {PLUGIN_ROOT / "client.py", PLUGIN_ROOT / "connection.py"}


@pytest.fixture(autouse=True)
def requests_off_the_gui_thread(monkeypatch):
    """Fail a test in which plugin code sends a request from the GUI thread.

    A request waits for the server, which freezes QGIS. Tests may call the
    client and the transport directly; the panel, the Browser and the layer
    code must send theirs from background tasks. `publish/` is exempt.
    """
    from geolens_qgis.client import GeoLensClient

    send = GeoLensClient._request
    blocked = []

    def request(self, path_or_url, *args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            frame = sys._getframe(1)
            while frame is not None:
                path = pathlib.Path(frame.f_code.co_filename).resolve()
                if (
                    path.is_relative_to(PLUGIN_ROOT)
                    and path not in TRANSPORT
                    and not path.is_relative_to(PLUGIN_ROOT / "publish")
                ):
                    blocked.append(f"{path_or_url} from {path.name}")
                    break
                frame = frame.f_back
        return send(self, path_or_url, *args, **kwargs)

    monkeypatch.setattr(GeoLensClient, "_request", request)
    yield
    assert blocked == [], "requests sent from the GUI thread"


@pytest.fixture
def real_qgis():
    """Skip tests that need the QGIS libraries when only the stub is present."""
    if not HAS_REAL_QGIS:
        pytest.skip("requires a real QGIS")
