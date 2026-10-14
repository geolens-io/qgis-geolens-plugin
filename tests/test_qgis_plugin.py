"""Plugin lifecycle against a real QGIS, through pytest-qgis's interface."""

import pytest

pytest.importorskip("pytest_qgis")

from geolens_qgis import classFactory  # noqa: E402


@pytest.fixture
def plugin(real_qgis, qgis_iface):
    instance = classFactory(qgis_iface)
    yield instance
    instance.unload()


def test_loads_registers_actions_and_unloads(plugin, qgis_iface):
    plugin.initGui()
    assert [action.text() for action in plugin.actions] == [
        "GeoLens Catalog",
        "About GeoLens",
    ]
    plugin.unload()
    assert plugin.actions == []
    assert plugin.toolbar is None


def test_opens_the_dock_and_unload_removes_it(plugin):
    plugin.initGui()
    plugin.toggle_dock()
    assert plugin.dock is not None
    assert not plugin.dock.isHidden()
    plugin.unload()
    assert plugin.dock is None
