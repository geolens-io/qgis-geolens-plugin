"""Style conversion against real QGIS layers, validated against the manifest schema."""

import json
import math
from pathlib import Path

import pytest

pytest.importorskip("pytest_qgis")

import jsonschema  # noqa: E402
from qgis.core import (  # noqa: E402
    Qgis,
    Qgs25DRenderer,
    QgsCategorizedSymbolRenderer,
    QgsClassificationEqualInterval,
    QgsClassificationJenks,
    QgsClassificationPrettyBreaks,
    QgsClassificationQuantile,
    QgsClassificationStandardDeviation,
    QgsEditorWidgetSetup,
    QgsFeature,
    QgsField,
    QgsFeatureRenderer,
    QgsFillSymbol,
    QgsFontMarkerSymbolLayer,
    QgsGradientColorRamp,
    QgsGradientStop,
    QgsGraduatedSymbolRenderer,
    QgsHeatmapRenderer,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPointClusterRenderer,
    QgsPointDisplacementRenderer,
    QgsRenderContext,
    QgsProperty,
    QgsRasterLayer,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSimpleMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsSymbol,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDate, QVariant  # noqa: E402
from qgis.PyQt.QtGui import QColor, QPainter  # noqa: E402

from geolens_qgis.publish.style.layer import convert_layer  # noqa: E402
from geolens_qgis.publish.style.ramps import ramp_colors  # noqa: E402

SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "publish_manifest_v2.schema.json").read_text()
)
LAYER_SCHEMA = {
    "$schema": SCHEMA["$schema"],
    "$defs": SCHEMA["$defs"],
    "$ref": "#/$defs/layer",
}
PATH = "layers[0].style"
NEXT_10 = math.nextafter(10.0, math.inf)
NEXT_100 = math.nextafter(100.0, math.inf)
GREY = "#cccccc"


@pytest.fixture(autouse=True)
def _qgis(real_qgis, qgis_app):
    return qgis_app


FIELD_TYPES = {"s": QVariant.String, "i": QVariant.Int, "d": QVariant.Double}


def make_layer(geometry="Point", fields=(("type", "s"),), rows=(), name="parks"):
    layer = QgsVectorLayer(f"{geometry}?crs=epsg:4326", name, "memory")
    layer.dataProvider().addAttributes([QgsField(n, FIELD_TYPES[t]) for n, t in fields])
    layer.updateFields()
    for attributes in rows:
        feature = QgsFeature(layer.fields())
        feature.setAttributes(list(attributes))
        layer.dataProvider().addFeature(feature)
    return layer


def convert(layer, **kwargs):
    entry, losses = convert_layer(layer, key="parks", dataset_key="qgis-1", **kwargs)
    jsonschema.validate(entry, LAYER_SCHEMA)
    return entry, losses


def marker(**props):
    return QgsMarkerSymbol.createSimple({"name": "circle", "size": "2", **props})


def fill(color, **props):
    return QgsFillSymbol.createSimple(
        {"color": color, "outline_color": "#222222", **props}
    )


def loss(kind, where, detail=None):
    item = {"path": f"{PATH}.{where}", "kind": kind}
    if detail:
        item["detail"] = detail
    return item


CLUSTER_SIZE = loss(
    "effect_dropped",
    "renderer.clusterSymbol",
    "cluster symbol size and outline (GeoLens sizes clusters by count)",
)


def popup(visible=None):
    return {"enabled": True, "visible_fields": visible}


# Single symbol


def test_single_marker_converts_size_stroke_and_alpha():
    layer = make_layer()
    symbol = marker(
        color="#ff8800", size="4", outline_color="#112233", outline_width="0.4"
    )
    symbol.setOpacity(0.5)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    entry, losses = convert(layer)
    assert entry == {
        "key": "parks",
        "dataset_key": "qgis-1",
        "display_name": "parks",
        "visible": True,
        "opacity": 1.0,
        "min_zoom": None,
        "max_zoom": None,
        "style_config": None,
        "paint": {
            "circle-radius": 7.56,
            "circle-opacity": 0.5,
            "circle-stroke-color": "#112233",
            "circle-stroke-width": 1.51,
            "circle-stroke-opacity": 0.5,
            "circle-color": "#ff8800",
        },
        "layout": {},
        "filter": None,
        "popup_config": popup(),
    }
    assert losses == []


def test_symbol_opacity_applies_to_the_marker_stroke_too():
    layer = make_layer()
    symbol = marker(color="#ff0000", outline_color="#000000")
    symbol.setOpacity(0.5)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[0]["paint"]["circle-stroke-opacity"] == 0.5


def test_symbol_opacity_multiplies_the_colour_alpha():
    layer = make_layer()
    symbol = marker(color="255,136,0,102")
    symbol.setOpacity(0.5)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[0]["paint"]["circle-opacity"] == 0.2


def test_a_marker_in_map_units_keeps_its_number_and_reports_the_unit():
    layer = make_layer()
    layer.setRenderer(
        QgsSingleSymbolRenderer(
            marker(color="#ff0000", size="10", size_unit="MapUnit", outline_style="no")
        )
    )
    entry, losses = convert(layer)
    assert entry["paint"]["circle-radius"] == 5.0
    assert entry["paint"]["circle-stroke-width"] == 0
    assert losses == [
        loss("unit_not_representable", "renderer.symbol", "MapUnits kept as pixels")
    ]


