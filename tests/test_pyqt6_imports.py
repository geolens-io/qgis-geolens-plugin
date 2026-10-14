"""Verify every plugin module imports under the QGIS PyQt6 shim."""

import importlib
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "geolens_qgis"


def module_names():
    for path in sorted(PLUGIN_ROOT.rglob("*.py")):
        relative = path.relative_to(PLUGIN_ROOT.parent).with_suffix("")
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        yield ".".join(parts)


@pytest.mark.parametrize("module_name", list(module_names()))
def test_module_imports_under_pyqt6(module_name):
    importlib.import_module(module_name)
