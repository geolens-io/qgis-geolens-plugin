"""Turn one QGIS symbol into the MapLibre paint properties GeoLens draws."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qgis.core import QgsSimpleMarkerSymbolLayerBase
from qgis.PyQt.QtCore import Qt

from .loss import LossLog
from .units import enum_name, to_pixels

_DASH_PATTERNS = {
    Qt.PenStyle.DashLine: [4, 2],
    Qt.PenStyle.DotLine: [1, 2],
    Qt.PenStyle.DashDotLine: [4, 2, 1, 2],
    Qt.PenStyle.DashDotDotLine: [4, 2, 1, 2, 1, 2],
}
_CAPS = {
    Qt.PenCapStyle.FlatCap: "butt",
    Qt.PenCapStyle.SquareCap: "square",
    Qt.PenCapStyle.RoundCap: "round",
}
_JOINS = {
    Qt.PenJoinStyle.BevelJoin: "bevel",
    Qt.PenJoinStyle.MiterJoin: "miter",
    Qt.PenJoinStyle.RoundJoin: "round",
}

# Each geometry's colour property and the property a size-driven class varies.
COLOR_PROPERTY = {"fill": "fill-color", "line": "line-color", "marker": "circle-color"}
OPACITY_PROPERTY = {
    "fill": "fill-opacity",
    "line": "line-opacity",
    "marker": "circle-opacity",
}
SIZE_PROPERTY = {"line": "line-width", "marker": "circle-radius"}


@dataclass
class SymbolStyle:
    """One symbol's look, split into what a class may change and what it may not."""

    kind: str
    color: str | None = None
    paint: dict[str, Any] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)
    builder: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> float | None:
        return self.paint.get(SIZE_PROPERTY.get(self.kind, ""))

    def fixed(self, with_size: bool = False) -> dict[str, Any]:
        """Everything a class can't change: all but the colour, and the size unless asked."""
        skip = None if with_size else SIZE_PROPERTY.get(self.kind)
        return {
            "paint": {k: v for k, v in self.paint.items() if k != skip},
            "layout": self.layout,
            "builder": self.builder,
        }


def _stroke_pixels(value: float, unit, losses: LossLog, where: str) -> float:
    """A zero width is a hairline one pixel wide in QGIS; MapLibre would draw nothing."""
    return to_pixels(value, unit, losses, where) if value else 1.0


def _opacity(symbol, color) -> float:
    return round(symbol.opacity() * color.alphaF(), 3)


def _data_defined(owner, losses: LossLog, where: str) -> None:
    """Report data-defined properties of a symbol or one of its layers."""
    properties = owner.dataDefinedProperties()
    if not properties.hasActiveProperties():
        return
    definitions = owner.propertyDefinitions()
    names = []
    for key in properties.propertyKeys():
        if properties.property(key).isActive():
            definition = definitions.get(key)
            names.append(definition.name() if definition else str(key))
    losses.add(
        "expression_not_representable",
        where,
        "data-defined " + ", ".join(sorted(names)),
    )


def _fill(symbol, layer, style: SymbolStyle, losses: LossLog, where: str) -> None:
    kind = layer.layerType()
    if kind == "SimpleFill":
        color = layer.color()
        style.color = color.name()
        style.paint["fill-opacity"] = _opacity(symbol, color)
        offset = layer.offset()
        if offset.x() or offset.y():
            style.paint["fill-translate"] = [
                to_pixels(offset.x(), layer.offsetUnit(), losses, where),
                to_pixels(offset.y(), layer.offsetUnit(), losses, where),
            ]
        if layer.brushStyle() == Qt.BrushStyle.NoBrush:
            style.builder["fillDisabled"] = True
        elif layer.brushStyle() != Qt.BrushStyle.SolidPattern:
            losses.add("symbol_layer_dropped", where, "fill pattern drawn solid")
        outline_color, outline_width = layer.strokeColor(), layer.strokeWidth()
        outline_unit, outline_style = layer.strokeWidthUnit(), layer.strokeStyle()
    elif kind == "SimpleLine":
        # An "outline" fill: no fill at all, only the line.
        style.color = symbol.color().name()
        style.paint["fill-opacity"] = _opacity(symbol, symbol.color())
        style.builder["fillDisabled"] = True
        outline_color, outline_width = layer.color(), layer.width()
        outline_unit, outline_style = layer.widthUnit(), layer.penStyle()
    else:
        losses.add("symbol_layer_dropped", where, kind)
        color = symbol.color()
        style.color = color.name()
        style.paint["fill-opacity"] = _opacity(symbol, color)
        return
    if outline_style == Qt.PenStyle.NoPen:
        style.builder["strokeDisabled"] = True
        if style.builder.get("fillDisabled"):
            # Neither fill nor outline: the symbol draws nothing.
            style.paint["fill-opacity"] = 0
            style.color = None
        return
    style.builder["outlineColor"] = outline_color.name()
    style.builder["outlineWidth"] = _stroke_pixels(
        outline_width, outline_unit, losses, where
    )
    if outline_style != Qt.PenStyle.SolidLine or _opacity(symbol, outline_color) < 1:
        losses.add("effect_dropped", where, "outline dash pattern or transparency")