def test_a_marker_offset_becomes_a_translation_in_pixels():
    layer = make_layer()
    symbol = marker(
        color="#ff0000", offset="1,-2", offset_unit="MM", outline_style="no"
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[0]["paint"]["circle-translate"] == [3.78, -7.56]


def test_a_dashed_marker_outline_is_reported():
    layer = make_layer()
    layer.setRenderer(
        QgsSingleSymbolRenderer(marker(color="#ff0000", outline_style="dash"))
    )
    assert convert(layer)[1] == [
        loss("effect_dropped", "renderer.symbol", "outline dash pattern")
    ]


def test_a_disabled_symbol_layer_is_not_drawn():
    layer = make_layer()
    symbol = marker(color="#ff0000")
    symbol.symbolLayer(0).setEnabled(False)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[0]["paint"] == {"circle-opacity": 0}


@pytest.mark.parametrize(
    "anchor", [{"horizontal_anchor_point": "0"}, {"vertical_anchor_point": "0"}]
)
def test_a_marker_anchored_off_centre_is_reported(anchor):
    layer = make_layer()
    symbol = marker(color="#ff0000", outline_style="no", **anchor)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[1] == [
        loss("effect_dropped", "renderer.symbol", "marker anchor point")
    ]


def test_a_symbol_with_no_layers_draws_nothing():
    layer = make_layer()
    symbol = marker(color="#ff0000")
    symbol.deleteSymbolLayer(0)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[0]["paint"] == {"circle-opacity": 0}


def test_a_renderer_reference_scale_is_reported():
    layer = make_layer()
    renderer = QgsSingleSymbolRenderer(marker(color="#ff0000", outline_style="no"))
    renderer.setReferenceScale(25000)
    layer.setRenderer(renderer)
    assert convert(layer)[1] == [
        loss("unit_not_representable", "renderer", "reference scale sizing")
    ]


def test_a_square_marker_is_drawn_as_a_circle_and_reported():
    layer = make_layer()
    layer.setRenderer(QgsSingleSymbolRenderer(marker(name="square", color="#00ff00")))
    entry, losses = convert(layer)
    assert entry["paint"]["circle-color"] == "#00ff00"
    assert [item["kind"] for item in losses] == ["marker_shape_approximated"]


def test_a_font_marker_is_drawn_as_a_circle_and_reported():
    layer = make_layer()
    symbol = QgsMarkerSymbol.createSimple({})
    symbol.changeSymbolLayer(
        0, QgsFontMarkerSymbolLayer("Arial", "A", 8, QColor("#336699"))
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    entry, losses = convert(layer)
    assert entry["paint"] == {"circle-opacity": 1.0, "circle-color": "#336699"}
    assert losses == [loss("icon_not_uploaded", "renderer.symbol", "FontMarker")]


def test_single_polygon_puts_the_outline_in_builder_state():
    layer = make_layer("Polygon")
    symbol = fill("#4daf4a", outline_width="0.5", outline_width_unit="MM")
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    entry, losses = convert(layer)
    assert entry["paint"] == {"fill-opacity": 1.0, "fill-color": "#4daf4a"}
    assert entry["style_config"] == {
        "builder": {"outlineColor": "#222222", "outlineWidth": 1.89}
    }
    assert losses == []


def test_polygon_with_no_fill_or_no_outline_sets_the_disabled_flags():
    layer = make_layer("Polygon")
    layer.setRenderer(QgsSingleSymbolRenderer(fill("#4daf4a", style="no")))
    assert convert(layer)[0]["style_config"]["builder"]["fillDisabled"] is True
    layer.setRenderer(QgsSingleSymbolRenderer(fill("#4daf4a", outline_style="no")))
    assert convert(layer)[0]["style_config"] == {"builder": {"strokeDisabled": True}}


def test_symbol_opacity_over_an_opaque_outline_is_reported():
    layer = make_layer("Polygon")
    symbol = fill("#4daf4a")
    symbol.setOpacity(0.5)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[1] == [
        loss(
            "effect_dropped", "renderer.symbol", "outline dash pattern or transparency"
        )
    ]


def test_data_defined_symbol_opacity_is_reported():
    layer = make_layer("Polygon", fields=(("alpha", "d"),))
    symbol = fill("#4daf4a")
    symbol.setDataDefinedProperty(
        QgsSymbol.Property.Opacity, QgsProperty.fromField("alpha")
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert [item["kind"] for item in convert(layer)[1]] == [
        "expression_not_representable"
    ]


def test_a_polygon_offset_becomes_a_translation_in_pixels():
    layer = make_layer("Polygon")
    symbol = fill("#4daf4a", offset="2,1", offset_unit="MM")
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[0]["paint"]["fill-translate"] == [7.56, 3.78]


def test_zero_width_lines_and_outlines_are_one_pixel_hairlines():
    layer = make_layer("LineString")
    line = QgsLineSymbol.createSimple({"line_width": "0", "line_style": "dash"})
    layer.setRenderer(QgsSingleSymbolRenderer(line))
    paint = convert(layer)[0]["paint"]
    assert paint["line-width"] == 1.0
    polygon = make_layer("Polygon")
    polygon.setRenderer(QgsSingleSymbolRenderer(fill("#4daf4a", outline_width="0")))
    entry, _ = convert(polygon)
    assert entry["style_config"]["builder"]["outlineWidth"] == 1.0


def test_a_layer_without_a_renderer_reports_an_unsupported_renderer():
    layer = make_layer()
    layer.setRenderer(None)
    entry, losses = convert(layer)
    assert entry["paint"] == {}
    assert losses == [loss("unsupported_renderer", "renderer", "none")]


def test_a_zero_width_marker_outline_is_a_one_pixel_hairline():
    layer = make_layer()
    layer.setRenderer(
        QgsSingleSymbolRenderer(marker(color="#ff0000", outline_width="0"))
    )
    assert convert(layer)[0]["paint"]["circle-stroke-width"] == 1.0


def test_a_shadowed_duplicate_class_does_not_set_the_shared_look():
    layer = make_layer("Polygon")
    hidden = fill("#4daf4a")
    hidden.setOpacity(0.0)
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory("a", hidden, "first"),
            QgsRendererCategory("a", fill("#ff0000"), "second"),
        ],
    )
    layer.setRenderer(renderer)
    assert convert(layer)[0]["paint"]["fill-opacity"] == 1.0


def test_a_dashed_outline_is_reported():
    layer = make_layer("Polygon")
    layer.setRenderer(QgsSingleSymbolRenderer(fill("#4daf4a", outline_style="dash")))
    assert convert(layer)[1] == [
        loss(
            "effect_dropped", "renderer.symbol", "outline dash pattern or transparency"
        )
    ]


def test_single_line_converts_width_offset_dash_cap_and_join():
    layer = make_layer("LineString")
    symbol = QgsLineSymbol.createSimple(
        {
            "line_color": "#0000ff",
            "line_width": "1",
            "line_style": "dash",
            "capstyle": "round",
            "joinstyle": "miter",
            "offset": "2",
        }
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    entry, losses = convert(layer)
    assert entry["paint"] == {
        "line-opacity": 1.0,
        "line-width": 3.78,
        "line-offset": 7.56,
        "line-dasharray": [4, 2],
        "line-color": "#0000ff",
    }
    assert entry["layout"] == {"line-cap": "round", "line-join": "miter"}
    assert losses == []


def test_a_line_with_no_pen_is_invisible():
    layer = make_layer("LineString")
    symbol = QgsLineSymbol.createSimple({"line_style": "no"})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[0]["paint"]["line-opacity"] == 0


def test_a_no_pen_class_is_hidden_without_blanking_the_others():
    layer = make_layer("LineString", fields=(("type", "s"),))
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory(
                "a", QgsLineSymbol.createSimple({"line_style": "no"}), "A"
            ),
            QgsRendererCategory(
                "b", QgsLineSymbol.createSimple({"line_color": "#ff0000"}), "B"
            ),
        ],
    )
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["paint"]["line-opacity"] == 1.0
    assert entry["filter"] == ["any", ["in", ["get", "type"], ["literal", ["b"]]]]
    assert losses == []


