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
    (settings_root(fake_home) / "QGIS4/profiles").mkdir(parents=True)
    (settings_root(fake_home) / "QGIS3/profiles").mkdir(parents=True)
    assert "QGIS4" in str(install.default_plugin_dir())


def test_uses_qgis3_when_it_is_the_only_profile(fake_home):
    (settings_root(fake_home) / "QGIS3/profiles").mkdir(parents=True)
    result = install.default_plugin_dir()
    assert "QGIS3" in str(result)
    assert str(result).endswith("profiles/default/python/plugins")


def test_falls_back_to_qgis3_when_no_profile_exists(fake_home):
    assert "QGIS3" in str(install.default_plugin_dir())


def test_windows_root_honors_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    (tmp_path / "Roaming/QGIS/QGIS4/profiles").mkdir(parents=True)
    assert "QGIS4" in str(install.default_plugin_dir())
