"""The GeoLens sequential colour ramps a heatmap can use, sampled as the builder samples them."""

from __future__ import annotations

import math

_STOPS = {
    "YlOrRd": (
        "#ffffcc",
        "#ffeda0",
        "#fed976",
        "#feb24c",
        "#fd8d3c",
        "#fc4e2a",
        "#e31a1c",
        "#bd0026",
        "#800026",
    ),
    "YlGnBu": (
        "#ffffd9",
        "#edf8b1",
        "#c7e9b4",
        "#7fcdbb",
        "#41b6c4",
        "#1d91c0",
        "#225ea8",
        "#253494",
        "#081d58",
    ),
    "Viridis": (
        "#440154",
        "#482777",
        "#3f4a8a",
        "#31678e",
        "#26838f",
        "#1f9d8a",
        "#6cce5a",
        "#b6de2b",
        "#fee825",
    ),
    "Blues": (
        "#f7fbff",
        "#deebf7",
        "#c6dbef",
        "#9ecae1",
        "#6baed6",
        "#4292c6",
        "#2171b5",
        "#08519c",
        "#08306b",
    ),
    "Greens": (
        "#f7fcf5",
        "#e5f5e0",
        "#c7e9c0",
        "#a1d99b",
        "#74c476",
        "#41ab5d",
        "#238b45",
        "#006d2c",
        "#00441b",
    ),
    "Oranges": (
        "#fff5eb",
        "#fee6ce",
        "#fdd0a2",
        "#fdae6b",
        "#fd8d3c",
        "#f16913",
        "#d94801",
        "#a63603",
        "#7f2704",
    ),
    "Reds": (
        "#fff5f0",
        "#fee0d2",
        "#fcbba1",
        "#fc9272",
        "#fb6a4a",
        "#ef3b2c",
        "#cb181d",
        "#a50f15",
        "#67000d",
    ),
    "Purples": (
        "#fcfbfd",
        "#efedf5",
        "#dadaeb",
        "#bcbddc",
        "#9e9ac8",
        "#807dba",
        "#6a51a3",
        "#54278f",
        "#3f007d",
    ),
    "BuGn": (
        "#f7fcfd",
        "#e5f5f9",
        "#ccece6",
        "#99d8c9",
        "#66c2a4",
        "#41ae76",
        "#238b45",
        "#006d2c",
        "#00441b",
    ),
    "BuPu": (
        "#f7fcfd",
        "#e0ecf4",
        "#bfd3e6",
        "#9ebcda",
        "#8c96c6",
        "#8c6bb1",
        "#88419d",
        "#810f7c",
        "#4d004b",
    ),
    "OrRd": (
        "#fff7ec",
        "#fee8c8",
        "#fdd49e",
        "#fdbb84",
        "#fc8d59",
        "#ef6548",
        "#d7301f",
        "#b30000",
        "#7f0000",
    ),
    "YlGn": (
        "#ffffe5",
        "#f7fcb9",
        "#d9f0a3",
        "#addd8e",
        "#78c679",
        "#41ab5d",
        "#238443",
        "#006837",
        "#004529",
    ),
}

HEATMAP_SAMPLES = 5
TRANSPARENT = "rgba(0,0,0,0)"


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _hex(channels) -> str:
    return "#" + "".join(f"{math.floor(c + 0.5):02x}" for c in channels)


def _sample(stops, t: float) -> str:
    last = len(stops) - 1
    if t <= 0:
        return stops[0]
    if t >= 1:
        return stops[last]
    segment = min(int(t * last), last - 1)
    fraction = t * last - segment
    low, high = _rgb(stops[segment]), _rgb(stops[segment + 1])
    return _hex(a + (b - a) * fraction for a, b in zip(low, high))


def ramp_colors(name: str, reversed_: bool = False) -> list[str]:
    """Return the colours the builder draws a heatmap ramp with."""
    stops = _STOPS[name]
    colors = [_sample(stops, i / (HEATMAP_SAMPLES - 1)) for i in range(HEATMAP_SAMPLES)]
    return colors[::-1] if reversed_ else colors


def heatmap_color_expression(name: str, reversed_: bool = False) -> list:
    """Return the `heatmap-color` expression the builder builds for a named ramp."""
    colors = ramp_colors(name, reversed_)
    expression: list = ["interpolate", ["linear"], ["heatmap-density"], 0, TRANSPARENT]
    for density, color in zip((0.2, 0.4, 0.6, 0.8, 1.0), colors):
        expression += [density, color]
    return expression


def nearest_ramp(colors: list[str]) -> tuple[str, bool, float]:
    """Return the closest named ramp, whether it runs reversed, and the mean channel error.

    `colors` are `HEATMAP_SAMPLES` hex colours sampled evenly along the source ramp.
    """
    best = None
    for name in _STOPS:
        for reverse in (False, True):
            candidate = ramp_colors(name, reverse)
            error = sum(
                abs(a - b)
                for x, y in zip(colors, candidate)
                for a, b in zip(_rgb(x), _rgb(y))
            ) / (3 * HEATMAP_SAMPLES)
            if best is None or error < best[2]:
                best = (name, reverse, error)
    return best