def test_a_class_with_no_symbol_is_hidden():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory("a", fill("#ff0000"), "A"),
            QgsRendererCategory("b", None, "B"),
        ],
    )
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["filter"] == ["any", ["in", ["get", "type"], ["literal", ["a"]]]]
    assert losses == []


def test_a_fill_with_no_brush_and_no_pen_draws_nothing():
    layer = make_layer("Polygon")
    symbol = fill("#ff0000", style="no", outline_style="no")
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    entry, _ = convert(layer)
    assert entry["paint"] == {"fill-opacity": 0}


def test_a_translucent_cluster_text_layer_is_reported():
    layer = pop_layer()
    renderer = QgsPointClusterRenderer()
    renderer.setEmbeddedRenderer(QgsSingleSymbolRenderer(colored("#ff0000")))
    cluster = marker(color="#0000ff", outline_style="no")
    cluster.appendSymbolLayer(
        QgsFontMarkerSymbolLayer("Arial", "1", 8, QColor(255, 255, 255, 90))
    )
    renderer.setClusterSymbol(cluster)
    layer.setRenderer(renderer)
    assert convert(layer)[1] == [
        CLUSTER_SIZE,
        loss("effect_dropped", "renderer.clusterSymbol", "cluster text transparency"),
    ]


def test_date_class_values_become_iso_text():
    layer = make_layer("Polygon", fields=(("day", "s"),))
    renderer = QgsCategorizedSymbolRenderer(
        "day", [QgsRendererCategory(QDate(2026, 10, 13), fill("#4daf4a"), "Today")]
    )
    layer.setRenderer(renderer)
    entry, _ = convert(layer)
    assert entry["style_config"]["categories"][0]["value"] == "2026-10-13"
    json.dumps(entry)


def test_a_custom_dash_is_measured_in_line_widths():
    layer = make_layer("LineString")
    symbol = QgsLineSymbol.createSimple(
        {
            "line_width": "0.5",
            "use_custom_dash": "1",
            "customdash": "2;1",
            "customdash_unit": "MM",
        }
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[0]["paint"]["line-dasharray"] == [4.0, 2.0]


def test_extra_symbol_layers_are_reported_dropped():
    layer = make_layer("Polygon")
    symbol, other = fill("#4daf4a"), fill("#ff0000")
    symbol.appendSymbolLayer(other.symbolLayer(0).clone())
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    assert convert(layer)[1] == [
        loss("symbol_layer_dropped", "renderer.symbol", "SimpleFill")
    ]


# Categorized


def city_state_renderer(null_color="#999999", other_color="#999999", **flags):
    def category(value, color, label):
        symbol = fill(color)
        return QgsRendererCategory(value, symbol, label, flags.get(label, True))

    return QgsCategorizedSymbolRenderer(
        "type",
        [
            category("city", "#4daf4a", "City park"),
            category("state", "#377eb8", "State park"),
            category(None, null_color, "null"),
            category("", other_color, "all other values"),
        ],
    )


CATEGORIZED_CLASSES = [
    {"value": "city", "label": "City park", "color": "#4daf4a"},
    {"value": "state", "label": "State park", "color": "#377eb8"},
]


def test_categorized_describes_classes_and_compiles_the_builders_match():
    layer = make_layer("Polygon")
    layer.setRenderer(city_state_renderer())
    entry, losses = convert(layer)
    assert entry["style_config"] == {
        "mode": "categorical",
        "column": "type",
        "categories": CATEGORIZED_CLASSES,
        "builder": {"outlineColor": "#222222", "outlineWidth": 0.98},
    }
    assert entry["paint"] == {
        "fill-opacity": 1.0,
        "fill-color": [
            "case",
            ["==", ["get", "type"], None],
            "#999999",
            [
                "match",
                ["get", "type"],
                "city",
                "#4daf4a",
                "state",
                "#377eb8",
                "#999999",
            ],
        ],
    }
    assert entry["filter"] is None
    assert losses == []


def drawn_colors(layer, renderer):
    """What QGIS draws for 'a', an unlisted value, NULL and ''."""
    layer.setRenderer(renderer)
    for value in ("a", "zzz", None, ""):
        feature = QgsFeature(layer.fields())
        feature.setAttributes([value])
        layer.dataProvider().addFeature(feature)
    context = QgsRenderContext()
    renderer.startRender(context, layer.fields())
    colors = []
    for feature in layer.getFeatures():
        symbol = renderer.symbolForFeature(feature, context)
        colors.append(symbol.color().name() if symbol else None)
    renderer.stopRender(context)
    return colors


@pytest.mark.parametrize(
    ("catch_alls", "catch_all_color"),
    [
        ([(None, "#ff0000")], "#ff0000"),
        ([("", "#0000ff")], "#0000ff"),
        ([(None, "#ff0000"), ("", "#0000ff")], "#0000ff"),
        ([("", "#0000ff"), (None, "#ff0000")], "#ff0000"),
        ([], None),
    ],
)
def test_a_null_class_and_an_empty_class_are_one_catch_all_and_the_last_wins(
    catch_alls, catch_all_color
):
    layer = make_layer("Polygon")
    categories = [QgsRendererCategory("a", fill("#4daf4a"), "A")] + [
        QgsRendererCategory(value, fill(color), "rest") for value, color in catch_alls
    ]
    renderer = QgsCategorizedSymbolRenderer("type", categories)
    assert drawn_colors(layer, renderer) == [
        "#4daf4a",
        catch_all_color,
        catch_all_color,
        catch_all_color,
    ]
    entry, losses = convert(layer)
    assert losses == []
    assert entry["style_config"]["categories"] == [
        {"value": "a", "label": "A", "color": "#4daf4a"}
    ]
    if catch_all_color:
        assert entry["paint"]["fill-color"] == [
            "case",
            ["==", ["get", "type"], None],
            catch_all_color,
            ["match", ["get", "type"], "a", "#4daf4a", catch_all_color],
        ]
        assert entry["filter"] is None
    else:
        assert entry["filter"] == ["any", ["in", ["get", "type"], ["literal", ["a"]]]]


def test_a_graduated_renderer_draws_no_nulls_and_the_filter_hides_them():
    layer = pop_layer(values=(5.0, None, -3.0, 5000.0))
    renderer = graduated("pop", [colored(c) for c in COLORS[:2]], [0, 10, 100])
    layer.setRenderer(renderer)
    context = QgsRenderContext()
    renderer.startRender(context, layer.fields())
    drawn = [
        renderer.symbolForFeature(feature, context) is not None
        for feature in layer.getFeatures()
    ]
    renderer.stopRender(context)
    assert drawn == [True, False, False, False]
    assert convert(layer)[0]["filter"] == [
        "all",
        [">=", ["get", "pop"], 0.0],
        ["<=", ["get", "pop"], 100.0],
    ]


def test_without_an_all_other_class_the_fallback_is_grey():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type", [QgsRendererCategory("city", fill("#4daf4a"), "City park")]
    )
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["paint"]["fill-color"] == [
        "case",
        ["==", ["get", "type"], None],
        GREY,
        ["match", ["get", "type"], "city", "#4daf4a", GREY],
    ]
    assert entry["filter"] == ["any", ["in", ["get", "type"], ["literal", ["city"]]]]
    assert losses == []


