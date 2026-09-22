"""
The chart module and its colours, against the data-viz method's rules: thin marks, capped bar thickness, rounded
data-ends anchored to a square baseline, 2px lines, >= 8px markers with a surface ring, no dashed grid, escaped labels,
big hover targets, and colours that clear contrast and colour-blind separation on THIS app's surface.
"""
import os
import re

import pytest

import colorcheck as cc
from app import charts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = open(os.path.join(ROOT, "app", "static", "style.css"), encoding="utf-8").read()


def css_value(name: str) -> str:
    return re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", CSS).group(1)


SURFACE, ACCENT, GREY = css_value("--surface"), css_value("--viz-accent"), css_value("--viz-grey")
INK, INK_SOFT = css_value("--ink"), css_value("--ink-soft")


def rows(*values, highlight=()):
    return [{"label": f"Row {i}", "value": v, "value_text": f"{v:g}%", "tip": f"{v:g}% | Row {i}", "highlight": i in highlight}
            for i, v in enumerate(values)]


def points(*values):
    return [{"x_label": f"{i + 1} Jan", "value": v, "tip": f"{v:g}% | Test {i}", "href": f"/attempts/{i}/result"}
            for i, v in enumerate(values)]


# --------------------------------------------------------------------------- colours

def test_the_colours_clear_contrast_on_the_apps_own_surface():
    assert SURFACE == "#ffffff"                                   # the checks below are only meaningful against this
    assert cc.contrast(ACCENT, SURFACE) >= 3.0                    # marks need 3:1
    assert cc.contrast(GREY, SURFACE) >= 3.0
    assert cc.contrast(INK, SURFACE) >= 7 and cc.contrast(INK_SOFT, SURFACE) >= 4.5      # text tokens are readable


def test_the_accent_sits_in_the_method_s_lightness_and_chroma_band():
    lightness, chroma, _ = cc.oklch(ACCENT)
    assert 0.43 <= lightness <= 0.77 and chroma >= 0.10
    assert ACCENT.lower() == css_value("--accent").lower()        # the charts use the same blue as buttons and links


def test_accent_and_grey_are_easy_to_tell_apart_for_everyone():
    report = cc.report(ACCENT, GREY, SURFACE)
    assert report["normal"] >= 15                                 # the normal-vision floor
    assert report["protanopia"] >= 8 and report["deuteranopia"] >= 8     # the colour-blind target
    assert (report["normal"], report["protanopia"], report["deuteranopia"]) == pytest.approx((19.9, 18.0, 20.0), abs=0.2)


def test_the_colour_maths_is_sane():
    assert cc.contrast("#000000", "#ffffff") == pytest.approx(21, abs=0.01)
    assert cc.contrast("#777777", "#777777") == pytest.approx(1, abs=0.001)
    assert cc.delta_e("#ff0000", "#ff0000") == 0
    assert cc.delta_e("#ff0000", "#00ff00", cc.DEUTAN) < cc.delta_e("#ff0000", "#00ff00")     # red/green collapse for deutan


def test_text_never_wears_the_data_colours():
    for selector in (".viz-tick", ".viz-label", ".viz-value", ".viz-endlabel"):
        rule = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS).group(1)
        assert "viz-accent" not in rule and "viz-grey" not in rule and "fill: var(--ink" in rule, selector


def test_the_mark_specs_are_in_the_stylesheet():
    line = re.search(r"\.viz-line\s*\{([^}]*)\}", CSS).group(1)
    assert "stroke-width: 2" in line and "stroke-linejoin: round" in line and "stroke-linecap: round" in line
    dot = re.search(r"\.viz-dot\s*\{([^}]*)\}", CSS).group(1)
    assert "stroke: var(--viz-surface)" in dot and "stroke-width: 2" in dot               # the 2px surface ring
    assert "opacity: 0.10" in re.search(r"\.viz-area\s*\{([^}]*)\}", CSS).group(1)          # a wash, not a block
    assert "dasharray" not in CSS.split("/* ---- charts")[1].split("/* ---- revision")[0]   # solid hairlines only
    stat = re.search(r"\.viz-stat-value\s*\{([^}]*)\}", CSS).group(1)
    assert "Georgia" not in stat and "tabular-nums" not in stat                            # sans, proportional figures


# --------------------------------------------------------------------------- horizontal bars

