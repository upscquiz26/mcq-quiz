"""
Server-drawn SVG charts. No JavaScript is needed to READ them (values are labelled directly and every chart has a
table twin in its template); app/static/charts.js only adds hover tooltips.

Built to the data-viz method's mark specs:
  * lines are 2px with round joins; markers are 10px (r = 5) with a 2px ring in the surface colour
  * bars are 16px thick (never over 24px), square at the baseline, 4px rounded at the data end
  * gridlines and axes are solid hairlines, recessive; there are no borders round marks
  * one series -> one colour and no legend box; the value is labelled at the end / the bar tip, never on every point
  * text wears text tokens (ink), never the series colour - identity comes from the coloured mark beside it
  * every mark's hover/focus target is larger than the mark itself

Colours live in CSS (style.css, ".viz") so a single place owns them; this module only emits class names.
Everything that came from data (names, labels) is escaped.
"""
from html import escape
from math import ceil, floor

from markupsafe import Markup

WIDTH = 640
BAR_THICKNESS = 16          # px; the spec caps bars at 24
ROW_HEIGHT = 34             # the whole row is the hover/focus target (>= 24px)
MARKER_RADIUS = 5           # 10px marker; >= 8px per spec
HIT_RADIUS = 14             # a 28px target around each marker
LABEL_LIMIT = 30            # characters before a category name is shortened (the full name is in the tooltip)


