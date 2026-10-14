"""Assemble the manifest layer for one QGIS vector layer."""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtGui import QPainter

from .loss import LossLog
from .renderers import convert_renderer
from .units import scale_to_zoom

MAX_ZOOM = 24
MAX_POPUP_FIELDS = 100
MAX_NAME = 255
MAX_FIELD_NAME = 128


def zoom_range(layer, losses: LossLog) -> tuple[float | None, float | None]:
    """Return the layer's `(min_zoom, max_zoom)` from its scale-based visibility.

    GeoLens zoom runs 0 to 24, so a bound outside that range is no bound. A
    layer that is only visible beyond the range is clamped to its edge.
    """
    if not layer.hasScaleBasedVisibility():
        return None, None
    low = scale_to_zoom(layer.minimumScale()) if layer.minimumScale() > 0 else None
    high = scale_to_zoom(layer.maximumScale()) if layer.maximumScale() > 0 else None
    if low is not None and low > MAX_ZOOM:
        losses.add(
            "scale_range_clamped", "min_zoom", f"zoom {low} is beyond {MAX_ZOOM}"
        )
        low = MAX_ZOOM
    if high is not None and high < 0:
        losses.add("scale_range_clamped", "max_zoom", f"zoom {high} is below 0")
        high = 0
    return (
        None if low is not None and low <= 0 else low,
        None if high is not None and high >= MAX_ZOOM else high,
    )


def popup_config(layer, losses: LossLog) -> dict[str, Any]:
    """Show the fields the layer's form doesn't hide."""
    fields = layer.fields()
    visible = [
        fields.at(index).name()
        for index in range(fields.count())
        if layer.editorWidgetSetup(index).type() != "Hidden"
    ]
    if len(visible) == fields.count():
        return {"enabled": True, "visible_fields": None}
    if any(len(name) > MAX_FIELD_NAME for name in visible):
        losses.add(
            "expression_not_representable",
            "popup_config",
            f"field names longer than {MAX_FIELD_NAME} characters left out",
        )
        visible = [name for name in visible if len(name) <= MAX_FIELD_NAME]
    if len(visible) > MAX_POPUP_FIELDS:
        losses.add(
            "expression_not_representable",
            "popup_config",
            f"{len(visible) - MAX_POPUP_FIELDS} fields beyond {MAX_POPUP_FIELDS} left out",
        )
    return {"enabled": bool(visible), "visible_fields": visible[:MAX_POPUP_FIELDS]}


def convert_layer(
    layer,
    *,
    key: str,
    dataset_key: str | None = None,
    dataset_id: str | None = None,
    visible: bool = True,
    path: str = "layers[0]",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a manifest layer for a vector layer, and the styling it lost.

    Exactly one of `dataset_key` and `dataset_id` names the dataset. `path` is
    the layer's place in the manifest, which prefixes each loss item's path.
    """
    if (dataset_key is None) == (dataset_id is None):
        raise ValueError("give exactly one of dataset_key and dataset_id")
    if type(layer).__name__ != "QgsVectorLayer":
        raise TypeError("only vector layers can be converted")
    losses = LossLog(f"{path}.style")
    style = convert_renderer(layer, losses)
    normal = QPainter.CompositionMode.CompositionMode_SourceOver
    if layer.blendMode() != normal or layer.featureBlendMode() != normal:
        losses.add("blend_mode_dropped", "blendMode")
    min_zoom, max_zoom = zoom_range(layer, losses)
    entry: dict[str, Any] = {"key": key}
    if dataset_key is not None:
        entry["dataset_key"] = dataset_key
    if dataset_id is not None:
        entry["dataset_id"] = dataset_id
    entry.update(
        display_name=layer.name()[:MAX_NAME] or key,
        visible=visible,
        opacity=round(layer.opacity(), 3),
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        style_config=style.style_config,
        paint=style.paint,
        layout=style.layout,
        filter=style.filter,
        popup_config=popup_config(layer, losses),
    )
    return entry, losses.items
