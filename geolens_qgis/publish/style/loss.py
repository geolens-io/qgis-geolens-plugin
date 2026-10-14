"""Loss items: what a style conversion could not carry over to GeoLens."""

from __future__ import annotations

from typing import Any

LOSS_KINDS = frozenset(
    {
        "unsupported_renderer",
        "rule_based_approximated",
        "expression_not_representable",
        "expression_materialized",
        "filter_not_representable",
        "unit_not_representable",
        "symbol_layer_dropped",
        "effect_dropped",
        "label_approximated",
        "icon_not_uploaded",
        "nested_group",
        "external_layer",
        "basemap_replaced",
        "unsupported_layer_type",
        "non_spatial_layer",
        "file_too_large",
        "public_blocked_by_policy",
        "raster_renderer_approximated",
        "scale_range_clamped",
        "class_edge_shifted",
        "null_class_changed",
        "marker_shape_approximated",
        "blend_mode_dropped",
    }
)

_ANALYZE_CODES = {
    "unsupported_renderer": "A061",
    "external_layer": "A030",
    "unsupported_layer_type": "A030",
    "non_spatial_layer": "A032",
    "file_too_large": "A010",
    "public_blocked_by_policy": "A020",
}


def analyze_code(kind: str) -> str:
    """Return the Analyze row code a loss kind raises."""
    if kind not in LOSS_KINDS:
        raise ValueError(f"unknown loss kind: {kind}")
    return _ANALYZE_CODES.get(kind, "A060")


class LossLog:
    """Collects the loss items of one layer under a manifest path prefix."""

    def __init__(self, base_path: str):
        self.base_path = base_path
        self.items: list[dict[str, Any]] = []

    def add(self, kind: str, where: str, detail: str | None = None) -> None:
        if kind not in LOSS_KINDS:
            raise ValueError(f"unknown loss kind: {kind}")
        item = {"path": f"{self.base_path}.{where}", "kind": kind}
        if detail:
            item["detail"] = detail
        if item not in self.items:
            self.items.append(item)
