"""Native Strike strategies: TWAP and grid-bot PROPOSALS plus read-only state.

This tab only builds strict ``StrikePlanProposal`` values (kinds ``twap``,
``grid_bot``, ``twap_cancel``, ``grid_stop``) and hands them to a typed
callback — the integrator wires that to the existing human approval path
(``ApprovalPanel.use_ticket_plan``). It never sends: no order, no create, no
cancel, no signature. The tables render the served native strategy reads
(``GET /v2/algo/twap?status=all`` and ``GET /v2/grid/bot``, the same calls as
``struck.strike.surface.authed_surface``) with Strike's strings as served;
cancellation proposals are built only from a SELECTED served row's id, never
from free-typed text. Every network call runs off the UI thread through the
stock ``Worker``; no state without a loud pending/failed/empty label.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..callhistory import CallFailed, RecordedHttp
from ..config import load_config
from ..strike.account import StrikeAuthedClient
from ..strike.auth import StrikeCredentialsError
from ..strike.market import StrikeMarketClient
from ..strike.models import StrikeLeg, StrikePlanProposal
from ..strike.surface import GridBots, ServedRow, TwapList, TwapStrategy
from ..strike.venue_auth import wallet_from_secrets
from . import theme
from .layout import LAYOUT
from .panels import Worker, network_badge, ui_log

TWAP_COLUMNS = ("id", "symbol", "side", "status", "filled", "total")
GRID_COLUMNS = ("id", "symbol", "status", "mode", "grid_count", "collateral")


def _button(text: str, variant: str = "default") -> QPushButton:
    button = QPushButton(text)
    button.setStyleSheet(theme.button_qss(variant))
    return button


def _cardlabel(text: str) -> QLabel:
    """Brand section label: numbered, uppercase, tracked (BRAND.md typography)."""
    label = QLabel(text.upper())
    label.setObjectName("cardlabel")
    return label


def _text(value: object) -> str:
    """A served scalar as display text (absent fields stay empty)."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return ""


def _extra(row: ServedRow, key: str) -> str:
    """A served bot extra (``extra="allow"`` keeps fields outside the model).
    pydantic types ``model_extra`` as ``dict[str, Any]``; the exact-type cast
    is the sanctioned boundary squeeze, then ``_text`` narrows the object."""
    extras = cast("dict[str, object] | None", row.model_extra)
    return _text((extras or {}).get(key))


def _dec(edit: QLineEdit) -> str | None:
    """A decimal text field, unchanged; the strict DecStr pattern rejects noise."""
    text = edit.text().strip()
    return text or None


def _int(edit: QLineEdit) -> int | None:
    """A whole-number text field; anything else fails loudly in the model."""
    text = edit.text().strip()
    return int(text) if text.isdigit() else None


def _twap_leg(
    symbol: str,
    side: Literal["BUY", "SELL"],
    size: str | None,
    price: str | None,
    duration_sec: int | None,
    reduce_only: bool,
    randomize: bool,
) -> StrikeLeg:
    """``POST /v2/algo/twap`` as the flat StrikeLeg (strike-trading: side is
    BUY/SELL, size is total_size, price is the optional limit_price). A missing
    size or duration is rejected loudly by the strict model, not repaired."""
    return StrikeLeg(
        kind="twap",
        symbol=symbol,
        side=side,
        type=None,
        size=size,
        price=price,
        stop_price=None,
        callback_rate=None,
        activation_price=None,
        time_in_force=None,
        post_only=False,
        reduce_only=reduce_only,
        tp=None,
        sl=None,
        leverage=None,
        client_order_id=None,
        duration_sec=duration_sec,
        randomize=randomize,
    )


