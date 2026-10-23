"""Struck panels (spec §7.3): market chart, bots (trigger CRUD), approval +
raw view. Every network/graph call runs off the UI thread (stock
``QThread``); approval is a HUMAN click on a button that names the plan
hash, never typed or scripted by Struck.
"""

from __future__ import annotations


import json
import time
from collections import deque
from functools import cmp_to_key
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, get_args

import httpx
from langgraph.types import Command
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableView,
    QTableWidgetItem,
    QTabWidget,
    QWidget,
)

from ..callhistory import RecordedHttp
from ..network import (
    NETWORK_SETTING_PATH,
    NETWORKS,
    NetworkProfile,
    profile,
    save_network_setting,
)
from ..config import (
    CONFIG_PATH,
    ConfigError,
    LLM_CONFIG_PATH,
    REPO_ROOT,
    StruckConfig,
    LlmConfig,
    LlmProvider,
    load_config,
    save_llm_config,
)
from ..plan.models import PlanDecision
from ..plan.providers import (
    PROVIDERS,
    LlmConfigError,
    chat_model,
    key_state,
    list_models,
    llm_config_of,
)
from ..private_file import write_private
from pydantic import ValidationError
from ..keys import ENTRIES, entry_states
from ..reset import audit as reset_audit
from ..reset import report_text as reset_report_text
from ..reset import reset as reset_delete
from .. import secrets as _secrets
from ..secrets import SecretError, delete_secret, has_secret, set_secret, unlock
from ..strike.account import (
    Account,
    PositionsResponse,
    ServedStrikeAccount,
    StrikeAuthedClient,
    server_offset_ms,
)
from ..strike.auth import ApiWallet, StrikeCredentialsError, logon_params
from ..strike.venue_auth import entry_for, wallet_from_secrets
from .venue_auth_dialog import StrikeConnectDialog
from ..strike.history import transactions
from ..strike.stream_models import (
    AccountUpdate,
    DepthUpdate,
    KlineEvent,
    MarkPriceUpdate,
    MiniTicker,
    OrderTradeUpdate,
    PublicTrade,
    RfqUpdate,
)
from ..strike.streams import (
    PRICE_BROADCAST_CHANNELS,
    PRICE_SYMBOL_CHANNELS,
    StrikeStream,
    ws_request,
)
from struck_py import decimal_compare
from ..strike.market import (
    KLINE_INTERVALS,
    Depth,
    Klines,
    Market,
    PriceType,
    StrikeMarketClient,
    Trades,
)
from ..strike.models import OrderType, StrikeConfirm, StrikeLeg, StrikePlanProposal
from ..strike.order_cli import _Graph, _ThreadConfig
from ..strike.pipeline import (
    SENT,
    FixedStrikePlanner,
    StrikePipeline,
    build_strike_graph,
    confirm_request_of,
    plan_of,
    prepared_of,
    validations_of,
)
from ..triggers.engine import Engine, TriggerStore
from ..triggers.kupo_watch import KupoWatch
from ..triggers.models import SequenceCondition, TriggerSpec
from ..triggers.policy import ApprovedPolicies
from . import theme
from .layout import LAYOUT
from .chart import CandleChart


UI_LOG = REPO_ROOT / "logs" / "ui_events.log"
_UI_ERRORS = (ValueError, RuntimeError, OSError, httpx.HTTPError)


def ui_log(panel: str, text: str) -> str:
    """Append one UI event/error line (no secrets: messages only name secret entries)."""
    UI_LOG.parent.mkdir(parents=True, exist_ok=True)
    with UI_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(UTC).isoformat(timespec='milliseconds')} [{panel}] {text}\n")
    return text


def wallet_or_reason() -> tuple[ApiWallet | None, str]:
    try:
        return wallet_from_secrets(), ""
    except StrikeCredentialsError as exc:
        return None, str(exc)


class Worker(QThread):
    """Runs one callable off the UI thread; emits its text result or error."""

    done = Signal(str)
    failed = Signal(str)

    def __init__(self, job: Callable[[], str]) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            self.done.emit(self._job())
        except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
            # Surfaced loudly in the UI (pydantic ValidationError is a
            # ValueError; every Struck error is a RuntimeError).
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _button(text: str, variant: str = "default") -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet(theme.button_qss(variant))
    return b


def _compact_row(w: QPushButton) -> QHBoxLayout:
    """A button at its NATURAL width, left-aligned. QFormLayout stretches its
    field widgets window-wide; buttons must not be swallowed by that (operator
    2026-10-22: no screen-wide billboard buttons)."""
    row = LAYOUT.hbox()
    row.addWidget(w)
    row.addStretch(1)
    return row


def network_badge(net: NetworkProfile) -> QLabel:
    """The selected network, on the title bar and every approval screen: named
    plainly, no danger styling ."""
    return QLabel(f"NETWORK: {net.label}")


