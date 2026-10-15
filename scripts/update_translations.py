#!/usr/bin/env python3
"""Refresh the Qt Linguist .ts files from tr() calls and compile the .qm files."""

from __future__ import annotations

import argparse
import ast
import shutil
import subprocess  # nosec B404
import sys
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path
from xml.sax.saxutils import escape  # nosec B406

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "geolens_qgis"
CONTEXT = "GeoLens"
LANGUAGES = ("es", "fr", "de", "zh")
LRELEASE_CANDIDATES = ("lrelease", "lrelease-qt6", "/usr/lib/qt6/bin/lrelease")


def _string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def extract_strings(root: Path) -> set[str]:
    """Source strings of every tr("...") and QCoreApplication.translate("GeoLens", "...")."""
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "tr" and node.args:
                text = _string(node.args[0])
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "translate"
                and len(node.args) >= 2
                and _string(node.args[0]) == CONTEXT
            ):
                text = _string(node.args[1])
            else:
                continue
            if text is not None:
                found.add(text)
    return found


def read_translations(ts_path: Path) -> dict[str, str]:
    if not ts_path.is_file():
        return {}
    translations = {}
    for message in ET.parse(ts_path).getroot().iter("message"):  # nosec B314
        translation = message.find("translation")
        if translation is None or translation.get("type") == "unfinished":
            continue
        translations[message.findtext("source", "")] = translation.text or ""
    return translations


def write_ts(ts_path: Path, language: str, sources: set[str]) -> int:
    """Write the .ts file, keeping existing translations. Returns the untranslated count."""
    existing = read_translations(ts_path)
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!DOCTYPE TS>",
        f'<TS version="2.1" language="{language}" sourcelanguage="en">',
        "<context>",
        f"    <name>{CONTEXT}</name>",
    ]
    missing = 0
    for source in sorted(sources):
        translated = existing.get(source)
        if translated:
            translation = f"<translation>{escape(translated)}</translation>"
        else:
            translation = '<translation type="unfinished"></translation>'
            missing += 1
        lines.append(
            f"    <message>\n        <source>{escape(source)}</source>\n"
            f"        {translation}\n    </message>"
        )
    lines += ["</context>", "</TS>", ""]
    ts_path.write_text("\n".join(lines), encoding="utf-8")
    return missing


def find_lrelease() -> str:
    for candidate in LRELEASE_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    raise SystemExit("lrelease not found; run this inside the qgis/qgis image")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PLUGIN_DIR)
    parser.add_argument(
        "--no-compile", action="store_true", help="only update the .ts files"
    )
    args = parser.parse_args()
    i18n_dir = args.source / "i18n"
    i18n_dir.mkdir(exist_ok=True)
    sources = extract_strings(args.source)
    for language in LANGUAGES:
        ts_path = i18n_dir / f"geolens_{language}.ts"
        missing = write_ts(ts_path, language, sources)
        print(f"{ts_path.name}: {len(sources)} strings, {missing} untranslated")
        if not args.no_compile:
            qm_path = ts_path.with_suffix(".qm")
            subprocess.run(  # nosec B603
                [find_lrelease(), "-silent", str(ts_path), "-qm", str(qm_path)],
                check=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
