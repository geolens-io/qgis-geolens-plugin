#!/usr/bin/env python3
"""Build a QGIS Plugin Manager compatible GeoLens zip archive."""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent / "geolens_qgis"
EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".git", "dist"}
EXCLUDED_FILES = (re.compile(r".*\.(?:pyc|pyo|ts)$"), re.compile(r".*~$"))


def metadata_version(source: Path) -> str:
    for line in (source / "metadata.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("version="):
            return line.partition("=")[2].strip()
    raise ValueError("metadata.txt contains no version")


def package_plugin(source: Path, output: Path, name: str = "geolens_qgis") -> Path:
    if not source.is_dir():
        raise FileNotFoundError(f"Plugin source does not exist: {source}")
    for required in ("__init__.py", "metadata.txt", "LICENSE"):
        if not (source / required).is_file():
            raise FileNotFoundError(f"Required plugin file is missing: {required}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if path.is_dir() or any(part in EXCLUDED_DIRS for part in relative.parts):
                continue
            if path.name.startswith(".") or any(
                pattern.fullmatch(path.name) for pattern in EXCLUDED_FILES
            ):
                continue
            # ZipFile.write follows symbolic links, which would copy file
            # contents from outside the plugin tree into the archive.
            if path.is_symlink():
                continue
            archive.write(path, Path(name) / relative)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PLUGIN_DIR)
    parser.add_argument("--name", default="geolens_qgis")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    version = metadata_version(args.source)
    output = args.output or Path("dist") / f"{args.name}-{version}.zip"
    path = package_plugin(args.source, output, args.name)
    print(f"Created {path} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