def _table(headers: tuple[str, ...]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setAlternatingRowColors(True)
    t.resizeColumnsToContents()
    return t


def fill_table(t: QTableWidget, rows: list[tuple[str, ...]]) -> None:
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            t.setItem(r, c, QTableWidgetItem(value))
    t.resizeColumnsToContents()


LEG_COLUMNS = (
    "#",
    "kind",
    "symbol",
    "side",
    "type",
    "size",
    "price",
    "stop",
    "TIF",
    "reduce_only",
    "post_only",
    "callback_rate",
    "activation",
    "TP",
    "SL",
    "leverage",
    "client_order_id",
)


def strike_leg_rows(legs: tuple[StrikeLeg, ...]) -> list[tuple[str, ...]]:
    return [
        (
            str(i),
            leg.kind,
            leg.symbol,
            leg.side or "",
            leg.type or "",
            leg.size or "",
            leg.price or "",
            leg.stop_price or "",
            leg.time_in_force or "",
            str(leg.reduce_only).lower(),
            str(leg.post_only).lower(),
            leg.callback_rate or "",
            leg.activation_price or "",
            f"{leg.tp.type} stop {leg.tp.stop_price} price {leg.tp.price or '-'}" if leg.tp else "",
            f"{leg.sl.type} stop {leg.sl.stop_price} price {leg.sl.price or '-'}" if leg.sl else "",
            str(leg.leverage or ""),
            leg.client_order_id or "",
        )
        for i, leg in enumerate(legs)
    ]


def _mono() -> QPlainTextEdit:
    t = QPlainTextEdit()
    t.setObjectName("mono")
    t.setReadOnly(True)
    return t


def _price_cmp(a: str, b: str) -> int:
    """Order two served decimal strings with the Rust decimal core (no floats)."""
    return -1 if decimal_compare(a, "lt", b) else (1 if decimal_compare(a, "gt", b) else 0)


def _level_asc(a: tuple[str, str], b: tuple[str, str]) -> int:
    return _price_cmp(a[0], b[0])


def _level_desc(a: tuple[str, str], b: tuple[str, str]) -> int:
    return _price_cmp(b[0], a[0])


DEPTH_LEVELS = 20 # the depth stream covers the top 20 per side (strike-orderbook)
TRADE_ROWS = 50


def _clock(ms: int) -> str:
    """A served unix-millisecond timestamp as UTC clock time, for display only."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%H:%M:%S")


def _taker_side(buyer_is_maker: bool) -> str:
    return "sell" if buyer_is_maker else "buy"


class MarketPanel(QWidget):
    """LIVE market view on the public price stream (``/ws/price``): depth,
    trade, markPrice@1s and kline_<interval> for the selected symbol, plus
    ``!markprice@arr`` and ``!miniticker@arr`` for every symbol. The
    symbol list and markets table come from ``/v2/markets`` once at start; chart
    history from ``/price/v2/klines``; depth follows the strike-orderbook
    synchronization pattern (subscribe, buffer, ack, REST snapshot, drop
    ``u <= lastUpdateId``, re-seed on the first diff, re-sync on cross / 60 s)."""

    # Kline intervals are the API's request enum (strike-klines), not data.
    INTERVALS = KLINE_INTERVALS
    MARKET_COLUMNS = (
        "symbol",
        "status",
        "mark_price",
        "index_price",
        "last_price",
        "default_leverage",
        "max_leverage",
    )

    def __init__(self, propose_plan: Callable[[StrikePlanProposal], None] | None = None) -> None:
        super().__init__()
        self.propose_plan = propose_plan
        self._symbols: list[str] = []
        self._markets: dict[str, Market] = {} # the served /v2/markets rows
        self._rows: list[tuple[str, ...]] = []
        self.symbol = QComboBox() # filled ONLY from the served /v2/markets
        self.symbol.setPlaceholderText("Symbol")
        self.interval = QComboBox()
        self.interval.addItems(self.INTERVALS)
        self.interval.setCurrentText("1h")
        self.price_type = QComboBox()
        self.price_type.addItems(["mark", "index", "last"])
        self.price_type.setToolTip("Candle prices served by Strike: mark, index, or last trade")
        self.candle_status = QLabel("Waiting for candles")
        self.candle_status.setObjectName("muted")
        self.reload = _button("Reload markets", "default")
        self.reload.clicked.connect(self.reload_markets)
        self.reload.setToolTip("Refetch /v2/markets now: new served listings need no restart")
        self.lev_note = QLabel("")
        self.lev_note.setObjectName("muted")
        self.badge = QLabel("")
        self.badge.setObjectName("muted")
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.markets_table = _table(self.MARKET_COLUMNS)
        self.depth_model = QStandardItemModel(0, 4)
        self.depth_model.setHorizontalHeaderLabels(["bid qty", "bid price", "ask price", "ask qty"])
        self.trades_model = QStandardItemModel(0, 4)
        self.trades_model.setHorizontalHeaderLabels(["Time", "Price", "Size", "Side"])
        self.chart = CandleChart()
        self.chart.timeframe.connect(self._on_timeframe)
        bar = LAYOUT.hbox()
        for w in (
            self.symbol,
            self.interval,
            self.price_type,
            self.reload,
            self.lev_note,
            self.badge,
            self.status,
        ):
            bar.addWidget(w)
        bar.addStretch(1)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        layout.addLayout(bar)
        layout.addWidget(self.candle_status)
        chart_row = LAYOUT.hbox()
        chart_row.addWidget(self.chart, 5)
        self.ticket = ChartTicket(self)
        self.ticket.setVisible(False) # "Show order panel" opens it beside the chart
        chart_row.addWidget(self.ticket, 1)
        self.chart_only = _button("Show order panel", "default")
        self.chart_only.clicked.connect(self._toggle_ticket)
        self.chart.price_clicked.connect(self._fill_price)
        layout.addLayout(chart_row, 3)
        tables = LAYOUT.hbox()
        self.depth_box = QGroupBox("Order book")
        self.trades_box = QGroupBox("Trades")
        for box, model in (
            (self.depth_box, self.depth_model),
            (self.trades_box, self.trades_model),
        ):
            view = QTableView()
            view.setModel(model)
            box_inner = LAYOUT.vbox()
            box_inner.addWidget(view)
            box.setLayout(box_inner)
            tables.addWidget(box, 1)
        markets_box = QGroupBox("Markets")
        markets_box_inner = LAYOUT.vbox()
        markets_box_inner.addWidget(self.markets_table)
        markets_box.setLayout(markets_box_inner)
        tables.insertWidget(0, markets_box, 1)
        layout.addLayout(tables, 2)
        self.preferred_symbol = "" # optional pick at start; must be served by /v2/markets
        self._sub_symbol = ""
        self._sub_interval = ""
        self._sub_price_type: PriceType = "mark"
        self._next_id = 1
        self._depth_ack = -1
        self._bids: dict[str, str] = {}
        self._asks: dict[str, str] = {}
        self._synced = False
        self._awaiting_first = False
        self._buffer: list[DepthUpdate] = []
        self._last_u = 0
        self._gen = 0
        self._snaps: dict[int, Depth] = {}
        self._candles: list[tuple[int, str, str, str, str, str]] = []
        self._seed_trades: Trades | None = None
        self._history_gen = 0
        self._history: dict[int, tuple[Klines, Trades]] = {}
        self._mark = ""
        self._workers: list[Worker] = []
        self.stream = self._new_stream()
        self._resync_timer = QTimer(self)
        self._resync_timer.setInterval(60_000) # strike-orderbook step 6: periodic re-sync
        self._resync_timer.timeout.connect(lambda: self.resync() if self._synced else None)
        self.symbol.currentTextChanged.connect(self._changed)
        self.interval.currentTextChanged.connect(self._changed)
        self.price_type.currentTextChanged.connect(self._changed)
        self.set_badge("NOT CONNECTED")

    # ---- helpers -------------------------------------------------------
    def _new_stream(self) -> StrikeStream:
        net = load_config().profile
        stream = StrikeStream(
            net.strike_price_ws, f"strike-{net.strike_venue}-price-ws", self._on_open
        )
        stream.state.connect(self.set_badge)
        stream.message.connect(self.on_message)
        return stream

    def _on_timeframe(self, interval: str) -> None:
        """Wheel zoom asked for a longer or shorter Strike interval."""
        if interval not in self.INTERVALS or interval == self.interval.currentText():
            return
        self.interval.setCurrentText(interval)

    def _changed(self, _text: str) -> None:
        self._set_lev_note(self.symbol.currentText())
        self.switch()

    def set_badge(self, text: str) -> None:
        self.badge.setText(ui_log("market", f"[{text}]"))

    def _failed(self, text: str) -> None:
        self.set_badge(f"FAILED: {text}") # the badge is not overwritten by live mark updates

    def _run(self, fn: Callable[[], str], done: Callable[[str], None]) -> None:
        worker = Worker(fn)
        worker.done.connect(done)
        worker.failed.connect(self._failed)
        worker.finished.connect(
            lambda: self._workers.remove(worker) if worker in self._workers else None
        )
        self._workers.append(worker)
        worker.start()

    def _client_call[T](self, call: Callable[[StrikeMarketClient], T]) -> T:
        cfg = load_config()
        http = RecordedHttp(cfg.timeout_s)
        try:
            return call(StrikeMarketClient(http, cfg.strike_api_url))
        finally:
            http.close()

    def _streams(self, symbol: str, interval: str) -> list[str]:
        suffix = "" if self._sub_price_type == "last" else f"_{self._sub_price_type}"
        return [*PRICE_SYMBOL_CHANNELS, f"kline_{interval}{suffix}"]

    def _sub(self, method: str, channel: str, symbol: str | None) -> dict[str, object]:
        req = ws_request(method, channel, self._next_id, symbol)
        if method == "subscribe" and channel == "depth":
            self._depth_ack = self._next_id
        self._next_id += 1
        return req

    def _set_market_cell(self, symbol: str, column: str, value: str) -> None:
        col = self.MARKET_COLUMNS.index(column)
        for row in range(self.markets_table.rowCount()):
            item = self.markets_table.item(row, 0)
            if item is not None and item.text() == symbol:
                self.markets_table.setItem(row, col, QTableWidgetItem(value))
                return

    # ---- startup -------------------------------------------------------
    def fetch(self) -> str:
        page = self._client_call(lambda c: c.markets().value)
        self._markets = page.markets
        self._symbols = sorted(self._markets)
        self._rows = [
            (
                m.symbol,
                m.status,
                m.mark_price or "",
                m.index_price or "",
                m.last_price or "",
                "" if m.default_leverage is None else str(m.default_leverage),
                str(m.margin_tiers[0].max_leverage) if m.margin_tiers else "",
            )
            for m in (self._markets[k] for k in self._symbols)
        ]
        if not self._symbols and page.refused:
            raise ValueError(
                "no /v2/markets entry parsed; refused: "
                + ", ".join(f"{r.symbol} ({r.reason})" for r in page.refused)
            )
        if not self._symbols:
            raise ValueError("no markets returned")
        refused = ""
        if page.refused:
            refused = " ; REFUSED (served, unparseable, shown nowhere): " + ", ".join(
                f"{r.symbol} ({r.reason})" for r in page.refused
            )
        return f"{len(self._symbols)} markets{refused}"

    def start(self) -> None:
        self._run(self.fetch, self._started)

    def reload_markets(self) -> None:
        """Refetch /v2/markets and refill the combobox + table WITHOUT touching
        the stream, subscriptions, history or depth state (new served listings
        appear live; unlike start(), no stream restart)."""
        self._run(self.fetch, self._markets_loaded)

    def _started(self, text: str) -> None:
        fill_table(self.markets_table, self._rows)
        self.symbol.blockSignals(True)
        self.symbol.clear()
        self.symbol.addItems(self._symbols)
        self.symbol.setCurrentIndex(max(0, self.symbol.findText(self.preferred_symbol)))
        self.symbol.blockSignals(False)
        self.status.setText(text)
        self._sub_symbol, self._sub_interval = (
            self.symbol.currentText(),
            self.interval.currentText(),
        )
        self._load_history()
        self.stream.open()
        self._resync_timer.start()

    def _markets_loaded(self, text: str) -> None:
        """The reload path: same fill as _started, but the stream stays up and
        the signal handlers stay connected."""
        fill_table(self.markets_table, self._rows)
        pick = self.symbol.currentText()
        self.symbol.blockSignals(True)
        self.symbol.clear()
        self.symbol.addItems(self._symbols)
        if pick in self._symbols:
            self.symbol.setCurrentText(pick)
        self.symbol.blockSignals(False)
        self._set_lev_note(pick)
        self.status.setText(text)

    def _toggle_ticket(self) -> None:
        """The chart NEVER leaves the screen (like
        TradingView/Binance, the order panel opens BESIDE the chart; the
        ticket toggles its own side column only)."""
        showing = self.ticket.isVisible()
        self.ticket.setVisible(not showing)
        self.chart_only.setText("Hide order panel" if not showing else "Show order panel")

    def _set_lev_note(self, symbol: str) -> None:
        """Chart-header leverage note: served default_leverage and the max
        leverage of the market's declared margin tiers (strike-calculations:
        tiers[0] is the smallest-notional tier, so it holds the highest
        allowed leverage). Display only; the Rust plan check governs."""
        market = self._markets.get(symbol)
        if market is None:
            self.lev_note.setText("")
            return
        default = (
            "default lev ?"
            if market.default_leverage is None
            else f"default lev {market.default_leverage}"
        )
        max_lev = (
            "" if not market.margin_tiers else f" max lev {market.margin_tiers[0].max_leverage}"
        )
        self.lev_note.setText(f"{default}{max_lev}")
        self.ticket.refresh()

    def _fill_price(self, price: float) -> None:
        """Chart click -> the ticket's price field. Formatting only: the price
        is rendered in the market's served quote_prec; the tick grid itself is
        enforced by the Rust plan check (strike-trading PRICE_FILTER)."""
        if not self.ticket.isVisible():
            self._toggle_ticket()
            self._set_lev_note(self.symbol.currentText())
        market = self._markets.get(self.symbol.currentText())
        prec = market.quote_prec if market is not None else 8
        self.ticket.price.setText(f"{price:.{prec}f}")

    def send_plan(self, plan: StrikePlanProposal) -> None:
        """ChartTicket -> the approval pipeline (same lane as the Strategies
        tab); never signs, never sends, stops at human approval."""
        if self.propose_plan is None:
            self._failed("REJECTED: no approval lane wired for chart order entry")
            return
        self.propose_plan(plan)

    def _on_open(self) -> list[dict[str, object]]:
        # (re)connect: resubscribe every active stream; depth re-syncs on its ack.
        self._reset_depth_state()
        symbol_subs = [
            self._sub("subscribe", ch, self._sub_symbol)
            for ch in self._streams(self._sub_symbol, self._sub_interval)
        ]
        broadcasts = [self._sub("subscribe", ch, None) for ch in PRICE_BROADCAST_CHANNELS]
        return symbol_subs + broadcasts

    def switch(self) -> None:
        """Symbol or interval changed: unsubscribe the old streams, subscribe the new."""
        symbol, interval = self.symbol.currentText(), self.interval.currentText()
        price_type = self.price_type.currentText()
        if price_type not in ("last", "mark", "index"):
            raise ValueError(f"unsupported candle price type: {price_type}")
        if (
            not symbol
            or (symbol, interval, price_type)
            == (self._sub_symbol, self._sub_interval, self._sub_price_type)
            or not self._sub_symbol
        ):
            return
        if price_type != self._sub_price_type:
            # Kline payloads omit priceType. A fresh socket prevents late
            # messages from the previous series being interpreted as this one.
            self.stream.message.disconnect(self.on_message)
            self.stream.state.disconnect(self.set_badge)
            self.stream.close()
            self._sub_price_type = cast(PriceType, price_type)
            self._sub_symbol, self._sub_interval = symbol, interval
            self.stream = self._new_stream()
            self._load_history()
            self.stream.open()
            return
        old = set(
            (self._sub_symbol, ch) for ch in self._streams(self._sub_symbol, self._sub_interval)
        )
        new = set((symbol, ch) for ch in self._streams(symbol, interval))
        if symbol != self._sub_symbol:
            self._mark = ""
            self.chart.set_price_lines([])
            self._reset_depth_state()
            self.trades_model.removeRows(
                0, self.trades_model.rowCount()
            ) # another symbol's trades do not belong here
        for sym, ch in sorted(old - new):
            self.stream.send(self._sub("unsubscribe", ch, sym))
        self._sub_symbol, self._sub_interval = symbol, interval
        for sym, ch in sorted(new - old):
            self.stream.send(self._sub("subscribe", ch, sym))
        self._load_history()

    def _load_history(self) -> None:
        symbol, interval = self._sub_symbol, self._sub_interval
        price_type = self._sub_price_type
        self._history_gen += 1
        generation = self._history_gen
        self._candles = []
        self.candle_status.setText(f"Loading {price_type} candles · {symbol} · {interval}")
        self._run(
            lambda: self._fetch_history(symbol, interval, generation, price_type),
            lambda text: self._draw_history(text, generation),
        )

    def _fetch_history(
        self, symbol: str, interval: str, generation: int, price_type: PriceType
    ) -> str:
        # 90 bars: the reference chart's readable density (design/dashboard
        # drawMain draws ~56; Struck keeps a little more history).
        klines = self._client_call(lambda c: c.klines(symbol, interval, 90, price_type).value)
        trades = self._client_call(lambda c: c.trades(symbol, TRADE_ROWS).value)
        self._history[generation] = (klines, trades)
        return f"{symbol} {interval} · {len(klines.root)} candles"

    def _retitle(self) -> None:
        symbol = self._sub_symbol if self._sub_symbol else "Market"
        self.depth_box.setTitle(f"{symbol} order book")
        self.trades_box.setTitle(f"{symbol} trades")

    def _draw_history(self, text: str, generation: int) -> None:
        klines, trades = self._history.pop(generation)
        if generation != self._history_gen:
            return
        rows = {r[0]: (r[0], r[1], r[2], r[3], r[4], r[5]) for r in klines.root}
        rows.update({row[0]: row for row in self._candles})
        self._candles = [rows[t] for t in sorted(rows)]
        self._seed_trades = trades
        self.status.setText(text)
        self._retitle()
        self.chart.set_rows(self._candles, self._sub_interval)
        self.candle_status.setText(
            f"{self._sub_price_type} candles · latest open "
            f"{datetime.fromtimestamp(self._candles[-1][0] / 1000, UTC):%Y-%m-%d %H:%M UTC}"
        )
        if self._seed_trades is not None and self.trades_model.rowCount() == 0:
            for t in self._seed_trades.root: # served newest first
                self.trades_model.appendRow(
                    [
                        QStandardItem(v)
                        for v in (
                            _clock(t.time),
                            t.price,
                            t.qty,
                            _taker_side(t.isBuyerMaker),
                        )
                    ]
                )

    # ---- stream --------------------------------------------------------
    def on_message(self, msg: dict[str, object]) -> None:
        e = msg.get("e")
        if e is None and msg.get("id") == self._depth_ack and "result" in msg:
            self.set_badge("LIVE")
            self.resync()
            return
        if e == "error":
            self.set_badge(f"FAILED: {json.dumps(msg.get('error'))}")
            return
        if e == "24hrMiniTicker":
            tick = MiniTicker.model_validate(msg)
            self._set_market_cell(tick.s, "last_price", tick.c)
            if tick.s != self._sub_symbol:
                return
        if e == "markPriceUpdate":
            marked = MarkPriceUpdate.model_validate(msg)
            self._set_market_cell(marked.s, "mark_price", marked.p)
            self._set_market_cell(marked.s, "index_price", marked.i)
            if marked.s != self._sub_symbol:
                return
            self._mark = marked.p
            self.status.setText(
                f"{marked.s} mark {marked.p} index {marked.i} "
                f"funding {marked.r} next {_clock(marked.T)}"
            )
            self._retitle()
            if len(self._candles) >= 2:
                self.chart.set_price_lines([("mark", marked.p, "gold")])
            return
        if msg.get("s") != self._sub_symbol:
            return # late message for a stream we already unsubscribed
        if e == "depthUpdate":
            self.on_depth(DepthUpdate.model_validate(msg))
        elif e == "trade":
            t = PublicTrade.model_validate(msg)
            self.trades_model.insertRow(
                0, [QStandardItem(v) for v in (_clock(t.T), t.p, t.q, _taker_side(t.m))]
            )
            if self.trades_model.rowCount() > TRADE_ROWS:
                self.trades_model.removeRow(self.trades_model.rowCount() - 1)
        elif e == "kline":
            event = KlineEvent.model_validate(msg)
            k = event.k
            if k.i != self._sub_interval:
                return
            self.candle_status.setText(
                f"{self._sub_price_type} candles · updated {_clock(event.E)} · close {k.c}"
            )
            row = (k.t, k.o, k.h, k.l, k.c, k.v)
            if self._candles and self._candles[-1][0] == k.t:
                self._candles[-1] = row
            elif not self._candles or k.t > self._candles[-1][0]:
                self._candles.append(row)
            if len(self._candles) >= 2:
                self.chart.set_rows(self._candles, self._sub_interval)
                if self._mark:
                    self.chart.set_price_lines([("mark", self._mark, "gold")])

    def _reset_depth_state(self) -> None:
        self._synced, self._awaiting_first, self._buffer = False, True, []
        self._bids, self._asks, self._last_u = {}, {}, 0

    def on_depth(self, ev: DepthUpdate) -> None:
        if self._awaiting_first:
            self._awaiting_first = False
            self.resync(keep=ev) # may be a full top-20 push without removals
            return
        if not self._synced:
            self._buffer.append(ev)
            return
        if ev.u <= self._last_u:
            return
        self._apply(ev)
        if self._crossed():
            self.resync()
        else:
            self.render_depth()

    def resync(self, keep: DepthUpdate | None = None) -> None:
        self._gen += 1
        gen, symbol = self._gen, self._sub_symbol
        self._synced = False
        self._buffer = [keep] if keep is not None else []

        def fetch() -> str:
            self._snaps[gen] = self._client_call(lambda c: c.depth(symbol, DEPTH_LEVELS).value)
            return str(gen)

        self._run(fetch, lambda g: self.seed(int(g), self._snaps.pop(int(g))))

    def seed(self, gen: int, snap: Depth) -> None:
        if gen != self._gen:
            return # a newer resync superseded this one
        self._bids = {p: q for p, q in snap.bids}
        self._asks = {p: q for p, q in snap.asks}
        self._last_u = snap.lastUpdateId
        for ev in self._buffer:
            if ev.u > self._last_u:
                self._apply(ev)
        self._buffer = []
        self._synced = True
        self.render_depth()

    def _apply(self, ev: DepthUpdate) -> None:
        for book, levels in ((self._bids, ev.b), (self._asks, ev.a)):
            for price, qty in levels or []:
                if qty == "0":
                    book.pop(price, None)
                else:
                    book[price] = qty
        self._bids = dict(self.sorted_bids())
        self._asks = dict(self.sorted_asks())
        self._last_u = ev.u

    def sorted_bids(self) -> list[tuple[str, str]]:
        return sorted(self._bids.items(), key=cmp_to_key(_level_desc))[:DEPTH_LEVELS]

    def sorted_asks(self) -> list[tuple[str, str]]:
        return sorted(self._asks.items(), key=cmp_to_key(_level_asc))[:DEPTH_LEVELS]

    def _crossed(self) -> bool:
        bids, asks = self.sorted_bids(), self.sorted_asks()
        return bool(bids and asks) and decimal_compare(bids[0][0], "gte", asks[0][0])

    def render_depth(self) -> None:
        bids, asks = self.sorted_bids(), self.sorted_asks()
        n = max(len(bids), len(asks))
        self.depth_model.setRowCount(n)
        for r in range(n):
            bp, bq = bids[r] if r < len(bids) else ("", "")
            ap, aq = asks[r] if r < len(asks) else ("", "")
            for c, v in enumerate((bq, bp, ap, aq)):
                item = cast(
                    "QStandardItem | None", self.depth_model.item(r, c)
                ) # None at runtime for an empty cell
                if item is None:
                    self.depth_model.setItem(r, c, QStandardItem(v))
                elif item.text() != v:
                    item.setText(v)


def _action_text(spec: TriggerSpec) -> str:
    if isinstance(spec.condition, SequenceCondition):
        return " then ".join(
            "+".join(a.kind for a in step.actions) for step in spec.condition.steps
        )
    return ", ".join(a.kind for a in spec.actions)


class BotsPanel(QWidget):
    """Trigger CRUD over the ONE ``TriggerStore`` plus an explicit tick."""

    COLUMNS = ("id", "status", "label", "condition", "actions", "last result")

    def __init__(self, store: TriggerStore, account: AccountPanel | None = None) -> None:
        super().__init__()
        self.store = store
        self._account = account
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setAlternatingRowColors(True)
        from .builders import TriggerBuilder # builders reuse this module's widgets

        self.builder = TriggerBuilder(self.add_spec)
        self.log = _mono()
        buttons = LAYOUT.hbox()
        actions: list[tuple[str, str, Callable[[], None]]] = [
            ("Add (from builder)", "primary", self.add),
            ("Edit selected", "default", self.edit_selected),
            ("Save edit", "primary", self.save_edit),
            ("Pause", "warning", lambda: self.set_status("paused")),
            ("Resume", "success", lambda: self.set_status("active")),
            ("Remove", "danger", self.remove),
            ("Tick now", "default", self.tick),
        ]
        for text, variant, slot in actions:
            b = _button(text, variant)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        layout.addLayout(buttons)
        # The builder's ModelForm minimum is ~2857px (every TriggerSpec field):
        # without the scroll wrapper it eats the whole tab and crushes the table.
        self.table.setMinimumHeight(240)
        layout.addWidget(self.table, 3)
        layout.addWidget(LAYOUT.scroll(self.builder), 2)
        layout.addWidget(self.log, 1)
        self._editing: str | None = None
        self._worker: Worker | None = None
        self.refresh()

    def refresh(self) -> None:
        rows = self.store.list()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            values = (
                r.id,
                r.status,
                r.spec.label,
                r.spec.condition.kind,
                _action_text(r.spec),
                r.last_result or "",
            )
            for j, value in enumerate(values):
                self.table.setItem(i, j, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

    def _selected_id(self) -> str | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.text() if item is not None else None

    def _report(self, text: str) -> None:
        self.log.appendPlainText(ui_log("bots", text))
        self.refresh()

    def _need_selection(self, action: str) -> str | None:
        trigger_id = self._selected_id()
        if trigger_id is None:
            self._report(f"NOT DONE: {action} needs a selected trigger row")
        return trigger_id

    def load_proposed(self, spec: TriggerSpec) -> None:
        """Chat 'Review': the LLM's TriggerSpec is shown in the builder form for the
        EXISTING Add validation. Nothing is stored until the human presses Add."""
        self._editing = None
        self.builder.load(spec)
        self._report(
            f"chat proposal '{spec.label}' loaded into the builder: press Add to validate and "
            "store it (a fired trigger only creates a proposal that needs fresh approval)"
        )

    def add_spec(self, spec: TriggerSpec) -> None:
        try:
            record = self.store.add(spec, datetime.now(UTC))
        except _UI_ERRORS as exc:
            self._report(f"REJECTED: {exc}")
            return
        self._report(f"added {record.id}")

    def _built(self) -> TriggerSpec | None:
        try:
            return self.builder.spec()
        except _UI_ERRORS as exc:
            self._report(f"REJECTED: {exc}")
            return None

    def add(self) -> None:
        spec = self._built()
        if spec is not None:
            self.add_spec(spec)

    def edit_selected(self) -> None:
        trigger_id = self._need_selection("Edit selected")
        if trigger_id is None:
            return
        self._editing = trigger_id
        self.builder.load(self.store.get(trigger_id).spec)
        self._report(f"editing {trigger_id} in the builder (press Save edit)")

    def save_edit(self) -> None:
        if self._editing is None:
            self._report("NOT DONE: select a trigger and press 'Edit selected' first")
            return
        spec = self._built()
        if spec is None:
            return
        try:
            self.store.update(self._editing, spec, datetime.now(UTC))
        except _UI_ERRORS as exc:
            self._report(f"REJECTED: {exc}")
            return
        self._report(f"updated {self._editing}")

    def set_status(self, status: str) -> None:
        trigger_id = self._need_selection("Pause" if status == "paused" else "Resume")
        if trigger_id is None:
            return
        try:
            self.store.set_status(
                trigger_id, "paused" if status == "paused" else "active", datetime.now(UTC)
            )
        except _UI_ERRORS as exc:
            self._report(f"FAILED: {exc}")
            return
        self._report(f"{trigger_id}: {status}")

    def remove(self) -> None:
        trigger_id = self._need_selection("Remove")
        if trigger_id is None:
            return
        answer = QMessageBox.question(self, "Remove trigger", f"Remove {trigger_id}?")
        if answer != QMessageBox.StandardButton.Yes:
            self._report(f"NOT DONE: removal of {trigger_id} not confirmed")
            return
        self.store.remove(trigger_id)
        self._report(f"removed {trigger_id}")

    def _tick(self, updates: tuple[AccountUpdate, ...] | None) -> str:
        cfg = load_config()
        http = RecordedHttp(cfg.timeout_s)
        try:
            # Same as `triggers.cli tick`: positions and completed deposits
            # come from the API wallet. Without it those conditions fail closed.
            wallet, reason = wallet_or_reason()
            deposits = None
            account = None
            if wallet is not None:
                client = StrikeAuthedClient(http, cfg.strike_api_url, wallet)
                account = ServedStrikeAccount(client)
                _, history = transactions(client, tx_type="deposit", status=2, limit=100)
                deposits = tuple(history.transactions)
            engine = Engine(
                self.store,
                KupoWatch(http, cfg.kupo_url),
                StrikeMarketClient(http, cfg.strike_api_url),
                account,
                ApprovedPolicies(),
            )
            head = [f"strike_position conditions disabled: {reason}"] if reason else []
            return "\n".join(head + engine.tick(datetime.now(UTC), deposits, updates))
        finally:
            http.close()

    def tick(self) -> None:
        updates = None if self._account is None else self._account.stream_updates()
        self._worker = Worker(lambda: self._tick(updates))
        self._worker.done.connect(self._report)
        self._worker.failed.connect(self._report)
        self._worker.start()


class ApprovalPanel(QWidget):
    """The SAME LangGraph + execute() as ``order_cli``: Run -> HUMAN Approve
    click -> exact bodies, congruence, explanation -> HUMAN Send click ->
    signed send to the selected network's Strike venue (API-wallet signature).
    The network badge heads the panel. No API wallet = Send disabled, reason
    shown and logged (``logs/ui_events.log``)."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_ids: set[str] = set()
        self.plan_path: Path | None = None
        self.run_btn = _button("Run to approval", "primary")
        self.run_btn.clicked.connect(self.run_to_approval)
        self.approve_btn = _button("Approve", "success")
        self.approve_btn.clicked.connect(lambda: self.decide("approve"))
        self.reject_btn = _button("Reject", "danger")
        self.reject_btn.clicked.connect(lambda: self.decide("reject"))
        self.send_btn = _button("Send", "warning")
        self.send_btn.clicked.connect(lambda: self.confirm_send("send"))
        self.abort_btn = _button("Abort send", "danger")
        self.abort_btn.clicked.connect(lambda: self.confirm_send("abort"))
        for b in (self.approve_btn, self.reject_btn, self.send_btn, self.abort_btn):
            b.setEnabled(False)
        self.key_label = QLabel("")
        self.key_label.setObjectName("muted")
        self.hash_label = QLabel("no plan")
        self.hash_label.setObjectName("muted")
        self.checks = QTableWidget(0, 4)
        self.checks.setHorizontalHeaderLabels(("rule", "ok", "expected", "actual"))
        self.plan_view = _table(LEG_COLUMNS)
        self._plan_text: str | None = None
        self.ticket = OrderTicket(self)
        self.raw_view = _mono()
        bar = LAYOUT.hbox()
        for w in (
            self.run_btn,
            self.approve_btn,
            self.reject_btn,
            self.send_btn,
            self.abort_btn,
            self.hash_label,
        ):
            bar.addWidget(w)
        bar.addStretch(1)
        plan_box, raw_box = (
            QGroupBox("Plan legs + deterministic checks"),
            QGroupBox("Raw request / tx + plain explanation"),
        )
        pl = LAYOUT.vbox()
        plan_box.setLayout(pl)
        rl = LAYOUT.vbox()
        raw_box.setLayout(rl)
        pl.addWidget(QLabel("Plans come from the Chat panel (Review) or the Order ticket builder."))
        self.plan_tabs = QTabWidget()
        review = QWidget()
        rv = LAYOUT.vbox()
        review.setLayout(rv)
        rv.addWidget(self.plan_view, 1)
        rv.addWidget(self.checks, 2)
        self.plan_tabs.addTab(review, "Plan legs + checks")
        self.plan_tabs.addTab(self.ticket, "Order ticket")
        from .builders import OnchainSendBuilder # builders reuse this module's widgets

        self.onchain = OnchainSendBuilder()
        self.plan_tabs.addTab(self.onchain, "On-chain send")
        pl.addWidget(self.plan_tabs, 1)
        rl.addWidget(self.raw_view)
        cols = LAYOUT.hbox()
        cols.addWidget(plan_box, 1)
        cols.addWidget(raw_box, 1)
        self.wait = QLabel(self.onchain.wait.text())
        self.wait.setObjectName("wait")
        self.wait.setWordWrap(True)
        self.onchain.waiting.connect(self.wait.setText)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        self.network_badge = network_badge(load_config().profile)
        layout.addWidget(self.network_badge)
        layout.addLayout(bar)
        layout.addWidget(self.wait)
        layout.addWidget(self.key_label)
        layout.addLayout(cols, 1)
        self._wallet, self._key_reason = wallet_or_reason()
        self._show_key_state()
        self._http: RecordedHttp | None = None
        self._graph: _Graph | None = None
        self._shown_hash: str | None = None # the plan_hash the human SEES
        self._config: _ThreadConfig = {"configurable": {"thread_id": "ui"}}
        self._worker: Worker | None = None

    def use_chat_plan(self, plan: StrikePlanProposal) -> None:
        """Chat 'Review': the LLM's StrikePlanProposal enters the SAME path as a
        plan file / the ticket. Nothing runs until the human presses Run."""
        # A new plan invalidates any paused run: no Approve/Send can act on the old hash.
        self._graph = None
        self._shown_hash = None
        for b in (self.approve_btn, self.reject_btn, self.send_btn, self.abort_btn):
            b.setEnabled(False)
        self.plan_path = None
        self._plan_text = plan.model_dump_json()
        self.hash_label.setText("chat plan (LLM proposal): press Run to approval")
        self.plan_tabs.setCurrentIndex(0)
        fill_table(self.plan_view, strike_leg_rows(plan.legs))
        self._loud(
            f"chat -> StrikePlanProposal ({len(plan.legs)} leg(s)): {plan.intent}\n"
            "Press Run to approval: deterministic check, then YOUR approval. The chat cannot approve."
        )

    def _show_key_state(self) -> None:
        self._wallet, self._key_reason = wallet_or_reason()
        if self._wallet is None:
            self.key_label.setText(ui_log("approval", f"SEND DISABLED: {self._key_reason}"))
            self.send_btn.setToolTip(self._key_reason)
        else:
            self.key_label.setText(
                f"Strike API wallet {self._wallet.public_key_hex[:8]}… loaded ({load_config().profile.strike_label})"
            )

    def _loud(self, text: str) -> None:
        self.raw_view.setPlainText(ui_log("approval", text))

    def load_plan(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._loud(f"FAILED: {exc}")
            return
        # A new plan invalidates any paused run: no Approve/Send can act on the old hash.
        self._graph = None
        self._shown_hash = None
        for b in (self.approve_btn, self.reject_btn, self.send_btn, self.abort_btn):
            b.setEnabled(False)
        self.plan_path = path
        self._plan_text = text
        self.hash_label.setText(str(path))

    def use_ticket_plan(self, plan: StrikePlanProposal) -> None:
        # A new plan invalidates any paused run: no Approve/Send can act on the old hash.
        self._graph = None
        self._shown_hash = None
        for b in (self.approve_btn, self.reject_btn, self.send_btn, self.abort_btn):
            b.setEnabled(False)
        self.plan_path = None
        self._plan_text = plan.model_dump_json()
        self.hash_label.setText("order ticket plan (press Run to approval)")
        self.plan_tabs.setCurrentIndex(0)
        self._loud(
            "order ticket -> plan built (same StrikePlanProposal as a plan file); press Run to approval"
        )

    def _start(self) -> str:
        if self._plan_text is None:
            raise ValueError("open a plan file or build one with the order ticket first")
        plan_text = self._plan_text
        cfg = load_config()
        self._http = RecordedHttp(cfg.timeout_s)
        self._wallet, self._key_reason = wallet_or_reason()
        # SAME pipeline + execute() as order_cli; the sender exists only with the key.
        pipeline = StrikePipeline(
            FixedStrikePlanner(plan_text),
            StrikeMarketClient(self._http, cfg.strike_api_url),
            sender=StrikeAuthedClient(self._http, cfg.strike_api_url, self._wallet)
            if self._wallet
            else None,
        )
        self._graph = cast("_Graph", build_strike_graph(pipeline))
        self._config = {"configurable": {"thread_id": f"ui-{time.time_ns()}"}}
        self._graph.invoke({"request": f"plan {self.plan_path or 'order ticket'}"}, self._config)
        return "approval"

    def run_to_approval(self) -> None:
        self.run_btn.setEnabled(False)
        self._worker = Worker(self._start)
        self._worker.done.connect(self._show_approval)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _failed(self, text: str) -> None:
        self._loud(f"FAILED: {text}")
        self.run_btn.setEnabled(True)

    def _show_approval(self, _: str) -> None:
        self._show_key_state()
        if self._graph is None:
            self._loud("FAILED: no pipeline run (press Run to approval)")
            return
        state = self._graph.get_state(self._config).values
        rows = [
            (f"leg {i} {c.id}", c.ok, c.expected, c.actual)
            for i, v in enumerate(validations_of(state))
            for c in v.report.checks
        ] + [
            (f"leg {i} not evaluated", False, s, "")
            for i, v in enumerate(validations_of(state))
            for s in v.not_evaluated
        ]
        self.checks.setRowCount(len(rows))
        for r, (rule, ok, expected, actual) in enumerate(rows):
            for col, value in enumerate(
                (
                    rule,
                    "ok" if ok else "—" if rule.endswith("not evaluated") else "FAIL",
                    expected,
                    actual,
                )
            ):
                self.checks.setItem(r, col, QTableWidgetItem(value))
        self.checks.resizeColumnsToContents()
        if "rejected" in state:
            self._loud(f"REJECTED before approval: {state['rejected']}")
            self.run_btn.setEnabled(True)
            return
        plan = plan_of(state)
        fill_table(self.plan_view, strike_leg_rows(plan.legs))
        self.hash_label.setText(f"plan_hash {plan.plan_hash} — awaiting YOUR approval")
        self._shown_hash = plan.plan_hash
        self.wait.setText(
            "WAITING FOR YOU. Press Approve on this tab. Nothing is built, and there is no signature yet. "
            f"Plan {plan.plan_hash}."
        )
        self.raw_view.setPlainText(
            "Graph paused at the N3 approval interrupt. Nothing is built.\n"
            f"pending receipts: {state['receipts_dir']}"
        )
        self.approve_btn.setText(f"Approve {plan.plan_hash[:12]}…")
        self.approve_btn.setEnabled(True)
        self.reject_btn.setEnabled(True)

    def decide(self, verdict: str) -> None:
        """Called ONLY by the human's button click."""
        if self._graph is None:
            self._loud("NOT DONE: no plan awaiting approval (press Run to approval)")
            return
        plan = plan_of(self._graph.get_state(self._config).values)
        if plan.plan_hash != self._shown_hash:
            self._loud(
                f"NOT DONE: displayed plan_hash {self._shown_hash} != paused graph plan_hash "
                f"{plan.plan_hash}; press Run to approval again"
            )
            return
        decision = PlanDecision(
            decision="approve" if verdict == "approve" else "reject",
            plan_hash=plan.plan_hash,
            approver="ui-human-click",
            ts_ms=time.time_ns() // 1_000_000,
        )
        self.approve_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        graph, config = self._graph, self._config

        def resume() -> str:
            graph.invoke(
                Command(resume=cast("dict[str, object]", json.loads(decision.model_dump_json()))),
                config,
            )
            return "resumed"

        self._worker = Worker(resume)
        self._worker.done.connect(self._show_confirm)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _show_confirm(self, _: str) -> None:
        if self._graph is None:
            self._loud("FAILED: no pipeline run")
            return
        state = self._graph.get_state(self._config).values
        if "rejected" in state:
            self._loud(f"REJECTED: {state['rejected']}")
            self.run_btn.setEnabled(True)
            return
        lines: list[str] = []
        for i, p in enumerate(prepared_of(state)):
            lines.append(f"leg {i}: {p.request.method} {p.request.path}")
            lines.append(p.request.body)
            lines.append(f"congruence ok={p.congruence.report.ok}")
            lines.extend(p.explanation.lines)
        lines.append(f"receipts: {confirm_request_of(state).receipts_dir}")
        plan = plan_of(state)
        self.abort_btn.setEnabled(True)
        self._wallet, self._key_reason = wallet_or_reason()
        if self._wallet is None:
            lines.append(
                f"Paused at the send confirm. NOTHING SENT. SEND DISABLED: {self._key_reason}"
            )
            self.send_btn.setText("Send (disabled: no API wallet)")
            self.send_btn.setEnabled(False)
            self.wait.setText(
                "WAITING FOR YOU, but Send is disabled: no Strike API wallet for this network. "
                "Nothing has been sent."
            )
        else:
            lines.append(
                f"Paused at the send confirm. NOTHING SENT until you click 'Send {plan.plan_hash[:12]}…'."
            )
            self.send_btn.setText(
                f"Send {plan.plan_hash[:12]}… to {load_config().profile.strike_label}"
            )
            self.send_btn.setEnabled(True)
            self.wait.setText(
                f"WAITING FOR YOU. Press Send. Nothing has been sent. Plan {plan.plan_hash}."
            )
        self.raw_view.setPlainText(ui_log("approval", "\n".join(lines)))

    def confirm_send(self, verdict: str) -> None:
        """Called ONLY by the human's Send / Abort click; resumes into execute()."""
        if self._graph is None:
            self._loud("NOT DONE: nothing awaiting the send confirm")
            return
        plan = plan_of(self._graph.get_state(self._config).values)
        if plan.plan_hash != self._shown_hash:
            self._loud(
                f"NOT SENT: displayed plan_hash {self._shown_hash} != paused graph plan_hash "
                f"{plan.plan_hash}; press Run to approval again"
            )
            return
        selected = load_config().profile
        if verdict == "send" and plan.network != selected.strike_venue:
            self._loud(
                f"NOT SENT: NETWORK MISMATCH: the approved plan is for Strike {plan.network}, the "
                f"selected network is {selected.label}. An approval never carries over to another "
                "network; press Run to approval again on the intended network."
            )
            return
        if verdict == "send":
            self._wallet, self._key_reason = wallet_or_reason()
        if verdict == "send" and self._wallet is None:
            self._loud(f"NOT SENT: {self._key_reason}")
            return
        confirm = StrikeConfirm(
            decision="send" if verdict == "send" else "abort",
            plan_hash=plan.plan_hash,
            approver="ui-human-click",
            ts_ms=time.time_ns() // 1_000_000,
        )
        self.send_btn.setEnabled(False)
        self.abort_btn.setEnabled(False)
        graph, config = self._graph, self._config

        def resume() -> str:
            graph.invoke(
                Command(resume=cast("dict[str, object]", json.loads(confirm.model_dump_json()))),
                config,
            )
            return "executed"

        self._worker = Worker(resume)
        self._worker.done.connect(self._show_executed)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _show_executed(self, _: str) -> None:
        if self._graph is None:
            self._loud("FAILED: no pipeline run")
            return
        state = self._graph.get_state(self._config).values
        attempted = SENT.validate_json(state["executed_json"]) if "executed_json" in state else ()
        lines: list[str] = []
        for result in attempted:
            if result.outcome == "unknown":
                lines.append(
                    f"UNKNOWN leg {result.leg}: {result.method} {result.path}. "
                    "Strike may have received it; reconcile before retrying."
                )
            else:
                lines.append(
                    f"RESPONSE leg {result.leg}: {result.method} {result.path} "
                    f"-> HTTP {result.status}. Execution or settlement remains unconfirmed."
                )
                if result.path.startswith("/v2/algo/twap"):
                    lines.append(
                        "TWAP: confirm the native strategy status through strategyUpdate "
                        "or GET /v2/algo/twap. Request acceptance is not completion."
                    )
                elif result.path in ("/v2/marginMode", "/v2/leverage", "/v2/symbols"):
                    lines.append("Settings: confirm the applied values through GET /v2/symbols.")
        if "rejected" in state:
            lines.append(f"NOT SENT / STOPPED: {state['rejected']}")
        confirm = confirm_request_of(state)
        attempted_ids = {r.client_order_id for r in attempted if r.client_order_id}
        self.sent_ids |= attempted_ids
        if attempted_ids:
            lines.append(
                "N9s: waiting for ORDER_TRADE_UPDATE on the user stream (Account tab badge must be LIVE) for "
                + ", ".join(sorted(attempted_ids))
            )
        lines.append(f"receipts: {confirm.receipts_dir}")
        self._loud("\n".join(lines))
        self.run_btn.setEnabled(True)

    def on_order_update(self, u: OrderTradeUpdate) -> None:
        """N9s: order status / fills for orders this tab sent, as served by the stream."""
        d = u.data
        if d.c not in self.sent_ids:
            return
        fill = f" fill {d.l} @ {d.L} (filled {d.z}/{d.q}, commission {d.n})" if d.t > 0 else ""
        self.raw_view.appendPlainText(ui_log("approval", f"N9s {d.c} {d.s}: {d.x} -> {d.X}{fill}"))


class SettingsPanel(QWidget):
    """LLM planner provider settings -> the per-user ``<user config dir>/llm.json``
    (mode 0600, atomic write; never the git-tracked config/struck.json).

    Secrets: the "Secrets" box unlocks Struck's encrypted secret store
    (struck.secrets, one file on every OS) with a masked master-password field,
    then writes ONE entry from a masked (Password echo)
    field that is never pre-filled and is cleared after every attempt; the
    panel shows only present / missing per entry, never a value. A custom LLM
    host gets secret llm_custom only. Fields are cleared after every attempt.

    Network toggle (the rows of struck.network.PROFILES) -> ``<user config dir>/network.json``
    (0600, atomic). Takes effect on the next start (open streams and clients
    keep the network they were built for); a --network flag overrides it."""

    PROVIDERS: tuple[str, ...] = tuple(PROVIDERS)
    saved = Signal() # the Chat dock re-reads its provider

    # Endpoint examples (display only — the model rejects wrong shapes loudly).
    _ENDPOINT_EXAMPLES = {
        "ollama": " (e.g. http://127.0.0.1:11434)",
        "vllm": " (e.g. http://127.0.0.1:8010/v1 — OpenAI-compatible)",
        "azure_openai": " (e.g. https://<resource>.openai.azure.com)",
    }

    def __init__(
        self, llm_path: Path = LLM_CONFIG_PATH, network_path: Path = NETWORK_SETTING_PATH
    ) -> None:
        super().__init__()
        self.network_path = network_path
        self.network = QComboBox()
        self.network.addItems(NETWORKS)
        self.network.setCurrentText(load_config().network)
        save_net = _button("Save network (applies on restart)", "warning")
        save_net.clicked.connect(self.save_network)
        self.network_state = QLabel(f"running on: {load_config().profile.label}")
        self.network_state.setWordWrap(True)
        net_box = QGroupBox(
            "Network (one setting: Strike REST/WS, Cardano magic, Koios, addresses)"
        )
        nv = LAYOUT.vbox()
        net_box.setLayout(nv)
        nv.addWidget(self.network)
        nv.addLayout(_compact_row(save_net))
        nv.addWidget(self.network_state)

        # -- chain backend (node | koios) + node socket per network: the local
        # node section the registration dialogue leaves to Settings (registration
        # writes the koios default; a real socket is mounted later by hand).
        chain_box = QGroupBox("Cardano backend (node | koios) — socket + cardano-cli")
        cv = LAYOUT.vbox()
        chain_box.setLayout(cv)
        self.backend_pick = QComboBox()
        self.backend_pick.addItems(("koios", "node"))
        self.socket_preprod = QLineEdit()
        self.socket_preprod.setPlaceholderText("preprod node.socket: absolute path; empty = none")
        self.socket_mainnet = QLineEdit()
        self.socket_mainnet.setPlaceholderText("mainnet node.socket: absolute path; empty = none")
        self.cli_path = QLineEdit()
        self.cli_path.setPlaceholderText("cardano-cli absolute path")
        chain_save = _button("Save chain config", "default")
        chain_save.setObjectName("save_chain")
        chain_save.clicked.connect(self.save_chain_config)
        self.chain_state = QLabel("")
        self.chain_state.setWordWrap(True)
        cform = LAYOUT.form()
        cform.addRow("backend", self.backend_pick)
        cform.addRow(self.socket_preprod)
        cform.addRow(self.socket_mainnet)
        cform.addRow("cardano-cli", self.cli_path)
        cv.addLayout(cform)
        cv.addLayout(_compact_row(chain_save))
        cv.addWidget(self.chain_state)
        nv.addWidget(chain_box)
        self._load_chain_config()
        self.llm_path = llm_path # per-user <user config dir>/llm.json, never the tracked config
        self.provider = QComboBox()
        self.provider.addItems(self.PROVIDERS)
        # The model id is SERVED by the provider (its own model-list endpoint):
        # Load fills the list. The field stays editable only as an explicit
        # human override when a host cannot list (the error is shown anyway).
        self.model_pick = QComboBox()
        self.model_pick.setEditable(True)
        self.model_pick.setPlaceholderText("model id — press Load served models")
        self.load_models_btn = _button("Load served models", "default")
        self.load_models_btn.clicked.connect(self.load_models)
        model_row = LAYOUT.hbox()
        model_row.addWidget(self.model_pick, 1)
        model_row.addWidget(self.load_models_btn)
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText(
            "endpoint URL (required for vllm / ollama / azure_openai; optional otherwise; no default)"
        )
        # Every LlmConfig field has a visible control: no hidden setting survives a save.
        self.allow_custom_host = QCheckBox(
            "allow a NON-official host for a hosted provider (explicit opt-in; https only)"
        )
        self.state = QLabel("")
        self.state.setObjectName("muted")
        self.state.setWordWrap(True)
        self.key_label = QLabel("")
        self.provider.currentTextChanged.connect(self._show_key)
        save = _button("Save LLM settings", "primary")
        save.clicked.connect(self.save)
        form = LAYOUT.form()
        form.addRow("provider", self.provider)
        form.addRow("model (served list)", model_row)
        form.addRow("base_url", self.base_url)
        form.addRow("allow_custom_host", self.allow_custom_host)
        form.addRow("API key (secret store)", self.key_label)
        box = QGroupBox("LLM provider for the Chat dock (init_chat_model)")
        v = LAYOUT.vbox()
        box.setLayout(v)
        v.addLayout(form)
        v.addLayout(_compact_row(save))
        v.addWidget(self.state)
        self.master = QLineEdit()
        self.master.setObjectName("master_password")
        self.master.setEchoMode(QLineEdit.EchoMode.Password)
        self.master.setPlaceholderText("master password (masked; never stored, never logged)")
        self.master.returnPressed.connect(self.unlock_store)
        unlock = _button("Unlock secret store", "primary")
        unlock.setObjectName("unlock_store")
        unlock.clicked.connect(self.unlock_store)
        self.secret_entry = QComboBox()
        self.secret_entry.setObjectName("secret_entry")
        self.secret_entry.addItems(ENTRIES)
        self.secret_value = QLineEdit()
        self.secret_value.setObjectName("secret_value")
        self.secret_value.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_value.setPlaceholderText("value (masked; never shown, never pre-filled)")
        self.secret_value.returnPressed.connect(self.store_secret)
        store = _button("Store secret", "primary")
        store.setObjectName("store_secret")
        store.clicked.connect(self.store_secret)
        delete = _button("Delete secret", "danger")
        delete.setObjectName("delete_secret")
        delete.clicked.connect(self.delete_secret)
        self.secret_state = QLabel("")
        self.secret_state.setWordWrap(True)
        self.sources = QLabel("")
        self.sources.setObjectName("mono")
        sources_box = QGroupBox("Secrets (encrypted store; present / missing only)")
        sv = LAYOUT.vbox()
        sources_box.setLayout(sv)
        sform = LAYOUT.form()
        sform.addRow("master password", self.master)
        sform.addRow(_compact_row(unlock))
        sform.addRow("entry", self.secret_entry)
        sform.addRow("value", self.secret_value)
        sv.addLayout(sform)
        sv.addLayout(_compact_row(store))
        sv.addLayout(_compact_row(delete))
        sv.addWidget(self.secret_state)
        sv.addWidget(self.sources)
        # -- guarded LOG OUT lives HERE, in the Secrets box (operator 2026-10-22:
        # "still no deletion of master" — it was buried at the tab bottom) and
        # COMPACT (no screen-wide buttons). NO dead buttons: everything stays
        # clickable and every click SPEAKS — the audit sheet names every asset
        # that would be lost; the delete click itself runs the LIVE audit and
        # refuses by name when anything exists (the guard is in the action,
        # not in a grayed-out widget). Nothing here signs or sends.
        reset_note = QLabel(
            "Log out deletes: secret store, Strike env, registration marker, LLM config, "
            "empty trigger store. network.json and endpoints stay. Refused BY NAME while "
            "assets exist."
        )
        reset_note.setWordWrap(True)
        self.reset_sheet = QLabel("")
        self.reset_sheet.setObjectName("mono")
        self.reset_sheet.setWordWrap(True)
        self.reset_audit_btn = _button("Asset audit (read-only)", "default")
        self.reset_audit_btn.setObjectName("reset_audit")
        self.reset_audit_btn.clicked.connect(self._reset_audit)
        self.reset_token = QLineEdit()
        self.reset_token.setObjectName("reset_token")
        self.reset_token.setPlaceholderText("type: LOG OUT")
        self.reset_token.setMaximumWidth(220)
        self.reset_btn = _button("Delete master (log out)", "danger")
        self.reset_btn.setObjectName("reset_delete")
        self.reset_btn.clicked.connect(self._reset_run)
        reset_row = LAYOUT.hbox()
        reset_row.addWidget(self.reset_audit_btn)
        reset_row.addWidget(self.reset_token)
        reset_row.addWidget(self.reset_btn)
        reset_row.addStretch(1)
        master_row = QGroupBox("master (log out) — audited, refuses while assets exist")
        masterv = LAYOUT.vbox()
        master_row.setLayout(masterv)
        for w in (reset_note, self.reset_sheet):
            masterv.addWidget(w)
        masterv.addLayout(reset_row)
        sv.addWidget(master_row)

        self.strike_btn = _button("Connect Strike API wallet…", "primary")
        self.strike_btn.setObjectName("connect_strike")
        self.strike_btn.clicked.connect(self.open_strike_dialog)
        self.strike_dialog: StrikeConnectDialog | None = None
        self._model_worker: Worker | None = None
        strike_box = QGroupBox("Strike API wallet (asked on unlock; you pick preprod or mainnet)")
        kv = LAYOUT.vbox()
        strike_box.setLayout(kv)
        kv.addLayout(_compact_row(self.strike_btn))
        # Never truncate: the whole tab scrolls when it does not fit (layout.py).
        content = QWidget()
        column = LAYOUT.vbox()
        for card in (net_box, box, sources_box, strike_box):
            column.addWidget(card)
        column.addStretch(1)
        content.setLayout(column)
        outer = LAYOUT.vbox()
        self.setLayout(outer)
        outer.addWidget(LAYOUT.scroll(content))
        self._show_key(self.provider.currentText()) # first paint follows the initial provider
        self.show_state()

    def _reset_audit_job(self) -> str:
        report = reset_audit(load_config())
        hard = reset_report_text(report)
        return ("CLEAR\n" if report.clear else "HOLDS\n") + hard # first line is the gate

    def _reset_audit_failed(self, text: str) -> None:
        self.reset_sheet.setText(ui_log("settings", f"[AUDIT FAILED: {text}]"))

    def _reset_audit(self) -> None:
        self._reset_worker = Worker(self._reset_audit_job) # kept alive: no GC mid-thread
        self._reset_worker.done.connect(self._reset_sheet)
        self._reset_worker.failed.connect(self._reset_audit_failed)
        self._reset_worker.start()

    def _reset_sheet(self, text: str) -> None:
        verdict, _, rest = text.partition("\n")
        self.reset_sheet.setText(ui_log("settings", rest))
        if verdict != "CLEAR":
            self.secret_state.setText(
                ui_log(
                    "settings",
                    "[LOG OUT REFUSED — ASSETS/STATE PRESENT: the sheet above names them; "
                    "nothing was deleted.]",
                )
            )

    def _reset_started(self, receipt: str) -> None:
        self.secret_state.setText(
            ui_log(
                "settings",
                f"[LOGGED OUT: credential surface deleted; receipt {receipt}] Restart Struck to register again.",
            )
        )

    def _reset_failed(self, text: str) -> None:
        # Includes the FULL guard sheet: reset() re-audits live and refuses by name.
        self.secret_state.setText(ui_log("settings", f"[LOG OUT NOT DONE: {text}]"))

    def _reset_job(self) -> str:
        return str(reset_delete())

    def _reset_run(self) -> None:
        """No dead gating: the delete click itself runs the LIVE audit
        (struck.reset.reset re-audits, fail-closed) and refuses with the named
        sheet when anything exists. The exact typed token is the human's
        confirmation, checked HERE, every click."""
        if self.reset_token.text() != "LOG OUT":
            self.secret_state.setText(
                ui_log(
                    "settings",
                    "[type exactly LOG OUT in the token field to confirm — the audit sheet "
                    "shows what the guard checks (and any assets it found)]",
                )
            )
            self._reset_audit() # speak: show the sheet right now, refuse later anyway
            return
        self.reset_token.clear() # disarm immediately: one token per reset
        self._reset_worker = Worker(self._reset_job) # kept alive: no GC mid-thread
        self._reset_worker.done.connect(self._reset_started)
        self._reset_worker.failed.connect(self._reset_failed)
        self._reset_worker.start()

    def open_strike_dialog(self) -> None:
        """Human click only: the masked register / import / verify dialog."""
        try:
            self.strike_dialog = StrikeConnectDialog(self)
        except StrikeCredentialsError as exc:
            self.secret_state.setText(ui_log("settings", f"[STRIKE: {exc}]"))
            return
        self.strike_dialog.finished.connect(self.show_state)
        self.strike_dialog.open()

    def _show_key(self, provider: str) -> None:
        """Per-provider field guidance — some providers are a KEYED API (openai,
        anthropic, …: a secret-store entry), some are an ENDPOINT (ollama, vllm:
        a URL, ollama keyless), some both (azure_openai) — plus the key status
        only (present / missing; never the value)."""
        llm = cast("LlmProvider", provider)
        info = PROVIDERS[llm]
        self.key_label.setText(key_state(llm))
        if info.endpoint_required:
            self.base_url.setPlaceholderText(
                f"endpoint URL — REQUIRED for {provider}"
                + self._ENDPOINT_EXAMPLES.get(provider, "")
            )
        elif info.keyed:
            self.base_url.setPlaceholderText(
                "endpoint URL — the provider's official host is used; custom host needs allow_custom_host"
            )
        else: # keyless endpoint provider: the URL is the whole configuration
            self.base_url.setPlaceholderText(
                f"endpoint URL — {provider} is keyless: the endpoint IS the configuration"
                + self._ENDPOINT_EXAMPLES.get(provider, "")
            )

    def _show_secrets(self) -> None:
        try:
            states = entry_states()
        except SecretError as exc:
            self.sources.setText(f"SECRET STORE UNAVAILABLE: {exc}")
            return
        self.sources.setText("\n".join(f"{name:<34} {state}" for name, state in states))

    def unlock_store(self) -> None:
        """Human click only: the masked master password unlocks (or creates) the store."""
        master = self.master.text()
        self.master.clear()
        try:
            unlock(master)
        except SecretError as exc:
            self.secret_state.setText(ui_log("settings", f"[STORE NOT UNLOCKED: {exc}]"))
            return
        self.secret_state.setText(ui_log("settings", f"[STORE UNLOCKED: {_secrets.SECRETS_PATH}]"))
        self.show_state()
        self.saved.emit() # the Chat dock re-reads its provider (keys now readable)
        self._ask_strike_key()

    def _ask_strike_key(self) -> None:
        """Unlock creates the store. With no API wallet saved, ask for one.

        The human picks preprod or mainnet in the dialog and pastes the key.
        One saved environment is enough; the other stays empty until they add it.
        """
        try:
            missing = not has_secret(entry_for("testnet")) and not has_secret(entry_for("mainnet"))
        except SecretError:
            return
        if missing:
            self.open_strike_dialog()

    def store_secret(self) -> None:
        """Human click only: the masked value goes to the store, then the field is cleared."""
        name = self.secret_entry.currentText()
        value = self.secret_value.text()
        self.secret_value.clear()
        try:
            set_secret(name, value)
        except SecretError as exc:
            self.secret_state.setText(ui_log("settings", f"[KEY NOT STORED: {exc}]"))
            return
        self.secret_state.setText(
            ui_log("settings", f"[KEY STORED: secret {name}; value not shown]")
        )
        self.show_state()

    def delete_secret(self) -> None:
        name = self.secret_entry.currentText()
        try:
            delete_secret(name)
        except SecretError as exc:
            self.secret_state.setText(ui_log("settings", f"[KEY NOT DELETED: {exc}]"))
            return
        self.secret_state.setText(ui_log("settings", f"[KEY DELETED: secret {name}]"))
        self.show_state()

    def show_state(self) -> None:
        self._show_key(self.provider.currentText())
        self._show_secrets()
        try:
            llm = llm_config_of(self.llm_path)
            self.provider.setCurrentText(llm.provider)
            self.model_pick.clear()
            self.model_pick.addItem(llm.model)
            self.base_url.setText(llm.base_url or "")
            self.allow_custom_host.setChecked(llm.allow_custom_host)
            chat_model(llm) # builds the stock client only; no request is sent
            self.state.setText(
                ui_log(
                    "settings",
                    f"[LLM READY: {llm.provider} / {llm.model} @ {llm.base_url or 'provider default endpoint'}"
                    f"; allow_custom_host={llm.allow_custom_host}]",
                )
            )
        except (LlmConfigError, ValueError, OSError) as exc:
            self.state.setText(ui_log("settings", f"[LLM NOT CONFIGURED: {exc}]"))

    def _load_chain_config(self) -> None:
        """Paint the CURRENT config/struck.json values (read-only meaningful
        fields; never invents a value)."""
        try:
            cfg = load_config()
            self.backend_pick.setCurrentText(cfg.chain_backend)
            raw: dict[str, object] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            sockets = cast("dict[str, object]", raw.get("node_socket", {}))
            self.socket_preprod.setText(str(sockets.get("preprod") or ""))
            self.socket_mainnet.setText(str(sockets.get("mainnet") or ""))
            self.cli_path.setText(str(raw.get("cardano_cli_path") or ""))
            self.chain_state.setText(
                ui_log("settings", f"[CHAIN CONFIG LOADED: backend {cfg.chain_backend}]")
            )
        except (ConfigError, ValueError, OSError) as exc:
            self.chain_state.setText(ui_log("settings", f"[CHAIN CONFIG UNLOADED: {exc}]"))

    def save_chain_config(self) -> None: # noqa: C901
        """Human click only. Merges the edited fields into config/struck.json,
        validates the WHOLE file through the strict model BEFORE writing (the
        node socket must be an absolute local path; every socket slot named),
        writes private 0600, and never touches the selected network."""
        try:
            raw: dict[str, object] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.chain_state.setText(ui_log("settings", f"[CHAIN CONFIG NOT SAVED: {exc}]"))
            return
        raw_sockets = raw.get("node_socket")
        sockets: dict[str, object] = (
            {str(k): v for k, v in raw_sockets.items()} if isinstance(raw_sockets, dict) else {}
        )
        updated: dict[str, object] = {}
        for name in NETWORKS:
            field = self.socket_preprod if name == "preprod" else self.socket_mainnet
            text = field.text().strip()
            updated[name] = text if text else None
        sockets.clear()
        sockets.update(updated)
        raw["node_socket"] = sockets
        raw["chain_backend"] = self.backend_pick.currentText()
        if self.cli_path.text().strip():
            raw["cardano_cli_path"] = self.cli_path.text().strip()
        try:
            StruckConfig.model_validate(raw) # whole-file schema check, nothing mutated yet
        except ValidationError as exc:
            errors: list[dict[str, object]] = cast("list[dict[str, object]]", exc.errors())
            first = errors[0] if errors else {"msg": "?", "loc": ()}
            self.chain_state.setText(
                ui_log(
                    "settings",
                    f"[CHAIN CONFIG NOT SAVED (invalid): {exc.error_count()} error(s): "
                    f"{first.get('msg')} at {first.get('loc')}]",
                )
            )
            return
        try:
            write_private(CONFIG_PATH, (json.dumps(raw, indent=2) + "\n").encode("utf-8"))
        except (OSError, ValueError) as exc:
            self.chain_state.setText(ui_log("settings", f"[CHAIN CONFIG NOT SAVED: {exc}]"))
            return
        self.chain_state.setText(
            ui_log(
                "settings",
                f"[CHAIN CONFIG SAVED: backend {raw['chain_backend']}; restart Struck to apply. "
                f"Running on: {load_config().chain_backend}]",
            )
        )

    def save_network(self) -> None:
        """Human click only. Saves the choice; nothing switches under a running session."""
        try:
            chosen = profile(self.network.currentText())
            save_network_setting(chosen.name, self.network_path)
        except (ValueError, OSError) as exc:
            self.network_state.setText(ui_log("settings", f"[NETWORK NOT SAVED: {exc}]"))
            return
        self.network_state.setText(
            ui_log(
                "settings",
                f"[NETWORK SAVED: {chosen.label}; restart Struck to switch. "
                f"Running on: {load_config().profile.label}]",
            )
        )

    def load_models(self) -> None:
        """Human click: ask the provider itself which models it serves. The
        form's current values (unsaved edits included) route the request
        exactly like chat_model; failures are loud, never an empty fallback."""
        try:
            llm = LlmConfig(
                provider=cast("LlmProvider", self.provider.currentText()),
                model=self.model_pick.currentText().strip(),
                base_url=self.base_url.text().strip() or None,
                allow_custom_host=self.allow_custom_host.isChecked(),
            )
        except (LlmConfigError, ValueError) as exc:
            self.state.setText(ui_log("settings", f"[MODELS NOT LISTED: {exc}]"))
            return
        self.load_models_btn.setEnabled(False)
        self.state.setText(ui_log("settings", f"[LISTING MODELS: {llm.provider}…]"))

        def fetch() -> str:
            return ",".join(list_models(llm))

        self._model_worker = Worker(fetch)
        self._model_worker.done.connect(self._models_loaded)
        self._model_worker.failed.connect(self._models_failed)
        self._model_worker.start()

    def _models_loaded(self, joined: str) -> None:
        served = joined.split(",") if joined else []
        keep = self.model_pick.currentText()
        self.model_pick.clear()
        self.model_pick.addItems(served)
        self.model_pick.setCurrentText(keep if keep in served else (served[0] if served else ""))
        self.load_models_btn.setEnabled(True)
        self.state.setText(ui_log("settings", f"[MODELS SERVED: {len(served)}]"))

    def _models_failed(self, reason: str) -> None:
        self.load_models_btn.setEnabled(True)
        self.state.setText(ui_log("settings", f"[MODELS NOT LISTED: {reason}]"))

    def save(self) -> None:
        try:
            llm = LlmConfig(
                provider=cast("LlmProvider", self.provider.currentText()),
                model=self.model_pick.currentText().strip(),
                base_url=self.base_url.text().strip() or None,
                allow_custom_host=self.allow_custom_host.isChecked(),
            )
            save_llm_config(llm, self.llm_path) # 0600, atomic
        except (ValueError, OSError) as exc:
            self.state.setText(ui_log("settings", f"[NOT SAVED: {exc}]"))
            return
        self.show_state()
        self.saved.emit()


class KeyedTable:
    """Stock ``QStandardItemModel`` + ``QTableView``; rows keyed by column 0.
    ``upsert`` writes served strings into cells in place (never clears)."""

    def __init__(self, headers: tuple[str, ...]) -> None:
        self.headers = headers
        self.model = QStandardItemModel(0, len(headers))
        self.model.setHorizontalHeaderLabels(list(headers))
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)

    def row_of(self, key: str) -> int | None:
        found = self.model.findItems(key, Qt.MatchFlag.MatchExactly, 0)
        return found[0].row() if found else None

    def upsert(self, key: str, values: dict[str, str]) -> None:
        row = self.row_of(key)
        if row is None:
            row = self.model.rowCount()
            self.model.appendRow([QStandardItem("") for _ in self.headers])
            self.model.setItem(row, 0, QStandardItem(key))
        for col, name in enumerate(self.headers):
            if name in values:
                self.model.setItem(row, col, QStandardItem(values[name]))

    def remove(self, key: str) -> None:
        row = self.row_of(key)
        if row is not None:
            self.model.removeRow(row)

    def keys(self) -> list[str]:
        items = [self.model.item(r, 0) for r in range(self.model.rowCount())]
        return [i.text() for i in items if i is not None]

    def cell(self, key: str, name: str) -> str:
        row = self.row_of(key)
        item = self.model.item(row, self.headers.index(name)) if row is not None else None
        return item.text() if item is not None else ""