def test_boolean_classes_match_on_their_text():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory(True, fill("#4daf4a"), "Yes"),
            QgsRendererCategory(False, fill("#377eb8"), "No"),
        ],
    )
    layer.setRenderer(renderer)
    entry, _ = convert(layer)
    assert entry["paint"]["fill-color"][3] == [
        "match",
        ["to-string", ["get", "type"]],
        "true",
        "#4daf4a",
        "false",
        "#377eb8",
        GREY,
    ]
    assert [c["value"] for c in entry["style_config"]["categories"]] == [
        "true",
        "false",
    ]


def test_a_class_with_nothing_to_draw_is_hidden_and_keeps_a_valid_colour():
    layer = make_layer("Polygon")
    empty = fill("#4daf4a")
    empty.symbolLayer(0).setEnabled(False)
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory("a", fill("#ff0000"), "A"),
            QgsRendererCategory("b", empty, "B"),
        ],
    )
    layer.setRenderer(renderer)
    entry, _ = convert(layer)
    assert entry["style_config"]["categories"][1] == {
        "value": "b",
        "label": "B",
        "color": GREY,
    }
    assert entry["filter"] == ["any", ["in", ["get", "type"], ["literal", ["a"]]]]


def test_an_empty_first_class_does_not_blank_the_layer_look():
    layer = make_layer("Polygon")
    empty = fill("#4daf4a")
    empty.symbolLayer(0).setEnabled(False)
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory("a", empty, "A"),
            QgsRendererCategory("b", fill("#ff0000"), "B"),
        ],
    )
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["paint"]["fill-opacity"] == 1.0
    assert losses == []


def test_booleans_inside_a_multi_value_class_match_on_text():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type", [QgsRendererCategory([True, False], fill("#4daf4a"), "Any")]
    )
    layer.setRenderer(renderer)
    assert convert(layer)[0]["paint"]["fill-color"][3][1] == [
        "to-string",
        ["get", "type"],
    ]


def test_a_hidden_boolean_class_filters_on_its_text():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory(True, fill("#4daf4a"), "Yes"),
            QgsRendererCategory(False, fill("#377eb8"), "No", False),
            QgsRendererCategory("", fill("#999999"), "other"),
        ],
    )
    layer.setRenderer(renderer)
    assert convert(layer)[0]["filter"] == [
        "all",
        ["!=", ["to-string", ["get", "type"]], "false"],
    ]


def test_the_last_class_for_a_repeated_value_wins():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory("a", fill("#ff0000"), "first"),
            QgsRendererCategory("a", fill("#00ff00"), "second"),
        ],
    )
    layer.setRenderer(renderer)
    assert convert(layer)[0]["style_config"]["categories"] == [
        {"value": "a", "label": "second", "color": "#00ff00"}
    ]


def test_a_class_holding_several_values_expands_to_one_class_each():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type", [QgsRendererCategory(["a", "b"], fill("#4daf4a"), "A or B")]
    )
    layer.setRenderer(renderer)
    assert convert(layer)[0]["style_config"]["categories"] == [
        {"value": "a", "label": "A or B", "color": "#4daf4a"},
        {"value": "b", "label": "A or B", "color": "#4daf4a"},
    ]


def test_switched_off_classes_are_hidden_by_a_filter():
    layer = make_layer("Polygon")
    layer.setRenderer(city_state_renderer(**{"State park": False}))
    assert convert(layer)[0]["filter"] == ["all", ["!=", ["get", "type"], "state"]]


def test_switching_off_the_all_other_class_keeps_only_the_listed_values():
    layer = make_layer("Polygon")
    layer.setRenderer(city_state_renderer(**{"all other values": False}))
    assert convert(layer)[0]["filter"] == [
        "any",
        ["in", ["get", "type"], ["literal", ["city", "state"]]],
    ]


def test_a_class_that_differs_in_more_than_colour_is_reported():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory("city", fill("#4daf4a"), "City"),
            QgsRendererCategory("state", fill("#377eb8", outline_width="2"), "State"),
        ],
    )
    layer.setRenderer(renderer)
    assert convert(layer)[1] == [
        loss(
            "symbol_layer_dropped",
            "renderer.categories[1]",
            "differs in more than colour",
        )
    ]