def _grid_leg(
    symbol: str,
    mode: Literal["LONG", "SHORT", "NEUTRAL"],
    initialization_mode: Literal["LIMIT_ONLY", "MARKET_SEEDED"] | None,
    lower_price: str | None,
    upper_price: str | None,
    grid_count: int | None,
    collateral: str | None,
    sl_price: str | None,
    long_sl_price: str | None,
    short_sl_price: str | None,
) -> StrikeLeg:
    """``POST /v2/grid/bot`` as the flat StrikeLeg (strike-grid-bot fields;
    the SL fields are validated per mode by the strict model, loudly)."""
    return StrikeLeg(
        kind="grid_bot",
        symbol=symbol,
        side=None,
        type=None,
        size=None,
        price=None,
        stop_price=None,
        callback_rate=None,
        activation_price=None,
        time_in_force=None,
        post_only=False,
        reduce_only=False,
        tp=None,
        sl=None,
        leverage=None,
        client_order_id=None,
        grid_mode=mode,
        initialization_mode=initialization_mode,
        lower_price=lower_price,
        upper_price=upper_price,
        grid_count=grid_count,
        collateral=collateral,
        sl_price=sl_price,
        long_sl_price=long_sl_price,
        short_sl_price=short_sl_price,
    )


def _twap_cancel_leg(strategy_id: str) -> StrikeLeg:
    """``DELETE /v2/algo/twap/{id}``; the strategy_id comes from a served row."""
    return StrikeLeg(
        kind="twap_cancel",
        symbol="",
        side=None,
        type=None,
        size=None,
        price=None,
        stop_price=None,
        callback_rate=None,
        activation_price=None,
        time_in_force=None,
        post_only=False,
        reduce_only=False,
        tp=None,
        sl=None,
        leverage=None,
        client_order_id=None,
        strategy_id=strategy_id,
    )


def _grid_stop_leg(bot_id: str, cancel_orders: bool) -> StrikeLeg:
    """``DELETE /v2/grid/bot/{id}``; the bot_id comes from a served row."""
    return StrikeLeg(
        kind="grid_stop",
        symbol="",
        side=None,
        type=None,
        size=None,
        price=None,
        stop_price=None,
        callback_rate=None,
        activation_price=None,
        time_in_force=None,
        post_only=False,
        reduce_only=False,
        tp=None,
        sl=None,
        leverage=None,
        client_order_id=None,
        bot_id=bot_id,
        cancel_orders=cancel_orders,
    )