class AccountPanel(QWidget):
    """LIVE account/positions/orders: user stream (``/ws/user-api``) plus the
    spec's REST resync (``/v2/account``, ``/v2/positions``) after every
    (re)subscribe. Values are placed as served; rows update in place."""

    order_update = Signal(object) # OrderTradeUpdate, for N9s in the Approval tab

    def __init__(self) -> None:
        super().__init__()
        self.badge = QLabel("")
        self.badge.setObjectName("muted")
        self.account = KeyedTable(
            (
                "account",
                "wallet_balance",
                "available_balance",
                "unrealized_pnl",
                "margin_balance",
                "total_margin",
                "position_initial_margin",
                "maintenance_margin",
                "copy_reserved_balance",
                "copy_deployed_margin",
                "copy_profit_share_reserved",
            )
        )
        self.positions = KeyedTable(
            (
                "symbol",
                "size",
                "entry_price",
                "leverage",
                "margin_mode",
                "upnl",
                "maintenance_margin",
                "liquidation_price",
                "bankruptcy_price",
                "position_seq",
            )
        )
        self.strategies = KeyedTable(("id", "kind", "symbol", "status", "detail"))
        self.orders = KeyedTable(
            (
                "client_order_id",
                "symbol",
                "side",
                "type",
                "status X",
                "exec x",
                "size q",
                "price p",
                "filled z",
                "last fill l",
                "last price L",
                "commission n",
                "realized rp",
                "close reason",
            )
        )
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        layout.addWidget(self.badge)
        for title, t, stretch in (
            ("Balances", self.account, 1),
            ("Positions", self.positions, 2),
            ("Orders and fills", self.orders, 2),
            ("Strategies", self.strategies, 1),
        ):
            layout.addWidget(QLabel(title))
            layout.addWidget(t.view, stretch)
        self._wallet, self._reason = wallet_or_reason()
        self._stream: StrikeStream | None = None
        self._worker: Worker | None = None
        self._snapshot: tuple[Account, PositionsResponse] | None = None
        self._positions_only = False
        self._position_seq: dict[str, int] = {}
        self._stream_updates: deque[AccountUpdate] = deque(maxlen=64)
        self._offset_ms: int | None = None # Strike served clock - local clock
        self._clock_worker: Worker | None = None
        self.set_badge(f"KEY NOT SET: {self._reason}" if self._wallet is None else "NOT CONNECTED")

    def set_badge(self, text: str) -> None:
        self.badge.setText(ui_log("account", f"[{text}]"))

    def start(self) -> None:
        """Reads Strike's served clock first (off the UI thread), then logs on
        with a timestamp on THAT clock (account.server_offset_ms)."""
        if self._stream is not None or (
            self._clock_worker is not None and self._clock_worker.isRunning()
        ):
            return
        self._wallet, self._reason = wallet_or_reason()
        if self._wallet is None:
            self.set_badge(f"KEY NOT SET: {self._reason}")
            return
        self.set_badge("READING SERVER TIME")
        self._clock_worker = Worker(self._read_offset)
        self._clock_worker.done.connect(self._open_stream)
        self._clock_worker.failed.connect(self._clock_failed)
        self._clock_worker.start()

    def _read_offset(self) -> str:
        cfg = load_config()
        http = RecordedHttp(cfg.timeout_s)
        try:
            self._offset_ms = server_offset_ms(StrikeMarketClient(http, cfg.strike_api_url))
        finally:
            http.close()
        return "offset"

    def logon_request(self) -> list[dict[str, object]]:
        if self._wallet is None or self._offset_ms is None:
            raise StrikeCredentialsError("no API wallet or no served server time: not logging on")
        now_ms = time.time_ns() // 1_000_000 + self._offset_ms
        return [{"id": 1, "method": "session.logon", "params": logon_params(self._wallet, now_ms)}]

    def _clock_failed(self, error: str) -> None:
        self.set_badge(f"FAILED: server time not read, not logging on: {error}")

    def _open_stream(self, _: str = "") -> None:
        net = load_config().profile
        self._stream = StrikeStream(
            net.strike_user_ws,
            f"strike-{net.strike_venue}-userstream",
            self.logon_request,
            user_stream=True,
        )
        self._stream.state.connect(self.set_badge)
        self._stream.message.connect(self.on_message)
        self._stream.open()

    def on_message(self, msg: dict[str, object]) -> None:
        if msg.get("id") == 1 and msg.get("status") == 200:
            if self._stream is not None:
                result = msg.get("result")
                account_id = result.get("account_id") if isinstance(result, dict) else None
                acct = account_id if isinstance(account_id, str) and account_id else None
                self._stream.send(ws_request("subscribe", "userstream", 2, account_id=acct))
            self.set_badge("SUBSCRIBING")
        elif msg.get("e") == "rfqUpdate" and isinstance(msg.get("data"), dict):
            rfq = RfqUpdate.model_validate(msg)
            self.strategies.upsert(
                rfq.data.rfq_id,
                {
                    "kind": "rfq",
                    "symbol": rfq.data.symbol,
                    "status": rfq.data.status,
                    "detail": rfq.data.requested_size,
                },
            )
        elif msg.get("e") == "strategyUpdate" and isinstance(msg.get("data"), dict):
            data = cast("dict[str, object]", msg["data"])
            kind = str(data.get("event_type") or data.get("status") or "strategy")
            key = str(data.get("bot_id") or data.get("strategy_id") or kind)
            self.strategies.upsert(
                key,
                {
                    "kind": kind,
                    "symbol": str(data.get("symbol") or data.get("market") or ""),
                    "status": str(data.get("status") or ""),
                    "detail": str(
                        data.get("filled_size")
                        or data.get("realized_pnl")
                        or data.get("error")
                        or ""
                    ),
                },
            )
        elif msg.get("id") == 2 and "result" in msg:
            self.set_badge("LIVE (resyncing REST snapshot)")
            self.resync(positions_only=False)
        elif msg.get("e") == "error":
            self.set_badge(f"FAILED: {json.dumps(msg.get('error'))}")
        elif msg.get("e") == "ORDER_TRADE_UPDATE":
            self.apply_order(OrderTradeUpdate.model_validate(msg))
        elif (
            msg.get("e") == "ACCOUNT_UPDATE"
            and isinstance(msg.get("data"), dict)
            and ("event_type" in cast("dict[str, object]", msg["data"]))
        ):
            event = cast("dict[str, object]", msg["data"])
            self.set_badge(f"ACCOUNT {event.get('event_type')}")
        elif (
            msg.get("e") == "ACCOUNT_UPDATE"
            and isinstance(msg.get("data"), dict)
            and "B" in cast("dict[str, object]", msg["data"])
        ):
            self.apply_account(AccountUpdate.model_validate(msg))

    def apply_order(self, u: OrderTradeUpdate) -> None:
        d = u.data
        key = d.c or str(d.i)
        values = {
            "symbol": d.s,
            "side": d.S,
            "type": d.o,
            "status X": d.X,
            "exec x": d.x,
            "size q": d.q,
            "price p": d.p,
            "filled z": d.z,
            "close reason": d.cr,
        }
        if d.t > 0: # a fill exactly when t > 0 (userstream :145)
            values |= {
                "last fill l": d.l,
                "last price L": d.L,
                "commission n": d.n,
                "realized rp": d.rp,
            }
        self.orders.upsert(key, {k: v for k, v in values.items() if v != "" or k == "close reason"})
        self.order_update.emit(u)

    def stream_updates(self) -> tuple[AccountUpdate, ...]:
        """ACCOUNT_UPDATE messages this socket already validated, oldest first."""
        return tuple(self._stream_updates)

    def apply_account(self, u: AccountUpdate) -> None:
        self._stream_updates.append(u)
        if u.data.B:
            self.account.upsert("account", {"wallet_balance": u.data.B[0].wb})
        for p in u.data.P:
            if p.position_seq < self._position_seq.get(p.position_id, -1):
                continue # older than what we hold for this position_id (userstream :196)
            self._position_seq[p.position_id] = p.position_seq
            if p.pa == "0":
                self.positions.remove(p.s)
                continue
            self.positions.upsert(
                p.s,
                {
                    "size": p.pa,
                    "entry_price": p.ep,
                    "leverage": str(p.leverage),
                    "margin_mode": p.mt,
                    "position_seq": str(p.position_seq),
                },
            )
        if u.data.P:
            # upnl / margin / liquidation are not in the stream: re-read the served
            # /v2/positions (userstream :195 "or poll /v2/positions").
            self.resync(positions_only=True)

    def _fetch(self) -> str:
        if self._wallet is None:
            raise StrikeCredentialsError(self._reason)
        cfg = load_config()
        http = RecordedHttp(cfg.timeout_s)
        try:
            client = StrikeAuthedClient(http, cfg.strike_api_url, self._wallet)
            self._snapshot = (client.account()[1], client.positions()[1])
        finally:
            http.close()
        return "snapshot"

    def resync(self, positions_only: bool) -> None:
        self._positions_only = positions_only
        self._worker = Worker(self._fetch)
        self._worker.done.connect(self._snapshot_done)
        self._worker.failed.connect(self._snapshot_failed)
        self._worker.start()

    def _snapshot_done(self, _text: str) -> None:
        if self._snapshot is not None:
            self.apply_snapshot(*self._snapshot)

    def _snapshot_failed(self, text: str) -> None:
        sent = (
            f" (sent public key {self._wallet.public_key_hex[:16]}…)"
            if self._wallet is not None
            else f" ({self._reason})"
        )
        self.set_badge(f"FAILED: {text}{sent}")

    def apply_snapshot(self, acc: Account, pos: PositionsResponse) -> None:
        if not self._positions_only:
            self.account.upsert(
                "account",
                {
                    "wallet_balance": acc.wallet_balance,
                    "available_balance": acc.available_balance,
                    "unrealized_pnl": acc.unrealized_pnl,
                    "margin_balance": acc.margin_balance,
                    "total_margin": acc.total_margin,
                    "position_initial_margin": acc.position_initial_margin,
                    "maintenance_margin": acc.maintenance_margin,
                    "copy_reserved_balance": acc.copy_reserved_balance,
                    **(
                        {"copy_deployed_margin": acc.copy_deployed_margin}
                        if acc.copy_deployed_margin is not None
                        else {}
                    ),
                    **(
                        {"copy_profit_share_reserved": acc.copy_profit_share_reserved}
                        if acc.copy_profit_share_reserved is not None
                        else {}
                    ),
                },
            )
        served = {p.symbol for p in pos.positions}
        for key in self.positions.keys():
            if key not in served:
                self.positions.remove(key) # closed per the served snapshot
        for p in pos.positions:
            self.positions.upsert(
                p.symbol,
                {
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "leverage": str(p.leverage),
                    "margin_mode": p.margin_mode,
                    "upnl": p.upnl,
                    "maintenance_margin": p.maintenance_margin,
                    "liquidation_price": p.liquidation_price,
                    "bankruptcy_price": p.bankruptcy_price,
                },
            )
        if self._stream is not None and self.badge.text().startswith("[LIVE"):
            self.set_badge("LIVE")


