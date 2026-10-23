"""Reference-faithful candle chart from STOCK pyqtgraph items only (finplot
is unapproved, spec §7.2; no custom painting). The authority is
``design/dashboard.html`` (chart card CSS ~line 288, ``drawMain`` ~line 785):
the canvas there has NO fill of its own and sits on the card surface
``--card #0a0a0a`` — here the PlotWidget background IS that theme token, so
the chart area at rest is exactly #0a0a0a, and pyqtgraph's tick-driven alpha
grid (a computed wash, not the reference) is never drawn. Candles only, no
volume strip: up bodies #d4b36b / down #454443 at 64% of the slot with
translucent wicks; exactly five horizontal hairlines rgba(255,252,245,.05)
evenly spaced between the visible low and high — five stock InfiniteLines,
with IBM Plex Mono price labels from the left axis' fixed ticks at the same
levels; the last price is a dashed champagne line rgba(212,179,107,.5) with a
solid-champagne pill pinned right and a radial champagne glow centred 14 px
from the right edge. No vertical grid.

Data is Strike ``/price/v2/klines`` exactly as served; the served decimal
strings become floats ONLY for drawing pixels (display, never money). The
wheel zooms time only. The price scale follows the candles in view. Zooming
out past what this interval can show steps to a longer Strike interval (the
Strike kline enum). Sparse or flat served data keeps the selected interval;
only the user's zoom changes it. The view never pans into empty months.
"""

from __future__ import annotations

import os
from collections.abc import Callable

# pyqtgraph's documented binding selector (pyqtgraph Qt.py: PYQTGRAPH_QT_LIB).
os.environ["PYQTGRAPH_QT_LIB"] = "PySide6"

import pyqtgraph as pg  # noqa: E402
from pyqtgraph.Qt import QT_LIB  # noqa: E402
from PySide6.QtCore import QPointF, Qt, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPen, QRadialGradient  # noqa: E402
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsSceneWheelEvent,
    QWidget,
)  # noqa: E402

from ..strike.market import KLINE_INTERVALS, Klines  # noqa: E402
from .layout import LAYOUT  # noqa: E402
from .theme import BASE_COLORS, palette  # noqa: E402

if QT_LIB != "PySide6":
    raise RuntimeError(f"pyqtgraph bound to {QT_LIB}, Struck requires PySide6")

P = palette()

# Chart tokens from the reference render (design/dashboard.html drawMain):
# up candles are champagne, down candles graphite-grey; five warm-white
# hairlines; mono axis labels; the last price is a dashed champagne line with
# a solid-champagne tag pinned right and a champagne glow under its right end.
UP_BODY = QColor(P["gold"])  # #d4b36b
UP_WICK = QColor(212, 179, 107, 230)  # rgba(212,179,107,.9)
DOWN_BODY = QColor(0x45, 0x44, 0x43)
DOWN_WICK = QColor(122, 120, 114, 204)  # rgba(122,120,114,.8)
# Every stroke goes through pg.mkPen: it builds COSMETIC pens, whose width
# stays in device pixels. A raw QPen is non-cosmetic, and the view transform
# scales its "1 px" width into fat translucent bands across the plot (the
# "added background" the reference does not have).
GRID_PEN = pg.mkPen(QColor(255, 252, 245, 13))  # rgba(255,252,245,.05)
AXIS_TEXT = QColor(150, 150, 150, 204)  # rgba(150,150,150,.8)
LAST_PEN = pg.mkPen(QColor(212, 179, 107, 128), dash=[4, 5])  # rgba(...,.5)
PILL_FILL = QColor(212, 179, 107, 255)  # the .lastpx champagne pill
PILL_TEXT = QColor(0x17, 0x11, 0x06)  # near-black pill text
CROSSHAIR = pg.mkPen(QColor(150, 150, 150, 90), width=1)
NO_PEN = QPen(Qt.PenStyle.NoPen)  # candles are fills; the reference has no outline
GRID_LEVELS = 5  # drawMain: g = 0..4 hairlines between the visible low and high
GLOW_RADIUS = 60  # drawMain: radial glow radius in px
GLOW_INSET_PX = 14.0  # drawMain: glow centred at x = right edge - 14 px