def test_categories_of_different_sizes_are_reported():
    layer = make_layer()
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory("a", marker(color="#ff0000", size="2"), "A"),
            QgsRendererCategory("b", marker(color="#00ff00", size="6"), "B"),
        ],
    )
    layer.setRenderer(renderer)
    assert convert(layer)[1] == [
        loss(
            "symbol_layer_dropped",
            "renderer.categories[1]",
            "differs in more than colour",
        )
    ]


def test_a_disabled_catch_all_hides_nulls_empty_strings_and_unlisted_values():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory("city", fill("#4daf4a"), "City"),
            QgsRendererCategory("", fill("#999999"), "other", False),
        ],
    )
    layer.setRenderer(renderer)
    assert convert(layer)[0]["filter"] == [
        "any",
        ["in", ["get", "type"], ["literal", ["city"]]],
    ]


def test_a_class_attribute_that_is_an_expression_falls_back_to_the_first_symbol():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        'upper("type")', [QgsRendererCategory("CITY", fill("#4daf4a"), "City")]
    )
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["paint"] == {"fill-opacity": 1.0, "fill-color": "#4daf4a"}
    assert "mode" not in (entry["style_config"] or {})
    assert losses == [
        loss("expression_not_representable", "renderer.classAttribute", 'upper("type")')
    ]


# Graduated


def graduated(column, symbols, edges, method=None, layer=None):
    ranges = [
        QgsRendererRange(low, high, symbol, f"{low} - {high}")
        for (low, high), symbol in zip(zip(edges, edges[1:]), symbols)
    ]
    renderer = QgsGraduatedSymbolRenderer(column, ranges)
    if method is not None:
        renderer.setClassificationMethod(method)
    return renderer


COLORS = ["#fee5d9", "#fb6a4a", "#a50f15"]


def pop_layer(kind="d", values=(5.0, 50.0, 500.0)):
    return make_layer("Point", (("pop", kind),), [(v,) for v in values])


def colored(color):
    return marker(color=color, outline_style="no")


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        (QgsClassificationEqualInterval(), "equal_interval"),
        (QgsClassificationQuantile(), "quantile"),
        (QgsClassificationJenks(), "jenks"),
        (QgsClassificationStandardDeviation(), "std_dev"),
        (QgsClassificationPrettyBreaks(), "manual"),
        (None, "manual"),
    ],
)
def test_graduated_maps_each_classification_method(method, expected):
    layer = pop_layer()
    layer.setRenderer(
        graduated("pop", [colored(c) for c in COLORS], [0, 10, 100, 1000], method)
    )
    entry, losses = convert(layer)
    assert entry["style_config"] == {
        "mode": "graduated",
        "column": "pop",
        "breaks": [NEXT_10, NEXT_100],
        "colors": COLORS,
        "classCount": 3,
        "method": expected,
    }
    assert entry["paint"] == {
        "circle-radius": 3.78,
        "circle-opacity": 1.0,
        "circle-stroke-width": 0,
        "circle-color": [
            "case",
            ["==", ["get", "pop"], None],
            GREY,
            [
                "step",
                ["get", "pop"],
                COLORS[0],
                NEXT_10,
                COLORS[1],
                NEXT_100,
                COLORS[2],
            ],
        ],
    }
    assert entry["filter"] == [
        "all",
        [">=", ["get", "pop"], 0.0],
        ["<=", ["get", "pop"], 1000.0],
    ]
    assert losses == []


def test_graduated_reports_data_that_sits_on_a_class_edge():
    layer = pop_layer(values=(5.0, 10.0, 500.0))
    layer.setRenderer(
        graduated("pop", [colored(c) for c in COLORS], [0, 10, 100, 1000])
    )
    assert convert(layer)[1] == [
        loss("class_edge_shifted", "renderer.ranges", "values equal to a class edge")
    ]


def test_graduated_reports_overlapping_ranges():
    layer = pop_layer()
    ranges = [
        QgsRendererRange(0, 100, colored(COLORS[0]), "wide"),
        QgsRendererRange(10, 20, colored(COLORS[1]), "inside"),
        QgsRendererRange(30, 40, colored(COLORS[2]), "also inside"),
    ]
    layer.setRenderer(QgsGraduatedSymbolRenderer("pop", ranges))
    assert (
        loss(
            "filter_not_representable",
            "renderer.ranges",
            "overlapping ranges (QGIS uses the first match)",
        )
        in convert(layer)[1]
    )


def test_graduated_ranges_that_touch_in_reverse_order_are_overlapping():
    layer = pop_layer()
    ranges = [
        QgsRendererRange(10, 20, colored(COLORS[0]), "high"),
        QgsRendererRange(0, 10, colored(COLORS[1]), "low"),
    ]
    layer.setRenderer(QgsGraduatedSymbolRenderer("pop", ranges))
    assert (
        loss(
            "filter_not_representable",
            "renderer.ranges",
            "overlapping ranges (QGIS uses the first match)",
        )
        in convert(layer)[1]
    )


def test_graduated_ranges_that_only_touch_are_not_overlapping():
    layer = pop_layer()
    layer.setRenderer(
        graduated("pop", [colored(c) for c in COLORS], [0, 10, 100, 1000])
    )
    assert all(item["kind"] != "filter_not_representable" for item in convert(layer)[1])


def test_graduated_on_an_integer_field_moves_each_edge_up_by_one():
    layer = pop_layer("i", values=(5, 10, 500))
    layer.setRenderer(
        graduated("pop", [colored(c) for c in COLORS], [0, 10, 100, 1000])
    )
    entry, losses = convert(layer)
    assert entry["style_config"]["breaks"] == [11, 101]
    assert losses == [
        loss("class_edge_shifted", "renderer.ranges", "values equal to a class edge")
    ]


def test_a_fractional_edge_on_an_integer_field_needs_no_shift():
    layer = pop_layer("i", values=(5, 100, 500))
    layer.setRenderer(
        graduated("pop", [colored(c) for c in COLORS], [0, 10.5, 100, 1000])
    )
    entry, losses = convert(layer)
    assert entry["style_config"]["breaks"] == [10.5, 101]
    assert losses == [
        loss("class_edge_shifted", "renderer.ranges", "values equal to a class edge")
    ]


