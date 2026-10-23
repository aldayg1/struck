"""Real widget probes using K02's captured Strike bytes.

Oracle: selected-market history must not be replaced by an obsolete request.
Strike's klines contract permits a single candle; it does not permit silently
changing the user's interval. Fixture provenance and refresh commands live in
fixtures/read_cli/the golden provenance record. No network or mocked unit under test.
"""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from struck.strike.market import Klines, Trades
from struck.ui.chart import CandleChart
from struck.ui.panels import MarketPanel

APP = QApplication.instance() or QApplication([])
FIXTURES = Path(__file__).parent / "fixtures" / "read_cli"


def history() -> tuple[Klines, Trades]:
    return (
        Klines.model_validate_json((FIXTURES / "klines_BTC-USD_1h_5.json").read_bytes()),
        Trades.model_validate_json((FIXTURES / "trades_BTC-USD_5.json").read_bytes()),
    )


def test_obsolete_history_cannot_replace_selected_market() -> None:
    panel = MarketPanel()
    panel._history_gen = 2
    panel._history[1] = history()
    panel._draw_history("obsolete BTC request", 1)
    assert panel._candles == []
    assert panel.chart._t == []
    assert panel.status.text() == ""
    assert panel._history == {}
    panel.close()


def test_single_served_candle_preserves_selected_interval() -> None:
    chart = CandleChart()
    klines, _trades = history()
    row = klines.root[0]
    requested: list[str] = []
    chart.timeframe.connect(requested.append)
    chart.set_rows([(row[0], row[1], row[2], row[3], row[4], row[5])], "1h")
    assert chart._t == [1792674000.0]  # captured first openTime, milliseconds / 1000
    assert chart._last_text == "81405.8"  # captured first close
    assert requested == []
    chart.close()


def test_startup_crosshair_is_a_hairline_not_a_scaled_band() -> None:
    """Operator screenshot: a one-unit pen filled half the initial 0..1 view.

    Qt cosmetic pens stay in device pixels; the design requires hairlines on
    #0a0a0a. Check the actual startup raster with the cursor at view centre.
    """
    chart = CandleChart()
    chart.resize(880, 420)
    chart.show()
    APP.processEvents()
    chart.price.v_line.setPos(0.5)
    chart.price.h_line.setPos(0.5)
    APP.processEvents()
    raster = chart.grab().toImage()
    changed = 0
    for y in range(40, 360):
        for x in range(100, 800):
            if raster.pixelColor(x, y).name() != "#0a0a0a":
                changed += 1
    # Two one-pixel strokes, allowing antialiasing at both edges.
    assert changed <= 3 * (700 + 320), f"crosshair paints {changed} interior pixels"
    chart.close()