def _short(text: str, limit: int = LABEL_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _n(value: float) -> str:
    """A compact SVG coordinate."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def hbar_chart(rows: list[dict], *, chart_id: str, title: str, desc: str, max_value: float = 100.0,
               accent_all: bool = False, label_width: int = 210) -> Markup:
    """A horizontal bar chart, one bar per row.

    rows: {"label": str, "value": float, "value_text": str, "tip": str, "highlight": bool}
    Highlighted bars wear the accent colour and the rest the recessive grey (the 'emphasis' form). With
    accent_all=True every bar wears the accent: one nominal series, one colour, no legend needed.
    """
    left = label_width
    plot_w = WIDTH - left - 78                       # room at the right for the value beside the bar tip
    height = 8 + len(rows) * ROW_HEIGHT + 8
    parts = [
        f'<svg class="viz-svg" viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="{escape(chart_id)}-t {escape(chart_id)}-d">',
        f'<title id="{escape(chart_id)}-t">{escape(title)}</title><desc id="{escape(chart_id)}-d">{escape(desc)}</desc>',
        f'<line class="viz-axis" x1="{left}" x2="{left}" y1="4" y2="{height - 4}"/>',
    ]
    for i, row in enumerate(rows):
        top = 8 + i * ROW_HEIGHT
        mid = top + ROW_HEIGHT / 2
        y = mid - BAR_THICKNESS / 2
        width = max(0.0, min(row["value"], max_value)) / max_value * plot_w if max_value else 0.0
        x1 = left + width
        accent = accent_all or row.get("highlight")
        parts.append(f'<g class="viz-mark" tabindex="0" data-tip="{escape(row["tip"], quote=True)}">')
        parts.append(f'<rect class="viz-hit" x="0" y="{_n(top)}" width="{WIDTH}" height="{ROW_HEIGHT}"/>')
        parts.append(f'<text class="viz-label" x="{left - 12}" y="{_n(mid + 4)}" text-anchor="end">{escape(_short(row["label"]))}</text>')
        if width >= 1:
            r = min(4.0, width / 2, BAR_THICKNESS / 2)
            path = (f"M{_n(left)},{_n(y)} H{_n(x1 - r)} Q{_n(x1)},{_n(y)} {_n(x1)},{_n(y + r)} "
                    f"V{_n(y + BAR_THICKNESS - r)} Q{_n(x1)},{_n(y + BAR_THICKNESS)} {_n(x1 - r)},{_n(y + BAR_THICKNESS)} "
                    f"H{_n(left)} Z")
            parts.append(f'<path class="viz-bar{" accent" if accent else ""}" d="{path}"/>')
        parts.append(f'<text class="viz-value" x="{_n(x1 + 8)}" y="{_n(mid + 4)}">{escape(row["value_text"])}</text>')
        parts.append("</g>")
    parts.append("</svg>")
    return Markup("".join(parts))


def line_chart(points: list[dict], *, chart_id: str, title: str, desc: str, y_step: int = 25,
               y_max: float = 100.0) -> Markup:
    """A single-series line over evenly spaced points (needs at least two).

    points: {"x_label": str, "value": float, "tip": str, "href": str | None}
    The y axis always includes 0 and y_max, and stretches down if a value is negative (a test score can be).
    The last value is labelled directly; every point has a hover/focus target.
    """
    if len(points) < 2:
        raise ValueError("a line chart needs at least two points")
    height, left, right, top, bottom = 260, 46, 76, 14, 36
    plot_w, plot_h = WIDTH - left - right, height - top - bottom
    values = [p["value"] for p in points]
    y_lo = min(0.0, floor(min(values) / y_step) * y_step)
    y_hi = max(y_max, ceil(max(values) / y_step) * y_step)

    def x_at(i: int) -> float:
        return left + i / (len(points) - 1) * plot_w

    def y_at(v: float) -> float:
        return top + (y_hi - v) / (y_hi - y_lo) * plot_h

    cid = escape(chart_id)
    parts = [
        f'<svg class="viz-svg" viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="{cid}-t {cid}-d">',
        f'<title id="{cid}-t">{escape(title)}</title><desc id="{cid}-d">{escape(desc)}</desc>',
    ]
    tick = y_lo
    while tick <= y_hi + 1e-9:
        y = y_at(tick)
        css = "viz-axis" if tick == 0 else "viz-grid"
        parts.append(f'<line class="{css}" x1="{left}" x2="{WIDTH - right}" y1="{_n(y)}" y2="{_n(y)}"/>')
        parts.append(f'<text class="viz-tick" x="{left - 8}" y="{_n(y + 4)}" text-anchor="end">{int(tick)}%</text>')
        tick += y_step

    coords = [(x_at(i), y_at(p["value"])) for i, p in enumerate(points)]
    line = " ".join(f"{'M' if i == 0 else 'L'}{_n(x)},{_n(y)}" for i, (x, y) in enumerate(coords))
    baseline = y_at(max(y_lo, 0.0))
    area = f"{line} L{_n(coords[-1][0])},{_n(baseline)} L{_n(coords[0][0])},{_n(baseline)} Z"
    parts.append(f'<path class="viz-area" d="{area}"/>')
    parts.append(f'<path class="viz-line" d="{line}"/>')
    parts.append(f'<line class="viz-crosshair" x1="0" x2="0" y1="{top}" y2="{top + plot_h}"/>')

    for i, (p, (x, y)) in enumerate(zip(points, coords)):
        tip, href = escape(p["tip"], quote=True), p.get("href")
        opening = (f'<a class="viz-mark" href="{escape(href, quote=True)}" data-tip="{tip}" data-x="{_n(x)}">'
                   if href else f'<g class="viz-mark" tabindex="0" data-tip="{tip}" data-x="{_n(x)}">')
        parts.append(opening)
        parts.append(f'<circle class="viz-hit" cx="{_n(x)}" cy="{_n(y)}" r="{HIT_RADIUS}"/>')
        parts.append(f'<circle class="viz-dot" cx="{_n(x)}" cy="{_n(y)}" r="{MARKER_RADIUS}"/>')
        parts.append("</a>" if href else "</g>")

    last_x, last_y = coords[-1]
    end_text = escape("%s%%" % f"{values[-1]:g}")
    parts.append(f'<text class="viz-endlabel" x="{_n(last_x + 12)}" y="{_n(last_y + 4)}">{end_text}</text>')

    # Dates: only the ends (and the middle if there is room), so labels never collide.
    x_positions = {0: "start", len(points) - 1: "end"}
    if len(points) >= 5:
        x_positions[len(points) // 2] = "middle"
    for i, anchor in sorted(x_positions.items()):            # left to right, which is also the reading order
        parts.append(f'<text class="viz-tick" x="{_n(coords[i][0])}" y="{height - 12}" text-anchor="{anchor}">{escape(points[i]["x_label"])}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))