def test_graduated_size_drives_the_radius_when_colour_is_constant():
    layer = pop_layer()
    symbols = [
        marker(color="#ff0000", size=str(s), outline_style="no") for s in (2, 4, 8)
    ]
    layer.setRenderer(graduated("pop", symbols, [0, 10, 100, 1000]))
    entry, losses = convert(layer)
    assert entry["style_config"] == {
        "mode": "graduated",
        "column": "pop",
        "breaks": [NEXT_10, NEXT_100],
        "colors": ["#ff0000"] * 3,
        "classCount": 3,
        "method": "manual",
        "target": "radius",
        "sizes": [3.78, 7.56, 15.12],
        "sizeRange": [3.78, 15.12],
    }
    assert entry["paint"]["circle-radius"] == [
        "case",
        ["==", ["get", "pop"], None],
        0,
        ["step", ["get", "pop"], 3.78, NEXT_10, 7.56, NEXT_100, 15.12],
    ]
    assert entry["paint"]["circle-color"] == "#ff0000"
    assert losses == []


def test_a_switched_off_range_does_not_decide_the_graduation_target():
    layer = pop_layer()
    symbols = [
        marker(color=c, size=str(s), outline_style="no")
        for c, s in (("#ff0000", 2), ("#ff0000", 4), ("#00ff00", 8))
    ]
    renderer = graduated("pop", symbols, [0, 10, 100, 1000])
    renderer.updateRangeRenderState(2, False)
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["style_config"]["target"] == "radius"
    assert losses == []


def test_graduated_size_and_colour_together_keep_the_colour():
    layer = pop_layer()
    symbols = [
        marker(color=c, size=str(s), outline_style="no")
        for c, s in zip(COLORS, (2, 4, 8))
    ]
    layer.setRenderer(graduated("pop", symbols, [0, 10, 100, 1000]))
    entry, losses = convert(layer)
    assert "target" not in entry["style_config"]
    assert entry["paint"]["circle-radius"] == 3.78
    assert losses == [loss("unsupported_renderer", "renderer", "graduated size+color")]


def test_a_switched_off_class_does_not_supply_the_shared_look():
    layer = make_layer("Polygon")
    renderer = QgsCategorizedSymbolRenderer(
        "type",
        [
            QgsRendererCategory("a", fill("#ff0000", outline_width="3"), "A", False),
            QgsRendererCategory("b", fill("#00ff00"), "B"),
        ],
    )
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["style_config"]["builder"]["outlineWidth"] == 0.98
    assert losses == []


def test_a_single_range_is_a_constant_not_an_empty_step():
    layer = pop_layer()
    layer.setRenderer(graduated("pop", [colored("#fee5d9")], [0, 1000]))
    entry, _ = convert(layer)
    assert entry["style_config"]["breaks"] == []
    assert entry["paint"]["circle-color"] == [
        "case",
        ["==", ["get", "pop"], None],
        GREY,
        "#fee5d9",
    ]


def test_classes_with_an_unsized_marker_do_not_become_a_size_ramp():
    layer = pop_layer()
    font = QgsMarkerSymbol.createSimple({})
    font.changeSymbolLayer(
        0, QgsFontMarkerSymbolLayer("Arial", "A", 8, QColor("#ff0000"))
    )
    plain = marker(color="#ff0000", outline_style="no")
    layer.setRenderer(graduated("pop", [plain, font], [0, 10, 100]))
    entry, losses = convert(layer)
    assert "target" not in entry["style_config"]
    assert "icon_not_uploaded" in [item["kind"] for item in losses]


def test_switched_off_ranges_are_hidden_and_the_shared_edge_stays_with_the_lower_range():
    layer = pop_layer()
    renderer = graduated("pop", [colored(c) for c in COLORS], [0, 10, 100, 1000])
    renderer.updateRangeRenderState(1, False)
    layer.setRenderer(renderer)
    value = ["get", "pop"]
    assert convert(layer)[0]["filter"] == [
        "any",
        ["all", [">=", value, 0.0], ["<=", value, 10.0]],
        ["all", [">", value, 100.0], ["<=", value, 1000.0]],
    ]


# Heatmap, cluster, 2.5D and what GeoLens can't draw


def gradient(colors):
    stops = [QgsGradientStop(i / 4, QColor(c)) for i, c in enumerate(colors[1:-1], 1)]
    return QgsGradientColorRamp(QColor(colors[0]), QColor(colors[-1]), False, stops)


def heatmap(colors, weight=""):
    renderer = QgsHeatmapRenderer()
    renderer.setColorRamp(gradient(colors))
    renderer.setRadius(20)
    renderer.setRadiusUnit(Qgis.RenderUnit.Pixels)
    renderer.setWeightExpression(weight)
    return renderer


def density(*colors):
    expression = ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(0,0,0,0)"]
    for at, color in zip((0.2, 0.4, 0.6, 0.8, 1.0), colors):
        expression += [at, color]
    return expression


def test_heatmap_with_a_named_ramp_and_a_weight_field():
    layer = pop_layer()
    layer.setRenderer(heatmap(ramp_colors("YlOrRd"), weight="pop"))
    entry, losses = convert(layer)
    assert entry["style_config"] == {
        "render_mode": "heatmap",
        "builder": {"heatmapRamp": "YlOrRd", "heatmapWeightColumn": "pop"},
    }
    assert entry["paint"] == {
        "heatmap-radius": 20.0,
        "heatmap-color": density("#ffffcc", "#fed976", "#fd8d3c", "#e31a1c", "#800026"),
        "heatmap-weight": ["get", "pop"],
    }
    assert losses == []


def test_heatmap_with_a_reversed_ramp_sets_the_reversed_flag():
    layer = pop_layer()
    layer.setRenderer(heatmap(ramp_colors("Blues", reversed_=True)))
    entry, losses = convert(layer)
    assert entry["style_config"]["builder"] == {
        "heatmapRamp": "Blues",
        "heatmapReversed": True,
    }
    assert entry["paint"]["heatmap-color"][6] == "#08306b"
    assert losses == []