class ChartTicket(QGroupBox):
    """Manual order ticket beside the chart (operator request 2026-10-22):
    the order types Strike serves (strike-trading ``POST /v2/order``) — stop,
    stop_limit, take_profit, take_profit_limit and trailing_stop_market at the
    chart — with optional TP/SL exits (kind bracket: ``POST /v2/order/strategy``
    per strike-trading), the SERVED leverage range and an optional margin_mode
    leg (``POST /v2/marginMode``). It only BUILDS a strict ``StrikePlanProposal``
    and hands it to the approval pipeline: it never signs, sends or places."""

    def __init__(self, panel: MarketPanel) -> None:
        super().__init__("Order (chart)")
        self._panel = panel
        self.side = QComboBox()
        self.side.addItems(("buy", "sell"))
        self.type = QComboBox()
        self.type.addItems(cast("tuple[str, ...]", get_args(OrderType)))
        self.type.setCurrentText("limit")
        self.type.currentTextChanged.connect(self._typed)
        self.size_edit = QLineEdit()
        self.size_edit.setPlaceholderText("size (base units)")
        self.price = QLineEdit()
        self.price.setPlaceholderText("price (click the chart)")
        self.stop = QLineEdit()
        self.stop.setPlaceholderText("stop_price (trigger)")
        self.callback = QLineEdit()
        self.callback.setPlaceholderText("trailing callback_rate 0.1..5")
        self.activation = QLineEdit()
        self.activation.setPlaceholderText("activation_price (trailing)")
        self.tif = QComboBox()
        self.tif.addItems(("", "GTC", "IOC", "FOK"))
        self.leverage = QComboBox() # ONLY the served range (refresh())
        self.margin = QComboBox()
        self.margin.addItems(("", "cross", "isolated"))
        self.margin.setToolTip(
            "margin_mode leg first (POST /v2/marginMode); cannot change with an open position"
        )
        self.reduce_only = QCheckBox("reduce_only")
        self.tp_type = QComboBox()
        self.tp_type.addItems(("", "take_profit", "take_profit_limit"))
        self.tp_stop = QLineEdit()
        self.tp_stop.setPlaceholderText("TP stop_price")
        self.sl_type = QComboBox()
        self.sl_type.addItems(("", "stop", "stop_limit"))
        self.sl_stop = QLineEdit()
        self.sl_stop.setPlaceholderText("SL stop_price")
        propose = _button("Propose -> plan check", "primary")
        propose.clicked.connect(self.build)
        form = LAYOUT.form()
        for label, w in (
            ("side", self.side),
            ("type", self.type),
            ("size", self.size_edit),
            ("price", self.price),
            ("stop_price", self.stop),
            ("callback_rate", self.callback),
            ("activation_price", self.activation),
            ("time_in_force", self.tif),
            ("leverage", self.leverage),
            ("margin_mode", self.margin),
        ):
            form.addRow(label, w)
        form.addRow("flags", self.reduce_only)
        for label, kind, stop in (
            ("TP", self.tp_type, self.tp_stop),
            ("SL", self.sl_type, self.sl_stop),
        ):
            r = LAYOUT.hbox()
            r.addWidget(kind, 1)
            r.addWidget(stop, 1)
            form.addRow(label, r)
        form.addRow(propose)
        self.setLayout(form)
        self.refresh()
        self._typed(self.type.currentText())

    def refresh(self) -> None:
        """Leverage selector from the SERVED market only (computed live from
        /v2/markets by the panel): 1..margin_tiers[0].max_leverage
        (strike-calculations: tier 0 has the highest allowed leverage)."""
        market = self._panel._markets.get(self._panel.symbol.currentText())
        pick = self.leverage.currentText()
        self.leverage.clear()
        max_lev = (
            market.margin_tiers[0].max_leverage
            if market is not None and market.margin_tiers
            else None
        )
        if max_lev is None:
            self.leverage.addItem("")
            self.leverage.setEnabled(False)
            return
        self.leverage.addItems([str(n) for n in range(1, max_lev + 1)])
        self.leverage.setEnabled(True)
        default = market.default_leverage if market is not None else None
        if market is None or default is None:
            self.leverage.setCurrentText(pick if pick.isdigit() and int(pick) <= max_lev else "1")
        else:
            self.leverage.setCurrentText(str(default))

    def _typed(self, otype: str) -> None:
        """Enable exactly the fields the served order type accepts."""
        orderish = otype not in ("market", "trailing_stop_market")
        self.price.setEnabled(orderish)
        self.stop.setEnabled(otype in ("stop", "stop_limit", "take_profit", "take_profit_limit"))
        trailing = otype == "trailing_stop_market"
        self.callback.setEnabled(trailing)
        self.activation.setEnabled(trailing)

    @staticmethod
    def _opt(w: QLineEdit) -> str | None:
        return w.text().strip() or None

    def build(self) -> None:
        symbol = self._panel.symbol.currentText()
        if not symbol:
            self._panel._failed("REJECTED: no symbol. Pick a served symbol in the chart header.")
            return
        otype = self.type.currentText()
        leg: dict[str, object] = {
            "kind": "order",
            "symbol": symbol,
            "side": self.side.currentText(),
            "type": otype,
            "size": self._opt(self.size_edit),
            "price": self._opt(self.price),
            "stop_price": self._opt(self.stop),
            "callback_rate": self._opt(self.callback),
            "activation_price": self._opt(self.activation),
            "time_in_force": self.tif.currentText() or None,
            "post_only": False,
            "reduce_only": self.reduce_only.isChecked(),
            "leverage": None,
            "client_order_id": None,
            "tp": None,
            "sl": None,
        }
        lev = self.leverage.currentText()
        if lev.isdigit():
            leg["leverage"] = int(lev)
        for key, kind, stop in (
            ("tp", self.tp_type, self.tp_stop),
            ("sl", self.sl_type, self.sl_stop),
        ):
            if kind.currentText():
                leg[key] = {
                    "type": kind.currentText(),
                    "stop_price": self._opt(stop),
                    "price": None,
                }
        if leg["tp"] or leg["sl"]:
            leg["kind"] = "bracket" # POST /v2/order/strategy (strike-trading)
        legs: list[dict[str, object]] = [leg]
        if self.margin.currentText():
            # margin_mode leg FIRST (POST /v2/marginMode), order leg second;
            # an open position makes the served marginMode endpoint refuse.
            legs.insert(
                0,
                {
                    "kind": "margin_mode",
                    "symbol": symbol,
                    "side": None,
                    "type": None,
                    "size": None,
                    "price": None,
                    "stop_price": None,
                    "callback_rate": None,
                    "activation_price": None,
                    "time_in_force": None,
                    "post_only": False,
                    "reduce_only": False,
                    "leverage": None,
                    "client_order_id": None,
                    "tp": None,
                    "sl": None,
                    "margin_mode": self.margin.currentText(),
                },
            )
        proposal = {
            "lane": "strike",
            "intent": "chart order (human-built)",
            "legs": legs,
            "rationale": "human-built via the chart order ticket",
        }
        try:
            plan = StrikePlanProposal.model_validate_json(json.dumps(proposal))
        except ValueError as exc:
            self._panel._failed(f"REJECTED: {exc}")
            return
        self._panel.set_badge(f"plan proposed ({len(plan.legs)} leg(s)) -> approval")
        self._panel.send_plan(plan)


