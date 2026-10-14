#!/usr/bin/env python3
"""Install or remove GeoLens in the current user's default QGIS profile."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

PLUGIN_NAME = "geolens_qgis"


# Newest first: prefer an existing QGIS 4 profile tree over QGIS 3. The
# QGIS3 name is still the fallback when neither exists, preserving the old
# behavior for machines where QGIS has never run.
QGIS_VERSION_DIRS = ("QGIS4", "QGIS3")
PROFILE_PLUGINS = Path("profiles/default/python/plugins")


def qgis_settings_root() -> Path:
    home = Path.home()
    if sys.platform.startswith("linux"):
        return home / ".local/share/QGIS"
    if sys.platform == "darwin":
        return home / "Library/Application Support/QGIS"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "QGIS"
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def detect_plugin_dir() -> tuple[Path, bool]:
    """Return the default plugin directory and whether a real profile backs it.

    The flag reports the profile probe, not the plugins directory: a valid
    fresh profile has no `python/plugins` yet, and install() creates it.
    """
    root = qgis_settings_root()
    for name in QGIS_VERSION_DIRS:
        if (root / name / "profiles/default").is_dir():
            return root / name / PROFILE_PLUGINS, True
    return root / QGIS_VERSION_DIRS[-1] / PROFILE_PLUGINS, False


def default_plugin_dir() -> Path:
    return detect_plugin_dir()[0]


def install(source: Path, plugin_dir: Path) -> Path:
    target = plugin_dir / PLUGIN_NAME
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path)
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    if args.plugin_dir is not None:
        plugin_dir = args.plugin_dir
    else:
        plugin_dir, found_profile = detect_plugin_dir()
        if not found_profile:
            print(
                f"warning: no QGIS profile directory was found under "
                f"{qgis_settings_root()}, so {plugin_dir} is a guess and QGIS "
                "may load plugins from somewhere else. Pass --plugin-dir to "
                "override."
            )
    target = plugin_dir / PLUGIN_NAME
    if args.remove:
        if target.exists():
            shutil.rmtree(target)
            print(f"Removed {target}")
        return 0
    installed = install(Path(__file__).parent / PLUGIN_NAME, target.parent)
    print(f"Installed GeoLens at {installed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