def test_bars_are_thin_square_at_the_baseline_and_rounded_at_the_data_end():
    svg = str(charts.hbar_chart(rows(40, 80, 100), chart_id="b", title="t", desc="d"))
    paths = re.findall(r'<path class="viz-bar[^"]*" d="([^"]+)"', svg)
    assert len(paths) == 3
    assert charts.BAR_THICKNESS <= 24
    for d in paths:
        assert d.count("Q") == 2                                            # two rounded corners, both at the data end
        assert d.startswith("M210,")                                         # starts on the baseline (the label column's edge)...
        assert d.endswith("H210 Z")                                          # ...and returns along it with no curve: square there


def test_a_bar_is_16px_thick_and_never_more_than_24():
    svg = str(charts.hbar_chart(rows(50), chart_id="b", title="t", desc="d"))
    d = re.search(r'd="(M[^"]+)"', svg).group(1)
    top = float(re.search(r"^M210,([\d.]+)", d).group(1))
    bottom = float(re.search(r"V([\d.]+) Q", d).group(1)) + 4       # the straight edge stops 4px short (the corner radius)
    assert bottom - top == charts.BAR_THICKNESS == 16


def test_bar_lengths_are_proportional_and_a_full_bar_still_fits_its_label():
    svg = str(charts.hbar_chart(rows(25, 50, 100), chart_id="b", title="t", desc="d", label_width=210))
    ends = [float(x) for x in re.findall(r"H([\d.]+) Q", svg)]      # x where each straight top edge stops (data end - radius)
    lengths = [e - 210 for e in ends]
    assert lengths[1] == pytest.approx(2 * lengths[0], abs=8) and lengths[2] > lengths[1]
    value_x = [float(x) for x in re.findall(r'<text class="viz-value" x="([\d.]+)"', svg)]
    assert all(x < 640 - 30 for x in value_x)                       # the value beside the longest bar is inside the frame
    assert value_x[2] > ends[2]                                     # and sits beyond the bar tip, never on the bar


def test_a_zero_bar_draws_no_bar_but_keeps_its_label_and_value():
    svg = str(charts.hbar_chart(rows(0, 40), chart_id="b", title="t", desc="d"))
    assert svg.count('<path class="viz-bar') == 1
    assert ">0%<" in svg and ">40%<" in svg


def test_emphasis_uses_the_accent_only_for_highlighted_rows():
    svg = str(charts.hbar_chart(rows(30, 70, 45, highlight=(0, 2)), chart_id="b", title="t", desc="d"))
    classes = re.findall(r'<path class="(viz-bar[^"]*)"', svg)
    assert classes == ["viz-bar accent", "viz-bar", "viz-bar accent"]
    every = str(charts.hbar_chart(rows(30, 70), chart_id="b", title="t", desc="d", accent_all=True))
    assert re.findall(r'<path class="(viz-bar[^"]*)"', every) == ["viz-bar accent", "viz-bar accent"]


def test_every_row_is_a_large_focusable_hover_target_carrying_its_tooltip():
    svg = str(charts.hbar_chart(rows(10, 20, 30), chart_id="b", title="t", desc="d"))
    assert svg.count('<g class="viz-mark" tabindex="0" data-tip="') == 3
    for height in re.findall(r'<rect class="viz-hit"[^>]*height="([\d.]+)"', svg):
        assert float(height) >= 24
    assert 'data-tip="20% | Row 1"' in svg


def test_labels_and_tips_from_data_are_escaped():
    evil = [{"label": '<script>alert("x")</script>', "value": 50, "value_text": "<b>50%</b>",
             "tip": '"><img src=x onerror=alert(1)> | <i>detail</i>', "highlight": False}]
    svg = str(charts.hbar_chart(evil, chart_id='c"d', title="<t>", desc="a&b"))
    assert "<script" not in svg and "<img" not in svg and "<b>" not in svg and "<i>" not in svg
    assert "&lt;script&gt;" in svg and "&lt;b&gt;50%&lt;/b&gt;" in svg
    assert 'data-tip=""&gt;' not in svg and "&quot;" in svg
    assert 'id="c&quot;d-t"' in svg


def test_long_names_are_shortened_on_the_chart_but_complete_in_the_tooltip():
    name = "Constitutional provisions on the separation of powers between the Union and the States"
    svg = str(charts.hbar_chart([{"label": name, "value": 40, "value_text": "40%", "tip": f"40% | {name}", "highlight": False}],
                                chart_id="b", title="t", desc="d"))
    shown = re.search(r'<text class="viz-label"[^>]*>([^<]*)<', svg).group(1)
    assert shown.endswith("…") and len(shown) <= charts.LABEL_LIMIT
    assert name in svg                                                # still there, in the tooltip


def test_bar_charts_have_an_accessible_title_and_description_and_no_dashes():
    svg = str(charts.hbar_chart(rows(10), chart_id="acc", title="Accuracy", desc="How you did"))
    assert 'role="img"' in svg and "<title" in svg and "<desc" in svg and 'aria-labelledby="acc-t acc-d"' in svg
    assert "dasharray" not in svg


