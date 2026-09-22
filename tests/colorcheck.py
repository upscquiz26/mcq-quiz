"""
Colour checks for the charts, in Python.

The dataviz method's own validator (validate_palette.js) needs Node, which this machine doesn't have, so this
reimplements the parts of it that the app's charts depend on, with the same maths:

  * WCAG contrast ratio of a mark against the surface it is drawn on            (marks need >= 3:1)
  * Delta E in OKLab x 100 between two colours, for normal vision              (adjacent floor >= 15)
  * the same under protanopia and deuteranopia, using the Machado-Oliveira-Fernandes (2009) matrices at
    severity 1.0                                                                (target >= 8, floor >= 6)
  * OKLCH lightness / chroma of a colour                                         (band ~0.43-0.77, chroma >= ~0.10)

It is not a substitute for running the real script if Node is ever installed, but it uses the same definitions.
"""
from math import atan2, cbrt, degrees, hypot


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_to_linear(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(_srgb_to_linear(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))


def luminance(hex_color: str) -> float:
    r, g, b = hex_to_linear(hex_color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, 1..21."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def linear_to_oklab(rgb) -> tuple[float, float, float]:
    r, g, b = rgb
    l = cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def oklch(hex_color: str) -> tuple[float, float, float]:
    L, a, b = linear_to_oklab(hex_to_linear(hex_color))
    return L, hypot(a, b), degrees(atan2(b, a)) % 360


PROTAN = ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998))
DEUTAN = ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881))


def _simulate(rgb, matrix):
    return tuple(max(0.0, min(1.0, sum(matrix[i][j] * rgb[j] for j in range(3)))) for i in range(3))


def delta_e(a: str, b: str, matrix=None) -> float:
    """Euclidean distance in OKLab x 100, optionally after simulating a colour-vision deficiency."""
    ca, cb = hex_to_linear(a), hex_to_linear(b)
    if matrix is not None:
        ca, cb = _simulate(ca, matrix), _simulate(cb, matrix)
    la, lb = linear_to_oklab(ca), linear_to_oklab(cb)
    return 100 * hypot(hypot(la[0] - lb[0], la[1] - lb[1]), la[2] - lb[2])


def report(a: str, b: str, surface: str) -> dict:
    """Everything the charts' accent-vs-grey pair is held to."""
    return {
        "contrast_a": round(contrast(a, surface), 2), "contrast_b": round(contrast(b, surface), 2),
        "normal": round(delta_e(a, b), 1),
        "protanopia": round(delta_e(a, b, PROTAN), 1), "deuteranopia": round(delta_e(a, b, DEUTAN), 1),
        "lightness_a": round(oklch(a)[0], 3), "chroma_a": round(oklch(a)[1], 3),
    }
