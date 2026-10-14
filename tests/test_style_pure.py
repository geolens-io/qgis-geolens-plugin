"""Style conversion helpers that need no QGIS."""

import pytest

from geolens_qgis.publish.style.loss import LOSS_KINDS, LossLog, analyze_code
from geolens_qgis.publish.style.ramps import (
    heatmap_color_expression,
    nearest_ramp,
    ramp_colors,
)
from geolens_qgis.publish.style.units import scale_to_zoom, to_pixels


class Unit:
    def __init__(self, name):
        self.name = name


def convert(value, unit):
    losses = LossLog("layers[0].style")
    return to_pixels(value, Unit(unit), losses, "renderer"), losses.items


def test_millimetres_and_points_become_pixels_at_96_dpi():
    assert convert(1, "Millimeters") == (3.78, [])
    assert convert(0.5, "Millimeters") == (1.89, [])
    assert convert(3, "Points") == (4.0, [])
    assert convert(1, "Inches") == (96, [])
    assert convert(7, "Pixels") == (7.0, [])


@pytest.mark.parametrize("unit", ["MapUnits", "MetersInMapUnits", "Percentage"])
def test_units_without_a_pixel_size_keep_the_number_and_say_so(unit):
    assert convert(10, unit) == (
        10.0,
        [
            {
                "path": "layers[0].style.renderer",
                "kind": "unit_not_representable",
                "detail": f"{unit} kept as pixels",
            }
        ],
    )


def test_scale_denominators_become_zooms():
    assert scale_to_zoom(591657550.5 / 2**9) == 8.0
    assert scale_to_zoom(100000) == 11.5
    assert scale_to_zoom(5000) == 15.9


def test_every_loss_kind_maps_to_an_analyze_row():
    assert analyze_code("unsupported_renderer") == "A061"
    assert analyze_code("external_layer") == "A030"
    assert analyze_code("unsupported_layer_type") == "A030"
    assert analyze_code("non_spatial_layer") == "A032"
    assert analyze_code("file_too_large") == "A010"
    assert analyze_code("public_blocked_by_policy") == "A020"
    assert {analyze_code(kind) for kind in LOSS_KINDS} == {
        "A010",
        "A020",
        "A030",
        "A032",
        "A060",
        "A061",
    }


def test_a_kind_outside_the_published_list_is_refused():
    with pytest.raises(ValueError):
        LossLog("layers[0].style").add("made_up", "renderer")
    with pytest.raises(ValueError):
        analyze_code("local_raster_not_publishable")


def test_identical_loss_items_are_reported_once():
    losses = LossLog("layers[0].style")
    losses.add("effect_dropped", "renderer", "draw effect")
    losses.add("effect_dropped", "renderer", "draw effect")
    assert losses.items == [
        {
            "path": "layers[0].style.renderer",
            "kind": "effect_dropped",
            "detail": "draw effect",
        }
    ]


def test_a_named_ramp_samples_to_the_builder_colours():
    assert ramp_colors("YlOrRd") == [
        "#ffffcc",
        "#fed976",
        "#fd8d3c",
        "#e31a1c",
        "#800026",
    ]
    assert ramp_colors("YlOrRd", reversed_=True)[0] == "#800026"
    assert heatmap_color_expression("YlOrRd") == [
        "interpolate",
        ["linear"],
        ["heatmap-density"],
        0,
        "rgba(0,0,0,0)",
        0.2,
        "#ffffcc",
        0.4,
        "#fed976",
        0.6,
        "#fd8d3c",
        0.8,
        "#e31a1c",
        1.0,
        "#800026",
    ]


def test_the_nearest_ramp_ignores_a_small_error_and_finds_a_reversal():
    assert nearest_ramp(ramp_colors("Blues")) == ("Blues", False, 0.0)
    name, reverse, error = nearest_ramp(ramp_colors("Greens", reversed_=True))
    assert (name, reverse, error) == ("Greens", True, 0.0)
    assert nearest_ramp(["#ffffff", "#ffeeee", "#ff8888", "#ff3333", "#ff0000"])[2] > 6