def _line(symbol, layer, style: SymbolStyle, losses: LossLog, where: str) -> None:
    color = layer.color() if layer.layerType() == "SimpleLine" else symbol.color()
    style.color = color.name()
    style.paint["line-opacity"] = _opacity(symbol, color)
    if layer.layerType() == "SimpleLine" and layer.penStyle() == Qt.PenStyle.NoPen:
        # Draws nothing, like a symbol without layers.
        style.paint["line-opacity"] = 0
        style.color = None
        return
    if layer.layerType() != "SimpleLine":
        losses.add("symbol_layer_dropped", where, layer.layerType())
        return
    unit = layer.widthUnit()
    width = _stroke_pixels(layer.width(), unit, losses, where)
    style.paint["line-width"] = width
    if layer.offset():
        style.paint["line-offset"] = to_pixels(
            layer.offset(), layer.offsetUnit(), losses, where
        )
    if layer.useCustomDashPattern() and width:
        pattern_unit = layer.customDashPatternUnit()
        dashes = [
            round(to_pixels(length, pattern_unit, losses, where) / width, 2)
            for length in layer.customDashVector()
        ]
        if dashes:
            style.paint["line-dasharray"] = dashes
    elif layer.penStyle() in _DASH_PATTERNS:
        style.paint["line-dasharray"] = _DASH_PATTERNS[layer.penStyle()]
    style.layout["line-cap"] = _CAPS.get(layer.penCapStyle(), "butt")
    style.layout["line-join"] = _JOINS.get(layer.penJoinStyle(), "bevel")


def _marker(symbol, layer, style: SymbolStyle, losses: LossLog, where: str) -> None:
    kind = layer.layerType()
    if kind != "SimpleMarker":
        # SVG, image and font markers need an icon upload that this conversion doesn't do.
        losses.add("icon_not_uploaded", where, kind)
        color = symbol.color()
        style.color = color.name()
        style.paint["circle-opacity"] = _opacity(symbol, color)
        return
    if layer.shape() != QgsSimpleMarkerSymbolLayerBase.Shape.Circle:
        losses.add("marker_shape_approximated", where, enum_name(layer.shape()))
    color = layer.color()
    style.color = color.name()
    style.paint["circle-radius"] = round(
        to_pixels(layer.size(), layer.sizeUnit(), losses, where) / 2, 2
    )
    style.paint["circle-opacity"] = _opacity(symbol, color)
    base = QgsSimpleMarkerSymbolLayerBase
    if (
        layer.horizontalAnchorPoint() != base.HorizontalAnchorPoint.HCenter
        or layer.verticalAnchorPoint() != base.VerticalAnchorPoint.VCenter
    ):
        losses.add("effect_dropped", where, "marker anchor point")
    offset = layer.offset()
    if offset.x() or offset.y():
        style.paint["circle-translate"] = [
            to_pixels(offset.x(), layer.offsetUnit(), losses, where),
            to_pixels(offset.y(), layer.offsetUnit(), losses, where),
        ]
    if layer.strokeStyle() == Qt.PenStyle.NoPen:
        style.paint["circle-stroke-width"] = 0
        return
    if layer.strokeStyle() != Qt.PenStyle.SolidLine:
        losses.add("effect_dropped", where, "outline dash pattern")
    stroke = layer.strokeColor()
    style.paint["circle-stroke-color"] = stroke.name()
    style.paint["circle-stroke-width"] = _stroke_pixels(
        layer.strokeWidth(), layer.strokeWidthUnit(), losses, where
    )
    stroke_opacity = _opacity(symbol, stroke)
    if stroke_opacity < 1:
        style.paint["circle-stroke-opacity"] = stroke_opacity


_CONVERTERS = {"fill": _fill, "line": _line, "marker": _marker}
_SYMBOL_KINDS = {
    "QgsFillSymbol": "fill",
    "QgsLineSymbol": "line",
    "QgsMarkerSymbol": "marker",
}


def symbol_kind(symbol) -> str:
    return _SYMBOL_KINDS[type(symbol).__name__]


def hidden_style(kind: str) -> SymbolStyle:
    """The style of a class that has no symbol: it draws nothing."""
    return SymbolStyle(kind, paint={OPACITY_PROPERTY[kind]: 0})


def convert_symbol(symbol, losses: LossLog, where: str) -> SymbolStyle:
    """Convert a symbol's first symbol layer; every other layer is reported dropped."""
    kind = symbol_kind(symbol)
    style = SymbolStyle(kind)
    layers = [layer for layer in symbol.symbolLayers() if layer.enabled()]
    if not layers:
        # Nothing to draw: keep it invisible rather than letting MapLibre's defaults show it.
        style.paint[OPACITY_PROPERTY[kind]] = 0
        return style
    primary = layers[0]
    _CONVERTERS[kind](symbol, primary, style, losses, where)
    for extra in layers[1:]:
        losses.add("symbol_layer_dropped", where, extra.layerType())
    _data_defined(symbol, losses, where)
    for symbol_layer in layers:
        _data_defined(symbol_layer, losses, where)
        effect = symbol_layer.paintEffect()
        if effect is not None and effect.enabled():
            losses.add("effect_dropped", where, "draw effect")
    return style
