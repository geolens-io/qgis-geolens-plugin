"""Regression tests for the development installer's profile detection."""

import sys
from pathlib import Path

import pytest

import install


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    return tmp_path


def settings_root(home: Path) -> Path:
    return home / "Library/Application Support/QGIS"


def test_prefers_an_existing_qgis4_profile(fake_home):
    (settings_root(fake_home) / "QGIS4/profiles/default").mkdir(parents=True)
    (settings_root(fake_home) / "QGIS3/profiles/default").mkdir(parents=True)
    assert "QGIS4" in str(install.default_plugin_dir())


def test_uses_qgis3_when_it_is_the_only_profile(fake_home):
    (settings_root(fake_home) / "QGIS3/profiles/default").mkdir(parents=True)
    result = install.default_plugin_dir()
    assert "QGIS3" in str(result)
    assert str(result).endswith("profiles/default/python/plugins")


def test_falls_back_to_qgis3_when_no_profile_exists(fake_home):
    assert "QGIS3" in str(install.default_plugin_dir())


def test_reports_a_found_profile_without_a_plugins_directory(fake_home):
    """A fresh profile has no python/plugins yet; that is not a wrong profile."""
    (settings_root(fake_home) / "QGIS4/profiles/default").mkdir(parents=True)
    plugin_dir, found_profile = install.detect_plugin_dir()
    assert found_profile is True
    assert not plugin_dir.exists()


def test_reports_no_profile_when_none_exists(fake_home):
    _, found_profile = install.detect_plugin_dir()
    assert found_profile is False


def test_ignores_a_profiles_directory_without_the_default_profile(fake_home):
    """QGIS loads profiles/default unless told otherwise; a lone named profile isn't it."""
    (settings_root(fake_home) / "QGIS4/profiles/work").mkdir(parents=True)
    _, found_profile = install.detect_plugin_dir()
    assert found_profile is False


def test_windows_root_honors_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    (tmp_path / "Roaming/QGIS/QGIS4/profiles/default").mkdir(parents=True)
    assert "QGIS4" in str(install.default_plugin_dir())
