"""Length and scale conversions between QGIS and web maps."""

from __future__ import annotations

import math
from typing import Any

from .loss import LossLog

PIXELS_PER_MM = 96 / 25.4
PIXELS_PER_POINT = 96 / 72
PIXELS_PER_INCH = 96
REFERENCE_SCALE = 591657550.5

_UNIT_TO_PIXELS = {
    "Pixels": 1.0,
    "Millimeters": PIXELS_PER_MM,
    "Points": PIXELS_PER_POINT,
    "Inches": PIXELS_PER_INCH,
}


def enum_name(value: Any) -> str:
    """Return a QGIS enum member's name, for loss details."""
    return getattr(value, "name", None) or str(value)


def to_pixels(value: float, unit: Any, losses: LossLog, where: str) -> float:
    """Convert a length to pixels at 96 DPI.

    Map units, percentages and anything else with no fixed pixel size keep
    their number as if it were pixels, and say so.
    """
    factor = _UNIT_TO_PIXELS.get(enum_name(unit))
    if factor is None:
        losses.add("unit_not_representable", where, f"{enum_name(unit)} kept as pixels")
        factor = 1.0
    return round(value * factor, 2)


def scale_to_zoom(scale: float) -> float:
    """Return the web map zoom whose scale denominator is `scale`, to one decimal."""
    return round(math.log2(REFERENCE_SCALE / scale) - 1, 1)