class OrderTicket(QGroupBox):
    """§7.3 order ticket: fields mirror CreateOrderRequest / StrategyLeg; it
    only BUILDS a ``StrikePlanProposal`` (strict model) for Run to approval."""

    def __init__(self, panel: ApprovalPanel) -> None:
        super().__init__("Order ticket (builds the plan; Strike-native types only)")
        self._panel = panel
        self.symbol = QComboBox()
        self.symbol.setPlaceholderText("Symbol")
        load = _button("Load symbols", "default")
        load.clicked.connect(self.load_symbols)
        self.kind = QComboBox()
        self.kind.addItems(
            (
                "order",
                "bracket",
                "twap",
                "replace",
                "cancel_all",
                "twap_cancel",
                "iso_margin",
                "leverage",
                "margin_mode",
                "copy_subscribe",
                "copy_patch",
                "copy_stop",
                "copy_save",
                "copy_unsave",
                "rfq",
                "rfq_close",
                "rfq_response",
                "withdraw_quote",
                "withdraw",
                "vault_deposit",
                "vault_withdraw",
                "vault_withdraw_cancel",
                "vault_create",
                "vault_pause",
                "vault_resume",
            )
        )
        self.side = QComboBox()
        self.side.addItems(("buy", "sell"))
        self.type = QComboBox()
        self.type.addItems(cast("tuple[str, ...]", get_args(OrderType)))
        self.size_edit, self.price, self.stop, self.callback, self.activation, self.leverage = (
            QLineEdit() for _ in range(6)
        )
        self.tif = QComboBox()
        self.tif.addItems(("", "GTC", "IOC", "FOK"))
        self.post_only, self.reduce_only = QCheckBox("post_only"), QCheckBox("reduce_only")
        self.tp_type, self.sl_type = QComboBox(), QComboBox()
        self.tp_type.addItems(("", "take_profit", "take_profit_limit"))
        self.sl_type.addItems(("", "stop", "stop_limit"))
        self.tp_stop, self.tp_price, self.sl_stop, self.sl_price = (QLineEdit() for _ in range(4))
        self.duration, self.order_id, self.strategy_id, self.amount = (
            QLineEdit() for _ in range(4)
        )
        self.duration.setPlaceholderText("300..86400 seconds")
        self.callback.setPlaceholderText("trailing_stop_market: 0.1..5")
        self.randomize = QCheckBox("randomize")
        self.modify = QComboBox()
        self.modify.addItems(("", "add", "remove"))
        self.margin = QComboBox()
        self.margin.addItems(("", "cross", "isolated"))
        self.platform = QComboBox()
        self.platform.addItems(("", "strike", "hyperliquid"))
        self.copy_mode = QComboBox()
        self.copy_mode.addItems(("", "fixed_ratio", "fixed_amount"))
        self.blockchain = QComboBox()
        self.blockchain.addItems(("", "ethereum", "solana", "cardano"))
        self.response_status = QComboBox()
        self.response_status.addItems(("", "acked", "quoted", "uninterested"))
        (
            self.lead_account_id,
            self.copy_amount,
            self.margin_per_entry,
            self.subscription_id,
            self.client_rfq_id,
            self.requested_size,
            self.rfq_id,
            self.bid_size,
            self.ask_size,
            self.usd_amount,
            self.withdraw_id,
            self.wallet_signature,
            self.shares,
            self.vault_id,
            self.vault_name,
            self.initial_deposit,
            self.commission_bps,
            self.lockup_days,
            self.min_deposit,
            self.withdrawal_id,
        ) = (QLineEdit() for _ in range(20))
        self.withdraw_all = QCheckBox("withdraw_all")
        self.wallet_signature.setPlaceholderText("human signature of the quote message")
        build = _button("Build plan from ticket", "primary")
        build.clicked.connect(self.build)
        form = LAYOUT.form()
        # ~40 rows: the canonical never-truncate scroll (LAYOUT), so no field
        # is cut off on any screen.
        page = QWidget()
        page.setLayout(form)
        outer = LAYOUT.vbox()
        outer.addWidget(LAYOUT.scroll(page))
        self.setLayout(outer)
        row = LAYOUT.hbox()
        row.addWidget(self.symbol, 1)
        row.addWidget(load)
        form.addRow("symbol", row)
        for label, w in (
            ("kind", self.kind),
            ("side", self.side),
            ("type", self.type),
            ("size", self.size_edit),
            ("price", self.price),
            ("stop_price", self.stop),
            ("callback_rate", self.callback),
            ("activation_price", self.activation),
            ("time_in_force", self.tif),
            ("leverage", self.leverage),
            ("duration_sec", self.duration),
            ("order_id", self.order_id),
            ("strategy_id", self.strategy_id),
            ("amount", self.amount),
            ("modify_type", self.modify),
            ("margin_mode", self.margin),
            ("platform", self.platform),
            ("copy_mode", self.copy_mode),
            ("lead_account_id", self.lead_account_id),
            ("copy_amount", self.copy_amount),
            ("margin_per_entry", self.margin_per_entry),
            ("subscription_id", self.subscription_id),
            ("client_rfq_id", self.client_rfq_id),
            ("requested_size", self.requested_size),
            ("rfq_id", self.rfq_id),
            ("response_status", self.response_status),
            ("bid_size", self.bid_size),
            ("ask_size", self.ask_size),
            ("blockchain", self.blockchain),
            ("usd_amount", self.usd_amount),
            ("withdraw_id", self.withdraw_id),
            ("wallet_signature", self.wallet_signature),
            ("shares", self.shares),
            ("vault_id", self.vault_id),
            ("vault_name", self.vault_name),
            ("initial_deposit", self.initial_deposit),
            ("leader_commission_bps", self.commission_bps),
            ("lockup_days", self.lockup_days),
            ("min_deposit", self.min_deposit),
            ("withdrawal_id", self.withdrawal_id),
        ):
            form.addRow(label, w)
        form.addRow("vault flags", self.withdraw_all)
        flags = LAYOUT.hbox()
        flags.addWidget(self.post_only)
        flags.addWidget(self.reduce_only)
        flags.addWidget(self.randomize)
        form.addRow("flags", flags)
        for label, a, b, c in (
            ("TP", self.tp_type, self.tp_stop, self.tp_price),
            ("SL", self.sl_type, self.sl_stop, self.sl_price),
        ):
            r = LAYOUT.hbox()
            b.setPlaceholderText("stop_price")
            c.setPlaceholderText("price (*_limit only)")
            for w in (a, b, c):
                r.addWidget(w)
            form.addRow(label, r)
        form.addRow(build)
        self._worker: Worker | None = None
        self._symbols: list[str] = []

    def _fetch_symbols(self) -> str:
        cfg = load_config()
        http = RecordedHttp(cfg.timeout_s)
        try:
            self._symbols = sorted(
                StrikeMarketClient(http, cfg.strike_api_url).markets().value.markets
            )
        finally:
            http.close()
        return f"{len(self._symbols)} symbols"

    def load_symbols(self) -> None:
        self._worker = Worker(self._fetch_symbols)
        self._worker.done.connect(self._symbols_loaded)
        self._worker.failed.connect(self._symbols_failed)
        self._worker.start()

    def _symbols_failed(self, text: str) -> None:
        self._panel._loud(f"FAILED: {text}")

    def _symbols_loaded(self, text: str) -> None:
        self.symbol.clear()
        self.symbol.addItems(self._symbols)
        self.symbol.setCurrentIndex(0)
        self._panel._loud(text)

    @staticmethod
    def _opt(w: QLineEdit) -> str | None:
        return w.text().strip() or None

    def build(self) -> None:
        if not self.symbol.currentText():
            self._panel._loud("REJECTED: no symbol. Press Load symbols.")
            return
        exits: dict[str, object] = {}
        for key, t, stop, price in (
            ("tp", self.tp_type, self.tp_stop, self.tp_price),
            ("sl", self.sl_type, self.sl_stop, self.sl_price),
        ):
            exits[key] = (
                {"type": t.currentText(), "stop_price": self._opt(stop), "price": self._opt(price)}
                if t.currentText()
                else None
            )
        lev = self._opt(self.leverage)
        kind = self.kind.currentText()
        bps = self._opt(self.commission_bps)
        lockup = self._opt(self.lockup_days)
        if kind == "order" and (exits["tp"] or exits["sl"]):
            kind = "bracket"
        side = self.side.currentText()
        if kind == "twap":
            side = "BUY" if side == "buy" else "SELL"
        orderish = kind in ("order", "bracket", "replace")
        duration = self._opt(self.duration)
        _NO_MARKET = {
            "twap_cancel",
            "copy_subscribe",
            "copy_patch",
            "copy_stop",
            "copy_save",
            "copy_unsave",
            "rfq_close",
            "rfq_response",
            "withdraw_quote",
            "withdraw",
            "vault_deposit",
            "vault_withdraw",
            "vault_withdraw_cancel",
            "vault_create",
            "vault_pause",
            "vault_resume",
        }
        leg: dict[str, object] = {
            "kind": kind,
            "symbol": "" if kind in _NO_MARKET else self.symbol.currentText(),
            "side": side if kind in ("order", "bracket", "replace", "twap", "rfq") else None,
            "type": self.type.currentText() if orderish else None,
            "size": self._opt(self.size_edit)
            if kind in ("order", "bracket", "replace", "twap")
            else None,
            "price": self._opt(self.price)
            if kind in ("order", "bracket", "replace", "twap")
            else None,
            "stop_price": self._opt(self.stop) if orderish else None,
            "callback_rate": self._opt(self.callback) if orderish else None,
            "activation_price": self._opt(self.activation) if orderish else None,
            "time_in_force": (self.tif.currentText() or None) if orderish else None,
            "post_only": self.post_only.isChecked() if orderish else False,
            "reduce_only": self.reduce_only.isChecked()
            if kind in ("order", "bracket", "replace", "twap")
            else False,
            "tp": exits["tp"] if kind == "bracket" else None,
            "sl": exits["sl"] if kind == "bracket" else None,
            "leverage": int(lev)
            if lev is not None
            and lev.isdigit()
            and kind in ("order", "bracket", "replace", "leverage")
            else None,
            "client_order_id": None,
            "duration_sec": int(duration)
            if kind == "twap" and duration is not None and duration.isdigit()
            else None,
            "randomize": self.randomize.isChecked() if kind == "twap" else False,
            "margin_mode": (self.margin.currentText() or None) if kind == "margin_mode" else None,
            "order_id": self._opt(self.order_id) if kind == "replace" else None,
            "strategy_id": self._opt(self.strategy_id) if kind == "twap_cancel" else None,
            "amount": self._opt(self.amount) if kind == "iso_margin" else None,
            "modify_type": (self.modify.currentText() or None) if kind == "iso_margin" else None,
            "lead_account_id": self._opt(self.lead_account_id)
            if kind in ("copy_subscribe", "copy_save", "copy_unsave")
            else None,
            "platform": (self.platform.currentText() or None) if kind == "copy_subscribe" else None,
            "copy_mode": (self.copy_mode.currentText() or None)
            if kind == "copy_subscribe"
            else None,
            "copy_amount": self._opt(self.copy_amount) if kind == "copy_subscribe" else None,
            "margin_per_entry_order": self._opt(self.margin_per_entry)
            if kind == "copy_subscribe"
            else None,
            "subscription_id": self._opt(self.subscription_id)
            if kind in ("copy_patch", "copy_stop")
            else None,
            "client_rfq_id": self._opt(self.client_rfq_id) if kind == "rfq" else None,
            "requested_size": self._opt(self.requested_size) if kind == "rfq" else None,
            "rfq_id": self._opt(self.rfq_id) if kind in ("rfq_close", "rfq_response") else None,
            "response_status": (self.response_status.currentText() or None)
            if kind == "rfq_response"
            else None,
            "bid_size": self._opt(self.bid_size) if kind == "rfq_response" else None,
            "ask_size": self._opt(self.ask_size) if kind == "rfq_response" else None,
            "blockchain": (self.blockchain.currentText() or None)
            if kind == "withdraw_quote"
            else None,
            "usd_amount": self._opt(self.usd_amount)
            if kind in ("withdraw_quote", "vault_deposit")
            else None,
            "withdraw_id": self._opt(self.withdraw_id) if kind == "withdraw" else None,
            "wallet_signature": self._opt(self.wallet_signature) if kind == "withdraw" else None,
            "shares": self._opt(self.shares) if kind == "vault_withdraw" else None,
            "withdraw_all": self.withdraw_all.isChecked() if kind == "vault_withdraw" else False,
            "vault_id": self._opt(self.vault_id)
            if kind
            in (
                "rfq",
                "rfq_close",
                "vault_deposit",
                "vault_withdraw",
                "vault_withdraw_cancel",
                "vault_pause",
                "vault_resume",
            )
            else None,
            "vault_name": self._opt(self.vault_name) if kind == "vault_create" else None,
            "initial_deposit": self._opt(self.initial_deposit) if kind == "vault_create" else None,
            "leader_commission_bps": int(bps)
            if kind == "vault_create" and bps is not None and bps.isdigit()
            else None,
            "lockup_days": int(lockup)
            if kind == "vault_create" and lockup is not None and lockup.isdigit()
            else None,
            "min_deposit": self._opt(self.min_deposit) if kind == "vault_create" else None,
            "withdrawal_id": self._opt(self.withdrawal_id)
            if kind == "vault_withdraw_cancel"
            else None,
        }
        proposal = {
            "lane": "strike",
            "intent": "order ticket (human-built)",
            "legs": [leg],
            "rationale": "human-built via the order ticket",
        }
        try:
            plan = StrikePlanProposal.model_validate_json(json.dumps(proposal))
        except ValueError as exc:
            self._panel._loud(f"REJECTED: {exc}")
            return
        self._panel.use_ticket_plan(plan)