# Strike kline intervals (strike-klines), shortest first.
# Seconds are the candle width used only to choose how many bars fit the
# window. ``1M`` is treated as 30 days for that fit, not as a money unit.
INTERVALS: tuple[str, ...] = KLINE_INTERVALS
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1_800,
    "1h": 3_600,
    "2h": 7_200,
    "4h": 14_400,
    "6h": 21_600,
    "8h": 28_800,
    "12h": 43_200,
    "1d": 86_400,
    "3d": 259_200,
    "1w": 604_800,
    "1M": 2_592_000,
}
# A readable window is about 100 bars. Outside 40..180, step the interval.
_BARS_LO = 40
_BARS_HI = 180


class _ZoomBox(pg.ViewBox):
    """Time zoom only. The stock wheel also scales price, which crushes candles."""

    def __init__(self) -> None:
        super().__init__()
        self.zoom: Callable[[QGraphicsSceneWheelEvent, _ZoomBox], None] | None = None
        self.setMouseEnabled(x=True, y=False)

    def wheelEvent(self, ev: QGraphicsSceneWheelEvent, axis: object = None) -> None:
        if self.zoom is not None:
            self.zoom(ev, self)
        ev.accept()


class _CrosshairPlot(pg.PlotWidget):
    click_price = Signal(float)  # a chart click, as a view-coordinate price

    def __init__(self, item: pg.PlotItem) -> None:
        # The reference canvas has no fill of its own and sits on --card
        # #0a0a0a; the PlotWidget background IS that card token (theme.py
        # card_bg), so the chart area at rest is exactly #0a0a0a. showGrid is
        # never called: its numeric alpha is a wash computed from the axis pen
        # and pixel width, not the reference hairlines, which are five stock
        # InfiniteLines positioned in _fit_y.
        super().__init__(background=BASE_COLORS["card_bg"], plotItem=item)
        pen = pg.mkPen(CROSSHAIR, width=1)
        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pen)
        self.addItem(self.v_line, ignoreBounds=True)
        self.addItem(self.h_line, ignoreBounds=True)
        self.setMouseTracking(True)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        point = self.getViewBox().mapSceneToView(self.mapToScene(ev.position().toPoint()))
        self.v_line.setPos(point.x())
        self.h_line.setPos(point.y())
        super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        # Left click picks a price off the chart (manual order entry: the
        # clicked view-coordinate y becomes the ticket's limit price).
        if ev.button() == Qt.MouseButton.LeftButton:
            point = self.getViewBox().mapSceneToView(self.mapToScene(ev.position().toPoint()))
            self.click_price.emit(point.y())
        super().mousePressEvent(ev)


def _plot(box: _ZoomBox) -> tuple[pg.PlotItem, pg.DateAxisItem]:
    axis = pg.DateAxisItem(orientation="bottom", utcOffset=0)
    return pg.PlotItem(viewBox=box, axisItems={"bottom": axis}), axis


