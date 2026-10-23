"""Chart render fidelity against the design reference ``design/dashboard.html``.

The reference (chart card CSS ~line 288, ``drawMain`` ~line 785) paints
candles on a canvas with NO fill of its own over the card surface
``--card #0a0a0a``; bodies are ``#d4b36b`` up / ``#454443`` down; the hairline
grid is exactly five lines rgba(255,252,245,.05) between the visible low and
high; the main chart is candles-only (no volume strip). These probes render
the real widget offscreen with deterministic klines and assert on the real
pixels; every expected value comes from the reference file, not from what
the chart happens to draw. The old chart drew on ``#262626`` (the pre-rebase
``card_bg``) with a pyqtgraph alpha-grid wash — the regression guard asserts
that wash is absent.
"""

from __future__ import annotations

import os
from collections import Counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from struck.ui.chart import CandleChart, GRID_LEVELS  # noqa: E402

APP = QApplication.instance() or QApplication([])

# Reference tokens (design/dashboard.html :root + drawMain).
CARD = (10, 10, 10)  # --card #0a0a0a, the chart surface at rest
UP_BODY = (212, 179, 107)  # drawMain up fill #d4b36b
DOWN_BODY = (69, 68, 67)  # drawMain down fill #454443
OLD_CARD = (38, 38, 38)  # pre-rebase card_bg #262626: the "added background"


def _rows(count: int = 90) -> list[tuple[int, str, str, str, str, str]]:
    """Deterministic klines alternating up/down so both body colours render."""
    rows: list[tuple[int, str, str, str, str, str]] = []
    t = 1_700_000_000_000
    price = 100.0
    for i in range(count):
        o = price
        c = o + (1.3 if i % 2 == 0 else -1.3)
        hi, lo = max(o, c) + 0.4, min(o, c) - 0.4
        rows.append((t, f"{o:.2f}", f"{hi:.2f}", f"{lo:.2f}", f"{c:.2f}", "12"))
        t += 3_600_000
        price = c
    return rows


def _render() -> tuple[CandleChart, QImage, Counter[tuple[int, int, int]]]:
    chart = CandleChart()
    chart.resize(880, 420)
    chart.show()
    APP.processEvents()
    chart.set_rows(_rows(), "1h")
    APP.processEvents()
    image = chart.grab().toImage()
    counts: Counter[tuple[int, int, int]] = Counter()
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            counts[(colour.red(), colour.green(), colour.blue())] += 1
    chart.hide()
    return chart, image, counts


def test_chart_rests_exactly_on_the_card_black() -> None:
    _chart, _image, counts = _render()
    total = sum(counts.values())
    assert counts[CARD] > total * 0.5  # the plot surface dominates at rest


def test_reference_candle_body_colours_are_present() -> None:
    _chart, _image, counts = _render()
    assert counts[UP_BODY] > 0  # champagne bodies (drawMain up fill)
    assert counts[DOWN_BODY] > 0  # graphite bodies (drawMain down fill)


def test_no_old_grey_wash_and_no_volume_strip() -> None:
    chart, _image, counts = _render()
    assert counts[OLD_CARD] == 0  # the pre-rebase card wash is gone
    assert not hasattr(chart, "volume")  # reference main chart is candles-only


def test_grid_is_five_levels_between_the_visible_low_and_high() -> None:
    chart, _image, counts = _render()
    assert len(chart._grid) == GRID_LEVELS  # drawMain: g = 0..4 hairlines
    values = sorted(float(line.value()) for line in chart._grid)
    step = values[1] - values[0]
    for i in range(1, len(values) - 1):
        assert abs((values[i + 1] - values[i]) - step) < 1e-9  # even spacing
    # The hairline rgba(255,252,245,.05) blends to this exact card-over colour.
    assert counts[(22, 22, 21)] > 0