class _Payload(BaseModel):
    """What crosses the thread boundary: text only, like the liquidity tab."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    label: str
    symbols: tuple[str, ...]
    marks: tuple[tuple[str, str], ...]  # (symbol, served mark price) per market
    twap: TwapList | None
    grids: GridBots | None
    auth_note: str


class StrategiesPanel(QWidget):
    """Native TWAP / grid-bot creation and cancellation PROPOSALS plus the
    served read-only strategy state for the selected network. The proposal
    callback opens the existing human approval; this tab sends nothing."""

    def __init__(self, propose: Callable[[StrikePlanProposal], None]) -> None:
        super().__init__()
        self._propose = propose
        self._twap_rows: list[TwapStrategy] = []
        self._grid_rows: list[ServedRow] = []
        self._worker: Worker | None = None

        self.state = QLabel("Not loaded")
        self.state.setWordWrap(True)
        self.refresh = _button("Refresh strategies", "default")
        self.refresh.clicked.connect(self.load)
        self.twap_table = QTableWidget(0, len(TWAP_COLUMNS))
        self.grids_table = QTableWidget(0, len(GRID_COLUMNS))
        for table, columns in ((self.twap_table, TWAP_COLUMNS), (self.grids_table, GRID_COLUMNS)):
            table.setHorizontalHeaderLabels(list(columns))
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.twap_table.itemSelectionChanged.connect(self._reselect)
        self.grids_table.itemSelectionChanged.connect(self._reselect)

        self.cancel_twap = _button("Propose TWAP cancel (selected row)", "warning")
        self.cancel_twap.clicked.connect(self.propose_twap_cancel)
        self.cancel_twap.setToolTip("DELETE /v2/algo/twap/{id} proposal from the selected served row")
        self.cancel_bot = _button("Propose bot stop (selected row)", "warning")
        self.cancel_bot.clicked.connect(self.propose_grid_stop)
        self.cancel_bot.setToolTip("DELETE /v2/grid/bot/{id} proposal from the selected served row")
        self.cancel_orders = QCheckBox("stop also cancels the bot's live orders")
        self.cancel_orders.setChecked(True)
        self.cancel_twap.setEnabled(False)
        self.cancel_bot.setEnabled(False)

        self.symbol = QComboBox()
        self.symbol.setPlaceholderText("Symbol (from the served /v2/markets)")
        self.symbol.currentTextChanged.connect(self._show_mark)
        self.mark_note = QLabel("")
        self.mark_note.setObjectName("muted")
        self.mark_note.setWordWrap(True)
        self._marks: dict[str, str] = {}
        load_symbols = _button("Load symbols", "default")
        load_symbols.clicked.connect(self._load_symbols)
        symbol_row = LAYOUT.hbox()
        symbol_row.addWidget(self.symbol, 1)
        symbol_row.addWidget(load_symbols)
        self.side = QComboBox()
        self.side.addItems(("BUY", "SELL"))
        # QWidget already owns size() and lower(); distinct names, no shadowing.
        self.total_size = QLineEdit()
        self.total_size.setPlaceholderText("total_size, e.g. 1.5 (served as a decimal string)")
        self.limit_price = QLineEdit()
        self.limit_price.setPlaceholderText("limit_price, optional (> 0)")
        self.duration = QLineEdit()
        self.duration.setPlaceholderText("duration_sec, 300..86400")
        self.reduce_only = QCheckBox("reduce_only")
        self.randomize = QCheckBox("randomize")
        propose_twap = _button("Propose TWAP", "primary")
        propose_twap.clicked.connect(self.propose_twap)
        twap_form = LAYOUT.form()
        twap_form.addRow("side", self.side)
        twap_form.addRow("total_size", self.total_size)
        twap_form.addRow("limit_price", self.limit_price)
        twap_form.addRow("duration_sec", self.duration)
        flags = LAYOUT.hbox()
        flags.addWidget(self.reduce_only)
        flags.addWidget(self.randomize)
        twap_form.addRow("flags", flags)
        twap_form.addRow(propose_twap)
        twap_box = QGroupBox("03 — TWAP (POST /v2/algo/twap proposal)")
        twap_box.setLayout(twap_form)

        self.grid_mode = QComboBox()
        self.grid_mode.addItems(("LONG", "SHORT", "NEUTRAL"))
        self.init_mode = QComboBox()
        self.init_mode.addItems(("", "LIMIT_ONLY", "MARKET_SEEDED"))
        self.lower_price = QLineEdit()
        self.lower_price.setPlaceholderText("lower_price, e.g. 60000")
        self.upper_price = QLineEdit()
        self.upper_price.setPlaceholderText("upper_price > lower_price; mark strictly inside")
        self.grid_count = QLineEdit()
        self.grid_count.setPlaceholderText("grid_count, 2..200")
        self.collateral = QLineEdit()
        self.collateral.setPlaceholderText("collateral, e.g. 500 (USD; balance must cover it)")
        self.sl_price = QLineEdit()
        self.sl_price.setPlaceholderText("sl_price (LONG below / SHORT above the range)")
        self.long_sl_price = QLineEdit()
        self.long_sl_price.setPlaceholderText("long_sl_price (NEUTRAL only)")
        self.short_sl_price = QLineEdit()
        self.short_sl_price.setPlaceholderText("short_sl_price (NEUTRAL only)")
        propose_grid = _button("Propose grid bot", "primary")
        propose_grid.clicked.connect(self.propose_grid)
        grid_form = LAYOUT.form()
        grid_form.addRow("mode", self.grid_mode)
        grid_form.addRow("initialization_mode", self.init_mode)
        grid_form.addRow("lower_price", self.lower_price)
        grid_form.addRow("upper_price", self.upper_price)
        grid_form.addRow("grid_count", self.grid_count)
        grid_form.addRow("collateral", self.collateral)
        grid_form.addRow("sl_price", self.sl_price)
        grid_form.addRow("long_sl_price", self.long_sl_price)
        grid_form.addRow("short_sl_price", self.short_sl_price)
        grid_form.addRow(propose_grid)
        grid_box = QGroupBox("04 — Grid bot (POST /v2/grid/bot proposal)")
        grid_box.setLayout(grid_form)

        self.log = QPlainTextEdit()
        self.log.setObjectName("mono")
        self.log.setReadOnly(True)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        layout.addWidget(network_badge(load_config().profile))
        bar = LAYOUT.hbox()
        bar.addWidget(self.refresh)
        bar.addStretch(1)
        layout.addLayout(bar)
        layout.addWidget(self.state)
        tables = LAYOUT.hbox()
        twap_view = LAYOUT.vbox()
        twap_label = _cardlabel("01 — Native TWAP strategies (served, read-only)")
        twap_view.addWidget(twap_label)
        twap_view.addWidget(self.twap_table)
        grid_view = LAYOUT.vbox()
        grid_view.addWidget(_cardlabel("02 — Native grid bots (served, read-only)"))
        grid_view.addWidget(self.grids_table)
        for box in (twap_view, grid_view):
            tables.addLayout(box, 1)
        layout.addLayout(tables, 2)
        stops = LAYOUT.hbox()
        stops.addWidget(self.cancel_twap)
        stops.addWidget(self.cancel_bot)
        stops.addWidget(self.cancel_orders)
        layout.addLayout(stops)
        layout.addLayout(symbol_row)
        layout.addWidget(self.mark_note)
        forms = LAYOUT.hbox()
        forms.addWidget(twap_box)
        forms.addWidget(grid_box)
        layout.addLayout(forms, 2)
        layout.addWidget(self.log, 1)

    # ---- served-state refresh (off the UI thread) ----------------------
    def load(self) -> None:
        """Reads the served markets and the native strategy state. The signed
        strategy reads need the API wallet; without it the tables show the
        loud locked note instead of stale rows."""
        if self._worker is not None and self._worker.isRunning():
            return
        self._twap_rows = []
        self._grid_rows = []
        self.twap_table.setRowCount(0)
        self.grids_table.setRowCount(0)
        self._reselect()
        self.refresh.setEnabled(False)
        self.state.setText(f"Reading {load_config().profile.label}")

        def fetch() -> str:
            cfg = load_config()
            http = RecordedHttp(cfg.timeout_s)
            try:
                market = StrikeMarketClient(http, cfg.strike_api_url)
                served_markets = market.markets().value.markets
                symbols = tuple(sorted(served_markets))
                marks = tuple((s, served_markets[s].mark_price or "") for s in symbols)
                twap: TwapList | None = None
                grids: GridBots | None = None
                note = ""
                try:
                    wallet = wallet_from_secrets()
                except StrikeCredentialsError as exc:
                    note = str(exc)
                else:
                    client = StrikeAuthedClient(http, cfg.strike_api_url, wallet)
                    errors: list[str] = []
                    try:
                        twap = TwapList.model_validate_json(
                            client.request("GET", "/v2/algo/twap?status=all").content
                        )
                    except (CallFailed, httpx.HTTPError, ValueError) as exc:
                        errors.append(f"TWAP unavailable: {exc}")
                    try:
                        grids = GridBots.model_validate_json(client.request("GET", "/v2/grid/bot").content)
                    except (CallFailed, httpx.HTTPError, ValueError) as exc:
                        errors.append(f"Grid bots unavailable: {exc}")
                    note = "; ".join(errors)
                return _Payload(
                    label=cfg.profile.label,
                    symbols=symbols,
                    marks=marks,
                    twap=twap,
                    grids=grids,
                    auth_note=note,
                ).model_dump_json()
            finally:
                http.close()

        self._worker = Worker(fetch)
        self._worker.done.connect(self._show)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _load_symbols(self) -> None:
        """Public ``/v2/markets`` only, for the symbol combo and served marks."""
        if self._worker is not None and self._worker.isRunning():
            return

        def fetch() -> str:
            cfg = load_config()
            http = RecordedHttp(cfg.timeout_s)
            try:
                served = StrikeMarketClient(http, cfg.strike_api_url).markets().value.markets
                return ",".join(f"{s}={served[s].mark_price or ''}" for s in sorted(served))
            finally:
                http.close()

        self._worker = Worker(fetch)
        self._worker.done.connect(self._symbols_loaded)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _symbols_loaded(self, joined: str) -> None:
        pairs = [entry.split("=", 1) for entry in joined.split(",") if "=" in entry]
        self._marks = {symbol: mark for symbol, mark in pairs}
        self._fill_symbols(tuple(symbol for symbol, _ in pairs))
        self._show_mark(self.symbol.currentText())
        self._report(f"{len(pairs)} served symbols loaded")

    def _fill_symbols(self, symbols: tuple[str, ...]) -> None:
        current = self.symbol.currentText()
        self.symbol.blockSignals(True)
        self.symbol.clear()
        self.symbol.addItems(list(symbols))
        self.symbol.setCurrentIndex(symbols.index(current) if current in symbols else (0 if symbols else -1))
        self.symbol.blockSignals(False)

    def _failed(self, reason: str) -> None:
        self.refresh.setEnabled(True)
        self.state.setText(ui_log("strategies", f"FAILED: {reason}"))

    def _show_mark(self, symbol: str) -> None:
        """The served mark for the picked symbol: the grid range must strictly
        contain it (strike-grid-bot), so it is shown, never guessed."""
        mark = self._marks.get(symbol.strip())
        self.mark_note.setText(
            f"served mark {symbol}: {mark} — the grid range must strictly contain it"
            if mark is not None
            else f"no served mark yet for {symbol or '(no symbol)'}: press Refresh strategies"
        )

    def _show(self, payload: str) -> None:
        view = _Payload.model_validate_json(payload)
        self._fill_symbols(view.symbols)
        self._marks = dict(view.marks)
        self._show_mark(self.symbol.currentText())
        self._twap_rows = list(view.twap.strategies) if view.twap is not None else []
        self._grid_rows = list(view.grids.bots) if view.grids is not None else []
        self._fill_table(
            self.twap_table,
            [
                (row.strategy_id, row.market, row.side, row.status, row.filled_size, row.total_size)
                for row in self._twap_rows
            ],
        )
        self._fill_table(
            self.grids_table,
            [
                (
                    _text(row.id),
                    _text(row.symbol),
                    _text(row.status),
                    _text(row.mode),
                    _extra(row, "grid_count"),
                    _extra(row, "collateral"),
                )
                for row in self._grid_rows
            ],
        )
        if view.twap is None or view.grids is None:
            self._report(f"signed strategy reads unavailable: {view.auth_note}")
        elif not self._twap_rows and not self._grid_rows:
            self._report("no native TWAP or grid strategies served for this account")
        signed = (
            "strategy reads loaded"
            if view.twap is not None and view.grids is not None
            else view.auth_note
        )
        self.state.setText(
            f"{view.label} · twap {len(self._twap_rows)} · grids {len(self._grid_rows)} · {signed}"
        )
        self._reselect()
        self.refresh.setEnabled(True)

    @staticmethod
    def _fill_table(table: QTableWidget, rows: list[tuple[str, ...]]) -> None:
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(value))
        table.resizeColumnsToContents()

    def _selected_twap(self) -> TwapStrategy | None:
        row = self.twap_table.currentRow()
        return self._twap_rows[row] if 0 <= row < len(self._twap_rows) else None

    def _selected_grid(self) -> ServedRow | None:
        row = self.grids_table.currentRow()
        return self._grid_rows[row] if 0 <= row < len(self._grid_rows) else None

    def _reselect(self) -> None:
        self.cancel_twap.setEnabled(self._selected_twap() is not None)
        self.cancel_bot.setEnabled(self._selected_grid() is not None)

    # ---- proposals (the callback opens human approval; nothing sends) ---
    def _report(self, text: str) -> None:
        self.log.appendPlainText(ui_log("strategies", text))

    def _emit(self, plan: StrikePlanProposal) -> None:
        self._report(
            f"proposed {plan.intent} ({len(plan.legs)} leg) -> human approval; NOTHING SENT from this tab"
        )
        self._propose(plan)

    def _symbol(self) -> str:
        symbol = self.symbol.currentText().strip()
        if not symbol:
            self._report("REJECTED: no symbol. Press Load symbols (served /v2/markets only).")
        return symbol

    def propose_twap(self) -> None:
        symbol = self._symbol()
        if not symbol:
            return
        side = self.side.currentText()
        side_literal: Literal["BUY", "SELL"]
        if side == "BUY":
            side_literal = "BUY"
        elif side == "SELL":
            side_literal = "SELL"
        else:
            self._report("REJECTED: side is BUY or SELL")
            return
        try:
            leg = _twap_leg(
                symbol,
                side=side_literal,
                size=_dec(self.total_size),
                price=_dec(self.limit_price),
                duration_sec=_int(self.duration),
                reduce_only=self.reduce_only.isChecked(),
                randomize=self.randomize.isChecked(),
            )
        except ValueError as exc:
            self._report(f"REJECTED: {exc}")
            return
        self._emit(
            StrikePlanProposal(
                lane="strike",
                intent=f"native TWAP {side_literal} {symbol}",
                legs=(leg,),
                rationale=(
                    f"native Strike TWAP proposal on {load_config().profile.label}; "
                    "needs human approval, then the API-wallet signature"
                ),
            )
        )

    def propose_grid(self) -> None:
        symbol = self._symbol()
        if not symbol:
            return
        mode = self.grid_mode.currentText()
        mode_literal: Literal["LONG", "SHORT", "NEUTRAL"]
        if mode == "LONG":
            mode_literal = "LONG"
        elif mode == "SHORT":
            mode_literal = "SHORT"
        elif mode == "NEUTRAL":
            mode_literal = "NEUTRAL"
        else:
            self._report("REJECTED: mode is LONG, SHORT or NEUTRAL")
            return
        init_text = self.init_mode.currentText()
        initialization: Literal["LIMIT_ONLY", "MARKET_SEEDED"] | None = None
        if init_text == "LIMIT_ONLY":
            initialization = "LIMIT_ONLY"
        elif init_text == "MARKET_SEEDED":
            initialization = "MARKET_SEEDED"
        elif init_text:
            self._report("REJECTED: initialization_mode is LIMIT_ONLY or MARKET_SEEDED")
            return
        try:
            leg = _grid_leg(
                symbol,
                mode=mode_literal,
                initialization_mode=initialization,
                lower_price=_dec(self.lower_price),
                upper_price=_dec(self.upper_price),
                grid_count=_int(self.grid_count),
                collateral=_dec(self.collateral),
                sl_price=_dec(self.sl_price),
                long_sl_price=_dec(self.long_sl_price),
                short_sl_price=_dec(self.short_sl_price),
            )
        except ValueError as exc:
            self._report(f"REJECTED: {exc}")
            return
        self._emit(
            StrikePlanProposal(
                lane="strike",
                intent=f"native grid bot {mode_literal} {symbol}",
                legs=(leg,),
                rationale=(
                    f"native Strike grid-bot proposal on {load_config().profile.label}; "
                    "needs human approval, then the API-wallet signature"
                ),
            )
        )

    def propose_twap_cancel(self) -> None:
        row = self._selected_twap()
        if row is None:
            self._report("NOT DONE: select a served TWAP row first (no free-typed ids)")
            return
        try:
            leg = _twap_cancel_leg(row.strategy_id)
        except ValueError as exc:
            self._report(f"REJECTED: {exc}")
            return
        self._emit(
            StrikePlanProposal(
                lane="strike",
                intent=f"cancel native TWAP {row.strategy_id}",
                legs=(leg,),
                rationale=(
                    f"cancel TWAP {row.strategy_id} on {row.market} (served status {row.status}) "
                    f"on {load_config().profile.label}; needs human approval"
                ),
            )
        )

    def propose_grid_stop(self) -> None:
        row = self._selected_grid()
        if row is None:
            self._report("NOT DONE: select a served grid-bot row first (no free-typed ids)")
            return
        bot_id = _text(row.id)
        cancel_orders = self.cancel_orders.isChecked()
        try:
            leg = _grid_stop_leg(bot_id, cancel_orders)
        except ValueError as exc:
            self._report(f"REJECTED: {exc}")
            return
        self._emit(
            StrikePlanProposal(
                lane="strike",
                intent=f"stop native grid bot {bot_id}",
                legs=(leg,),
                rationale=(
                    f"stop grid bot {bot_id} on {_text(row.symbol)} (served status {_text(row.status)}), "
                    f"cancel_orders={str(cancel_orders).lower()}, on {load_config().profile.label}; "
                    "needs human approval"
                ),
            )
        )