class CandleChart(QWidget):
    timeframe = Signal(str)
    price_clicked = Signal(float)  # forwarded from _CrosshairPlot.click_price

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        pg.setConfigOptions(antialias=True)
        self._price_box = _ZoomBox()
        price_item, _price_axis = _plot(self._price_box)
        self.price = _CrosshairPlot(price_item)
        self.price.click_price.connect(self.price_clicked)  # click -> ticket
        self._price_box.zoom = self._wheel
        # Reference axis style: IBM Plex Mono ~9px, muted, floating labels —
        # drawMain draws no axis spine or tick dashes, only the mono labels.
        # A NoPen axis hides spine and ticks; the default tick length stays,
        # because a zero-length tick line is a rasterizer artefact (stray
        # black pixels at every tick position).
        tick = QFont("IBM Plex Mono")
        tick.setPixelSize(9)
        for side in ("left", "bottom"):
            axis = self.price.getAxis(side)
            axis.setTickFont(tick)
            axis.setTextPen(pg.mkPen(AXIS_TEXT))
            axis.setPen(QPen(Qt.PenStyle.NoPen))
        self.price.getViewBox().disableAutoRange()
        # Left gutter for the mono price labels; the fixed grid ticks are set
        # in _fit_y (five levels between the visible low and high).
        self.price.getAxis("left").setWidth(64)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        layout.addWidget(self.price, 1)
        # The reference main chart is candles-only (~236px): no volume strip.
        self._grid = [
            pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(GRID_PEN))
            for _ in range(GRID_LEVELS)
        ]
        self._lines: list[pg.InfiniteLine] = []
        self._interval = "1h"
        self._pending: str | None = None
        self._applying = False
        self._pin_right = True
        self._t: list[float] = []
        self._lo: list[float] = []
        self._hi: list[float] = []
        self._last_text: str = ""
        self._last_line: pg.InfiniteLine | None = None
        self._glow: QGraphicsEllipseItem | None = None
        self._step = float(INTERVAL_SECONDS["1h"])
        self._price_box.sigXRangeChanged.connect(self._on_x_range)

    def set_klines(self, klines: Klines) -> None:
        self.set_rows([(r[0], r[1], r[2], r[3], r[4], r[5]) for r in klines.root])

    def set_rows(
        self,
        rows: list[tuple[int, str, str, str, str, str]],
        interval: str | None = None,
    ) -> None:
        """(open time ms, o, h, l, c, v) as served by /price/v2/klines or the kline stream."""
        if not rows:
            raise ValueError("need at least one kline to draw")
        same = interval == self._interval or interval is None
        held: tuple[float, float, bool] | None = None
        if same and len(self._t) >= 2:
            x0, x1 = self._x_range()
            held = (x0, x1, self._pin_right)
        if interval in INTERVAL_SECONDS:
            self._interval = interval
        self._pending = None
        t = [r[0] / 1000 for r in rows]
        o, h, lo, c = ([float(r[i]) for r in rows] for i in (1, 2, 3, 4))
        step = float(INTERVAL_SECONDS.get(self._interval, 0))
        if step <= 0:
            step = t[1] - t[0]
        self._t, self._lo, self._hi, self._step = t, lo, h, step
        # Reference candles: up = champagne, down = graphite-grey (drawMain);
        # wicks are the translucent candle colours, bodies are plain fills
        # with no outline (the reference stroke/fillRect pair).
        up = [ci >= oi for oi, ci in zip(o, c, strict=True)]
        wick_brushes = [pg.mkBrush(UP_WICK if flag else DOWN_WICK) for flag in up]
        body_brushes = [pg.mkBrush(UP_BODY if flag else DOWN_BODY) for flag in up]
        no_pens = [QPen(NO_PEN) for _ in up]
        self._last_text = rows[-1][4]  # the served close string, for the tag
        self._applying = True
        self.price.clear()
        self.price.addItem(self.price.v_line, ignoreBounds=True)
        self.price.addItem(self.price.h_line, ignoreBounds=True)
        for line in self._grid:
            self.price.addItem(line, ignoreBounds=True)
        mid = [ti + step / 2 for ti in t]
        self.price.addItem(
            pg.BarGraphItem(
                x=mid,
                y0=lo,
                height=[hi - li for hi, li in zip(h, lo, strict=True)],
                width=step * 0.08,
                brushes=wick_brushes,
                pens=no_pens,
            )
        )
        body = [abs(ci - oi) for oi, ci in zip(o, c, strict=True)]
        self.price.addItem(
            pg.BarGraphItem(
                x=mid,
                y0=[min(oi, ci) for oi, ci in zip(o, c, strict=True)],
                height=body,
                width=step * 0.64,  # 64% of the slot, like the reference render
                brushes=body_brushes,
                pens=[
                    pg.mkPen(UP_BODY if flag else DOWN_BODY, width=1)
                    if oi == ci else QPen(NO_PEN)
                    for oi, ci, flag in zip(o, c, up, strict=True)
                ],
            )
        )
        # Last price: dashed champagne line, solid-champagne pill pinned right
        # (the .lastpx element: mono text on champagne), champagne glow at the
        # right edge positioned in _fit_y.
        self._last_line = pg.InfiniteLine(
            pos=c[-1],
            angle=0,
            movable=False,
            pen=LAST_PEN,
            label=self._last_text,
            labelOpts={
                "position": 0.97,
                "color": QColor(PILL_TEXT),
                "fill": QColor(PILL_FILL),
            },
        )
        self.price.addItem(self._last_line, ignoreBounds=True)
        glow = QRadialGradient(0, 0, GLOW_RADIUS)
        glow.setColorAt(0.0, QColor(212, 179, 107, 71))  # rgba(...,.28)
        glow.setColorAt(1.0, QColor(212, 179, 107, 0))
        self._glow = QGraphicsEllipseItem(
            -GLOW_RADIUS, -GLOW_RADIUS, GLOW_RADIUS * 2, GLOW_RADIUS * 2
        )
        self._glow.setBrush(glow)
        self._glow.setPen(QPen(Qt.PenStyle.NoPen))
        # The glow is 120 DEVICE pixels, not data units (x is unix seconds):
        # ignore the view transform; setPos still anchors it in data coords.
        self._glow.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.price.addItem(self._glow, ignoreBounds=True)
        for line in self._lines:
            self.price.addItem(line, ignoreBounds=True)
        left, right = t[0], t[-1] + step
        if held is None:
            self._pin_right = True
            self.price.setXRange(left, right, padding=0)
        else:
            old_left, old_right, pin = held
            span = old_right - old_left
            span = min(max(span, step * 8), right - left)
            if pin:
                self.price.setXRange(right - span, right, padding=0)
            else:
                start = max(left, min(old_left, right - span))
                self.price.setXRange(start, start + span, padding=0)
        self._applying = False
        self._fit_y(*self._x_range())

    def set_price_lines(self, lines: list[tuple[str, str, str]]) -> None:
        """(label, served-or-plan decimal string, palette colour key)."""
        for line in self._lines:
            self.price.removeItem(line)
        self._lines = []
        for label, value, color in lines:
            item = pg.InfiniteLine(
                pos=float(value),
                angle=0,
                movable=False,
                pen=pg.mkPen(P[color], width=1),
                label=f"{label} {value}",
                labelOpts={
                    "position": 0.95,
                    "color": P[color],
                    # Only the LAST price gets the solid-champagne pill; other
                    # price lines get a dark chip so the accent stays scarce.
                    "fill": QColor(10, 10, 10, 200),
                },
            )
            self.price.addItem(item, ignoreBounds=True)
            self._lines.append(item)
        if self._t:
            self._fit_y(*self._x_range())

    def zoom_span(self, factor: float, cursor: float) -> None:
        """``factor < 1`` zooms in. ``factor > 1`` zooms out. Cursor is unix seconds."""
        if len(self._t) < 2 or factor <= 0 or factor == 1:
            return
        x0, x1 = self._x_range()
        span = x1 - x0
        if span <= 0:
            return
        new_span = span * factor
        target = self._interval_for_span(new_span)
        if target != self._interval and self._pending is None:
            self._pending = target
            self.timeframe.emit(target)
            return
        left, right = self._bounds()
        new_span = min(max(new_span, self._step * 8), right - left)
        anchor = min(max(cursor, left), right)
        kept = (anchor - x0) / span
        nx0 = anchor - new_span * kept
        nx1 = nx0 + new_span
        if nx0 < left:
            nx0, nx1 = left, left + new_span
        if nx1 > right:
            nx1 = right
            nx0 = right - new_span
        self._pin_right = nx1 >= right - self._step
        self._apply_x(nx0, nx1)

    def _wheel(self, ev: QGraphicsSceneWheelEvent, box: _ZoomBox) -> None:
        steps = ev.delta() / 120
        if steps == 0:
            return
        center = box.mapSceneToView(ev.scenePos())
        x, _y = center.toTuple()
        factor: float = 0.8**steps
        self.zoom_span(factor, x)

    def _interval_for_span(self, span: float) -> str:
        """The Strike interval whose bars stay readable across ``span`` seconds."""
        chosen = self._interval
        if span / INTERVAL_SECONDS["1d"] > _BARS_HI:
            return "1d"
        if span / INTERVAL_SECONDS["1m"] < _BARS_LO:
            return "1m"
        best = abs(span / INTERVAL_SECONDS[self._interval] - 100)
        for name in INTERVALS:
            bars = span / INTERVAL_SECONDS[name]
            if _BARS_LO <= bars <= _BARS_HI and abs(bars - 100) < best:
                chosen = name
                best = abs(bars - 100)
        return chosen

    def _bounds(self) -> tuple[float, float]:
        return self._t[0], self._t[-1] + self._step

    def _x_range(self) -> tuple[float, float]:
        xr = self.price.getViewBox().viewRange()
        return float(xr[0][0]), float(xr[0][1])

    def _apply_x(self, x0: float, x1: float) -> None:
        self._applying = True
        self.price.setXRange(x0, x1, padding=0)
        self._applying = False
        self._fit_y(x0, x1)

    def _on_x_range(self, _box: object = None) -> None:
        if self._applying or len(self._t) < 2:
            return
        x0, x1 = self._x_range()
        left, right = self._bounds()
        span = x1 - x0
        if span <= 0:
            return
        if span >= right - left:
            nx0, nx1 = left, right
        elif x0 < left:
            nx0, nx1 = left, left + span
        elif x1 > right:
            nx0, nx1 = right - span, right
        else:
            nx0, nx1 = x0, x1
        self._pin_right = nx1 >= right - self._step
        if (nx0, nx1) != (x0, x1):
            self._apply_x(nx0, nx1)
            return
        self._fit_y(nx0, nx1)

    def _fit_y(self, x0: float, x1: float) -> None:
        lows: list[float] = []
        highs: list[float] = []
        for ti, lo, hi in zip(self._t, self._lo, self._hi, strict=True):
            if ti + self._step < x0 or ti > x1:
                continue
            lows.append(lo)
            highs.append(hi)
        for line in self._lines:
            y = float(line.value())
            lows.append(y)
            highs.append(y)
        if not lows:
            return
        self._applying = True
        self.price.setYRange(min(lows), max(highs), padding=0.08)
        self._applying = False
        # Reference grid (drawMain): exactly five hairlines evenly spaced
        # between the visible low and high, mono-labelled at the same levels.
        low, high = min(lows), max(highs)
        levels = [low + (high - low) * g / (GRID_LEVELS - 1) for g in range(GRID_LEVELS)]
        for line, level in zip(self._grid, levels, strict=True):
            line.setPos(level)
        self.price.getAxis("left").setTicks(
            [[(level, f"{level:.4f}") for level in levels]]
        )
        if self._glow is not None and self._last_line is not None:
            # The champagne glow rides the last-price line, centred 14 px from
            # the right edge in DEVICE pixels (reference: x = width - 14): map
            # that pixel back into data coords for the ignore-transform item.
            box = self.price.getViewBox()
            dev_y = box.mapFromView(QPointF(0.0, float(self._last_line.value()))).y()
            anchor = box.mapDeviceToView(
                QPointF(box.rect().right() - GLOW_INSET_PX, dev_y)
            )
            self._glow.setPos(anchor)
