"""The Struck brand palette (design/brand/BRAND.md): no blue accents.

Near-neutral cool greys (channel spread <= 12, e.g. brand SILVER ``#c0c3c8``)
are not blue; a blue-dominant colour beyond that spread is rejected.
"""

from __future__ import annotations

import re

from struck.ui import theme

_HEX = re.compile(r"#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})")
_RGBA = re.compile(r"rgba?\((\d+),\s*(\d+),\s*(\d+)")


def _rgb(text: str) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for m in _HEX.finditer(text):
        out.append((int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)))
    for m in _RGBA.finditer(text):
        out.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return out


def test_no_colour_anywhere_is_blue_dominant() -> None:
    sources = [*theme.BASE_COLORS.values(), *theme.palette().values(), theme.app_qss()]
    sources += [v for level in theme.SEMANTIC.values() for v in level.values()]
    colours = [c for s in sources for c in _rgb(s)]
    assert len(colours) > 40
    blue = [c for c in colours if c[2] > c[0] and c[2] > c[1] and max(c) - min(c) > 12]
    assert blue == []


def test_logo_is_shipped() -> None:
    assert theme.LOGO_PATH.is_file()
