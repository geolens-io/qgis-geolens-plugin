"""Translator loading, the About dialog and the extraction script."""

import importlib.util
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("pytest_qgis")

from geolens_qgis import classFactory, plugin as plugin_module  # noqa: E402
from package_plugin import PLUGIN_DIR, package_plugin  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "update_translations", ROOT / "scripts" / "update_translations.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin(real_qgis, qgis_iface):
    instance = classFactory(qgis_iface)
    yield instance
    instance.unload()


def _texts(plugin):
    return [action.text() for action in plugin.actions]


def test_menu_text_is_translated_for_a_language_with_a_catalog(plugin, monkeypatch):
    monkeypatch.setattr(plugin_module, "user_language", lambda: "es")
    plugin.initGui()
    assert _texts(plugin) == ["Catálogo de GeoLens", "Acerca de GeoLens"]
    assert plugin.translator is not None
    plugin.unload()
    assert plugin.translator is None


def test_unload_restores_the_source_language(plugin, monkeypatch):
    monkeypatch.setattr(plugin_module, "user_language", lambda: "de")
    plugin.initGui()
    plugin.unload()
    monkeypatch.setattr(plugin_module, "user_language", lambda: "xx")
    plugin.initGui()
    assert _texts(plugin) == ["GeoLens Catalog", "About GeoLens"]


def test_no_translator_for_an_unknown_language(plugin, monkeypatch):
    monkeypatch.setattr(plugin_module, "user_language", lambda: "xx")
    plugin.initGui()
    assert plugin.translator is None
    assert _texts(plugin) == ["GeoLens Catalog", "About GeoLens"]


def test_about_box_shows_the_version_and_links(plugin, monkeypatch):
    monkeypatch.setattr(plugin_module, "user_language", lambda: "xx")
    plugin.initGui()
    box = plugin.about_box()
    text = box.text()
    assert plugin.version() and plugin.version() in text
    for url in (
        plugin_module.DOCS_URL,
        plugin_module.REPOSITORY_URL,
        plugin_module.ISSUES_URL,
    ):
        assert url in text
    assert not box.iconPixmap().isNull()


@pytest.mark.parametrize(
    "locale, expected",
    [
        ("es_ES", "es"),
        ("pt-BR", "pt"),
        ("zh", "zh"),
        ("zh_CN", "zh"),
        ("zh_SG", "zh"),
        ("zh_Hans", "zh"),
        ("zh-Hans", "zh"),
        ("zh_TW", ""),
        ("zh_HK", ""),
        ("zh_MO", ""),
        ("zh_Hant", ""),
        ("zh-Hant", ""),
        ("zh-Hant-TW", ""),
    ],
)
def test_user_language_maps_locales_to_catalogs(monkeypatch, locale, expected):
    values = {"locale/overrideFlag": True, "locale/userLocale": locale}

    class Settings:
        def value(self, key, default=None, **_):
            return values.get(key, default)

    monkeypatch.setattr(plugin_module, "QgsSettings", Settings)
    assert plugin_module.user_language() == expected


def test_traditional_chinese_loads_no_catalog(plugin, monkeypatch):
    monkeypatch.setattr(plugin_module, "user_language", lambda: "")
    plugin.initGui()
    assert plugin.translator is None


def test_package_ships_qm_files_and_not_ts_sources(tmp_path):
    with zipfile.ZipFile(package_plugin(PLUGIN_DIR, tmp_path / "p.zip")) as archive:
        names = set(archive.namelist())
    for language in ("es", "fr", "de", "zh"):
        assert f"geolens_qgis/i18n/geolens_{language}.qm" in names
    assert not any(name.endswith(".ts") for name in names)
    assert "geolens_qgis/icons/icon.svg" in names


def test_extraction_finds_tr_and_translate_calls(tmp_path):
    script = _load_script()
    (tmp_path / "module.py").write_text(
        "from x import tr\n"
        'tr("Plain string")\n'
        'tr("Joined " "across lines")\n'
        'QCoreApplication.translate("GeoLens", "Qt style")\n'
        'QCoreApplication.translate("Other", "Other context")\n'
        "tr(variable)\n",
        encoding="utf-8",
    )
    assert script.extract_strings(tmp_path) == {
        "Plain string",
        "Joined across lines",
        "Qt style",
    }


def test_ts_update_keeps_existing_translations(tmp_path):
    script = _load_script()
    ts = tmp_path / "geolens_es.ts"
    script.write_ts(ts, "es", {"Hello", "Bye"})
    ts.write_text(
        ts.read_text(encoding="utf-8").replace(
            '<translation type="unfinished"></translation>',
            "<translation>Hola</translation>",
            1,
        ),
        encoding="utf-8",
    )
    missing = script.write_ts(ts, "es", {"Hello", "Bye", "New"})
    assert missing == 2
    assert script.read_translations(ts) == {"Bye": "Hola"}