def test_heatmap_with_a_custom_ramp_is_drawn_with_the_nearest_named_one():
    layer = pop_layer()
    layer.setRenderer(heatmap(["#ff00ff", "#00ffff", "#ffff00", "#00ff00", "#0000ff"]))
    entry, losses = convert(layer)
    assert [item["kind"] for item in losses] == ["raster_renderer_approximated"]
    assert losses[0]["path"] == f"{PATH}.renderer.colorRamp"
    assert (
        losses[0]["detail"]
        == f"drawn with {entry['style_config']['builder']['heatmapRamp']}"
    )


def test_a_data_defined_heatmap_radius_is_reported():
    layer = pop_layer()
    renderer = heatmap(ramp_colors("Blues"))
    renderer.setDataDefinedProperty(
        QgsFeatureRenderer.Property.HeatmapRadius, QgsProperty.fromField("pop")
    )
    layer.setRenderer(renderer)
    assert [item["kind"] for item in convert(layer)[1]] == [
        "expression_not_representable"
    ]


def test_a_heatmap_without_a_ramp_uses_the_default_one():
    layer = pop_layer()
    renderer = heatmap(ramp_colors("Blues"))
    renderer.setColorRamp(None)
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["style_config"]["builder"]["heatmapRamp"] == "YlOrRd"
    assert [item["kind"] for item in losses] == ["raster_renderer_approximated"]


def test_a_ramp_that_starts_translucent_is_reported():
    layer = pop_layer()
    colors = ramp_colors("Blues")
    first = QColor(colors[0])
    first.setAlpha(0)
    ramp = gradient(colors)
    ramp.setColor1(first)
    renderer = heatmap(colors)
    renderer.setColorRamp(ramp)
    layer.setRenderer(renderer)
    assert [item["kind"] for item in convert(layer)[1]] == [
        "raster_renderer_approximated"
    ]


def test_a_discrete_heatmap_ramp_is_reported():
    layer = pop_layer()
    colors = ramp_colors("Blues")
    ramp = gradient(colors)
    ramp.setDiscrete(True)
    renderer = heatmap(colors)
    renderer.setColorRamp(ramp)
    layer.setRenderer(renderer)
    assert [item["kind"] for item in convert(layer)[1]] == [
        "raster_renderer_approximated"
    ]


def test_a_fixed_heatmap_maximum_is_reported():
    layer = pop_layer()
    renderer = heatmap(ramp_colors("Blues"))
    renderer.setMaximumValue(50)
    layer.setRenderer(renderer)
    assert convert(layer)[1] == [
        loss(
            "raster_renderer_approximated",
            "renderer.maximumValue",
            "fixed maximum value not carried over",
        )
    ]


def test_a_translucent_heatmap_ramp_is_reported_even_when_the_colours_match():
    layer = pop_layer()
    colors = ramp_colors("Blues")
    middle = QColor(colors[2])
    middle.setAlpha(90)
    stops = [
        QgsGradientStop(0.25, QColor(colors[1])),
        QgsGradientStop(0.5, middle),
        QgsGradientStop(0.75, QColor(colors[3])),
    ]
    ramp = QgsGradientColorRamp(QColor(colors[0]), QColor(colors[-1]), False, stops)
    renderer = QgsHeatmapRenderer()
    renderer.setColorRamp(ramp)
    layer.setRenderer(renderer)
    assert "raster_renderer_approximated" in [
        item["kind"] for item in convert(layer)[1]
    ]


def test_a_heatmap_weight_expression_is_reported():
    layer = pop_layer()
    layer.setRenderer(heatmap(ramp_colors("Blues"), weight='"pop" * 2'))
    entry, losses = convert(layer)
    assert "heatmap-weight" not in entry["paint"]
    assert losses == [
        loss("expression_not_representable", "renderer.weightExpression", '"pop" * 2')
    ]


def test_cluster_keeps_the_radius_the_cluster_colours_and_the_point_symbol():
    layer = pop_layer()
    renderer = QgsPointClusterRenderer()
    renderer.setEmbeddedRenderer(
        QgsSingleSymbolRenderer(marker(color="#ff0000", size="3", outline_style="no"))
    )
    renderer.setTolerance(20)
    renderer.setToleranceUnit(Qgis.RenderUnit.Pixels)
    cluster = marker(color="#0000ff", size="6", outline_style="no")
    cluster.appendSymbolLayer(
        QgsFontMarkerSymbolLayer("Arial", "1", 8, QColor("#ffffff"))
    )
    renderer.setClusterSymbol(cluster)
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["style_config"] == {
        "render_mode": "cluster",
        "builder": {
            "clusterRadius": 20.0,
            "clusterColor": "#0000ff",
            "clusterTextColor": "#ffffff",
        },
    }
    assert entry["paint"] == {
        "circle-radius": 5.67,
        "circle-opacity": 1.0,
        "circle-stroke-width": 0,
        "circle-color": "#ff0000",
    }
    assert losses == [CLUSTER_SIZE]


def test_extra_cluster_symbol_layers_are_reported_as_dropped():
    layer = pop_layer()
    renderer = QgsPointClusterRenderer()
    renderer.setEmbeddedRenderer(QgsSingleSymbolRenderer(colored("#ff0000")))
    cluster = marker(color="#0000ff", outline_style="no")
    cluster.appendSymbolLayer(
        QgsSimpleMarkerSymbolLayer.create({"color": "#00ff00", "size": "3"})
    )
    cluster.appendSymbolLayer(
        QgsFontMarkerSymbolLayer("Arial", "1", 8, QColor("#ffffff"))
    )
    cluster.appendSymbolLayer(
        QgsSimpleMarkerSymbolLayer.create({"color": "#ffff00", "size": "4"})
    )
    renderer.setClusterSymbol(cluster)
    layer.setRenderer(renderer)
    assert convert(layer)[1] == [
        loss("symbol_layer_dropped", "renderer.clusterSymbol[1]", "SimpleMarker"),
        loss("symbol_layer_dropped", "renderer.clusterSymbol[3]", "SimpleMarker"),
        CLUSTER_SIZE,
    ]


def test_a_translucent_cluster_symbol_is_reported():
    layer = pop_layer()
    renderer = QgsPointClusterRenderer()
    renderer.setEmbeddedRenderer(QgsSingleSymbolRenderer(colored("#ff0000")))
    cluster = marker(color="#0000ff", outline_style="no")
    cluster.setOpacity(0.5)
    renderer.setClusterSymbol(cluster)
    layer.setRenderer(renderer)
    assert convert(layer)[1] == [
        CLUSTER_SIZE,
        loss("effect_dropped", "renderer.clusterSymbol", "cluster symbol transparency"),
    ]