# --------------------------------------------------------------------------- the line chart

def circles(svg: str, css: str):
    return [(float(cx), float(cy), float(r)) for cx, cy, r in
            re.findall(rf'<circle class="{css}" cx="([\d.\-]+)" cy="([\d.\-]+)" r="([\d.]+)"', svg)]


def test_a_line_needs_at_least_two_points():
    with pytest.raises(ValueError):
        charts.line_chart(points(50), chart_id="l", title="t", desc="d")


def test_the_line_has_one_marker_per_point_of_at_least_8px_and_a_bigger_hit_target():
    svg = str(charts.line_chart(points(40, 60, 80, 55), chart_id="l", title="t", desc="d"))
    dots, hits = circles(svg, "viz-dot"), circles(svg, "viz-hit")
    assert len(dots) == len(hits) == 4
    assert all(r * 2 >= 8 for _, _, r in dots)                         # 10px markers
    assert all(r * 2 >= 24 for _, _, r in hits)                        # a 28px target around each
    assert [(x, y) for x, y, _ in dots] == [(x, y) for x, y, _ in hits]


def test_higher_values_are_drawn_higher_and_points_are_evenly_spaced_left_to_right():
    svg = str(charts.line_chart(points(20, 80, 50), chart_id="l", title="t", desc="d"))
    (x0, y0, _), (x1, y1, _), (x2, y2, _) = circles(svg, "viz-dot")
    assert y1 < y2 < y0                                                # 80 above 50 above 20 (svg y grows downward)
    assert x0 < x1 < x2 and (x1 - x0) == pytest.approx(x2 - x1, abs=0.2)


def test_the_axis_always_shows_zero_and_100_and_stretches_for_negative_scores():
    svg = str(charts.line_chart(points(40, 60), chart_id="l", title="t", desc="d"))
    ticks = [int(t) for t in re.findall(r'class="viz-tick"[^>]*text-anchor="end">(-?\d+)%<', svg)]
    assert ticks == [0, 25, 50, 75, 100]
    negative = str(charts.line_chart(points(-30, 55), chart_id="l", title="t", desc="d"))
    ticks = [int(t) for t in re.findall(r'class="viz-tick"[^>]*text-anchor="end">(-?\d+)%<', negative)]
    assert ticks[0] == -50 and 0 in ticks and ticks[-1] == 100
    assert 'class="viz-axis"' in negative                              # the zero line is drawn as the axis


def test_only_the_final_value_is_labelled_directly_not_every_point():
    svg = str(charts.line_chart(points(31.5, 60, 72), chart_id="l", title="t", desc="d"))
    assert svg.count('class="viz-endlabel"') == 1 and ">72%<" in svg
    assert ">31.5%<" not in svg and ">60%<" not in svg                 # the others live in the tooltip and the table


def test_date_labels_are_only_at_the_ends_and_middle_so_they_cannot_collide():
    few = str(charts.line_chart(points(1, 2, 3), chart_id="l", title="t", desc="d"))
    assert re.findall(r'text-anchor="(?:start|middle|end)">(\d+ Jan)<', few) == ["1 Jan", "3 Jan"]
    many = str(charts.line_chart(points(*range(1, 10)), chart_id="l", title="t", desc="d"))
    assert re.findall(r'text-anchor="(?:start|middle|end)">(\d+ Jan)<', many) == ["1 Jan", "5 Jan", "9 Jan"]


def test_line_marks_are_links_to_the_result_and_carry_their_tooltips():
    svg = str(charts.line_chart(points(50, 70), chart_id="l", title="t", desc="d"))
    assert svg.count('<a class="viz-mark" href="/attempts/') == 2
    assert 'data-tip="70% | Test 1"' in svg and 'data-x="' in svg
    assert '<path class="viz-line"' in svg and '<path class="viz-area"' in svg and 'class="viz-crosshair"' in svg


def test_line_chart_text_from_data_is_escaped():
    pts = [{"x_label": "<b>1 Jan</b>", "value": 10, "tip": '<script>x</script> | "q"', "href": None},
           {"x_label": "2 Jan", "value": 20, "tip": "ok | fine", "href": None}]
    svg = str(charts.line_chart(pts, chart_id="l", title="<t>", desc="d"))
    assert "<script" not in svg and "<b>" not in svg and "&lt;b&gt;1 Jan&lt;/b&gt;" in svg
    assert svg.count('<g class="viz-mark" tabindex="0"') == 2           # without an href a mark is still keyboard-focusable