def test_a_non_circle_or_icon_cluster_symbol_is_reported():
    for symbol, kind in (
        (marker(name="square", color="#0000ff"), "marker_shape_approximated"),
        (QgsMarkerSymbol.createSimple({}), "icon_not_uploaded"),
    ):
        if kind == "icon_not_uploaded":
            symbol.changeSymbolLayer(
                0, QgsFontMarkerSymbolLayer("Arial", "A", 8, QColor("#0000ff"))
            )
        layer = pop_layer()
        renderer = QgsPointClusterRenderer()
        renderer.setEmbeddedRenderer(QgsSingleSymbolRenderer(colored("#ff0000")))
        renderer.setClusterSymbol(symbol)
        layer.setRenderer(renderer)
        kinds = [item["kind"] for item in convert(layer)[1]]
        assert kind in kinds


def test_a_disabled_cluster_symbol_layer_is_ignored():
    layer = pop_layer()
    renderer = QgsPointClusterRenderer()
    renderer.setEmbeddedRenderer(QgsSingleSymbolRenderer(colored("#ff0000")))
    cluster = marker(color="#0000ff", outline_style="no")
    cluster.symbolLayer(0).setEnabled(False)
    renderer.setClusterSymbol(cluster)
    layer.setRenderer(renderer)
    assert "clusterColor" not in convert(layer)[0]["style_config"]["builder"]


def test_25d_keeps_the_roof_colour_and_reports_the_walls_and_height():
    layer = make_layer("Polygon")
    renderer = Qgs25DRenderer()
    renderer.setRoofColor(QColor("#aa5500"))
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert entry["paint"] == {"fill-color": "#aa5500", "fill-opacity": 1.0}
    assert entry["style_config"] is None
    assert losses == [
        loss(
            "effect_dropped",
            "renderer",
            "2.5D walls, shadow and constant extrusion height",
        )
    ]


def test_an_unsupported_renderer_falls_back_to_a_single_symbol():
    layer = pop_layer()
    renderer = QgsPointDisplacementRenderer()
    renderer.setEmbeddedRenderer(QgsSingleSymbolRenderer(colored("#ff0000")))
    layer.setRenderer(renderer)
    entry, losses = convert(layer)
    assert "circle-color" in entry["paint"]
    assert entry["style_config"] is None
    assert losses == [loss("unsupported_renderer", "renderer", "pointDisplacement")]


# The layer around the renderer


def test_scale_based_visibility_becomes_a_zoom_range():
    layer = make_layer()
    layer.setScaleBasedVisibility(True)
    layer.setMinimumScale(100000)
    layer.setMaximumScale(5000)
    entry, losses = convert(layer)
    assert (entry["min_zoom"], entry["max_zoom"]) == (11.5, 15.9)
    assert losses == []


def test_a_zoom_range_outside_the_map_is_no_range_unless_it_hides_the_layer():
    layer = make_layer()
    layer.setScaleBasedVisibility(True)
    layer.setMinimumScale(5e9)
    layer.setMaximumScale(0)
    entry, losses = convert(layer)
    assert (entry["min_zoom"], entry["max_zoom"]) == (None, None)
    assert losses == []
    layer.setMinimumScale(2)
    layer.setMaximumScale(1)
    entry, losses = convert(layer)
    assert (entry["min_zoom"], entry["max_zoom"]) == (24, None)
    assert losses == [
        loss("scale_range_clamped", "min_zoom", "zoom 27.1 is beyond 24"),
    ]


def test_layer_opacity_visibility_and_blend_mode():
    layer = make_layer()
    layer.setOpacity(0.8)
    layer.setBlendMode(QPainter.CompositionMode.CompositionMode_Multiply)
    entry, losses = convert(layer, visible=False)
    assert (entry["opacity"], entry["visible"]) == (0.8, False)
    assert losses == [loss("blend_mode_dropped", "blendMode")]


def test_hidden_form_fields_are_left_out_of_the_popup():
    layer = make_layer(fields=(("type", "s"), ("secret", "s"), ("name", "s")))
    layer.setEditorWidgetSetup(1, QgsEditorWidgetSetup("Hidden", {}))
    assert convert(layer)[0]["popup_config"] == popup(["type", "name"])


def test_field_names_over_the_manifest_limit_are_left_out_of_the_popup():
    long_name = "f" * 129
    layer = make_layer(fields=(("type", "s"), ("secret", "s"), (long_name, "s")))
    layer.setEditorWidgetSetup(1, QgsEditorWidgetSetup("Hidden", {}))
    entry, losses = convert(layer)
    assert entry["popup_config"] == popup(["type"])
    assert losses == [
        loss(
            "expression_not_representable",
            "popup_config",
            "field names longer than 128 characters left out",
        )
    ]


def test_more_visible_fields_than_the_manifest_allows_are_reported():
    names = [f"f{i}" for i in range(102)]
    layer = make_layer(fields=tuple((name, "s") for name in names))
    layer.setEditorWidgetSetup(0, QgsEditorWidgetSetup("Hidden", {}))
    entry, losses = convert(layer)
    assert entry["popup_config"] == popup(names[1:101])
    assert losses == [
        loss(
            "expression_not_representable",
            "popup_config",
            "1 fields beyond 100 left out",
        )
    ]


def test_a_dataset_id_replaces_the_dataset_key():
    entry, _ = convert_layer(
        make_layer(), key="parks", dataset_id="5d1f0e2a-7c3b-4a9d-b2e1-0c8f6a4d3b21"
    )
    jsonschema.validate(entry, LAYER_SCHEMA)
    assert "dataset_key" not in entry


def test_exactly_one_dataset_reference_is_required():
    for kwargs in (
        {},
        {"dataset_key": "qgis-1", "dataset_id": "5d1f0e2a-7c3b-4a9d-b2e1-0c8f6a4d3b21"},
    ):
        with pytest.raises(ValueError):
            convert_layer(make_layer(), key="parks", **kwargs)


def test_only_vector_layers_convert():
    with pytest.raises(TypeError):
        convert_layer(QgsRasterLayer("", "dem"), key="dem", dataset_key="qgis-2")
