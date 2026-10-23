"""Liquidity reads: public book, marks, vaults and copy, plus signed account reads.

No quote, deposit, withdrawal, or order is sent from this tab.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..callhistory import CallFailed, RecordedHttp
from ..config import load_config
from ..strike.account import StrikeAuthedClient
from ..strike.auth import StrikeCredentialsError
from ..strike.market import StrikeMarketClient
from ..strike.surface import (
    AuthedSurface,
    OpenOrder,
    PublicSurface,
    ServedRow,
    TwapStrategy,
    authed_surface,
    public_surface,
)
from ..strike.venue_auth import wallet_from_secrets
from .layout import LAYOUT
from .panels import Worker


class _Payload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    label: str
    public: PublicSurface
    authed: AuthedSurface | None
    auth_note: str


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return ""


def _fill(table: QTableWidget, rows: list[tuple[str, ...]]) -> None:
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(value))


def _order_cells(row: OpenOrder) -> tuple[str, ...]:
    return (
        row.Symbol,
        row.Side,
        row.Type,
        row.Size,
        row.Price,
        row.Status,
    )


def _twap_cells(row: TwapStrategy) -> tuple[str, ...]:
    return (
        row.strategy_id,
        row.market,
        row.side,
        row.status,
        row.filled_size,
        row.total_size,
    )


def _grid_cells(row: ServedRow) -> tuple[str, ...]:
    return (_text(row.id), _text(row.symbol), _text(row.status), _text(row.mode))


def _copy_cells(row: ServedRow) -> tuple[str, ...]:
    return (_text(row.symbol), _text(row.size), _text(row.margin))


def _table(headers: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    return table


class LiquidityPanel(QWidget):
    """Strike's public and signed reads for the selected network."""

    def __init__(self) -> None:
        super().__init__()
        self.state = QLabel("Not loaded")
        self.state.setWordWrap(True)
        self.book = _table(("symbol", "bid", "bid qty", "ask", "ask qty"))
        self.marks = _table(("symbol", "mark", "index", "funding"))
        self.interest = _table(("symbol", "open interest"))
        self.vaults = _table(("vault", "type", "name"))
        self.leads = _table(("platform", "nickname"))
        self.symbols = _table(("symbol", "leverage", "margin mode"))
        self.orders = _table(("symbol", "side", "type", "size", "price", "status"))
        self.twap = _table(("id", "symbol", "side", "status", "filled", "total"))
        self.grids = _table(("id", "symbol", "status", "mode"))
        self.copy_pos = _table(("symbol", "size", "margin"))
        self.refresh = QPushButton("Refresh reads")
        self.refresh.clicked.connect(self.load)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        title = QLabel("LIQUIDITY · Trade with intent")
        title.setObjectName("brand")
        note = QLabel(
            "Public book, marks, open interest, vaults and copy discovery, then the "
            "signed reads (symbols, open orders, TWAP, grid, copy, vault positions). "
            "Nothing on this tab sends an order or a quote."
        )
        note.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addWidget(self.refresh)
        layout.addWidget(self.state)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        tables = LAYOUT.vbox()
        content.setLayout(tables)
        for heading, table in (
            ("Book", self.book),
            ("Mark", self.marks),
            ("Open interest", self.interest),
            ("Vaults", self.vaults),
            ("Lead traders", self.leads),
            ("Symbol settings", self.symbols),
            ("Open orders", self.orders),
            ("TWAP", self.twap),
            ("Grid bots", self.grids),
            ("Copy positions", self.copy_pos),
        ):
            label = QLabel(heading)
            label.setObjectName("muted")
            tables.addWidget(label)
            table.setMinimumHeight(180)
            tables.addWidget(table)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        self._worker: Worker | None = None

    def load(self) -> None:
        cfg = load_config()
        self.refresh.setEnabled(False)
        self.state.setText(f"Reading {cfg.profile.label}")

        def fetch() -> str:
            http = RecordedHttp(cfg.timeout_s)
            try:
                market = StrikeMarketClient(http, cfg.strike_api_url)
                public = public_surface(market)
                note = ""
                authed: AuthedSurface | None = None
                try:
                    wallet = wallet_from_secrets()
                except StrikeCredentialsError as exc:
                    note = str(exc)
                else:
                    try:
                        authed = authed_surface(StrikeAuthedClient(http, cfg.strike_api_url, wallet))
                    except CallFailed as exc:
                        note = str(exc)
                return _Payload(
                    label=cfg.profile.label, public=public, authed=authed, auth_note=note
                ).model_dump_json()
            finally:
                http.close()

        self._worker = Worker(fetch)
        self._worker.done.connect(self._show)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _failed(self, reason: str) -> None:
        self.refresh.setEnabled(True)
        self.state.setText(f"FAILED: {reason}")

    def _show(self, payload: str) -> None:
        view = _Payload.model_validate_json(payload)
        public = view.public
        _fill(
            self.book,
            [
                (row.symbol, row.bidPrice, row.bidQty, row.askPrice, row.askQty)
                for row in public.book
            ],
        )
        _fill(self.marks, [(row.s, row.p, row.i, row.r) for row in public.marks])
        _fill(self.interest, [(row.symbol, row.openInterest) for row in public.interest])
        _fill(self.vaults, [(row.vault_id, row.type, row.name) for row in public.vaults.vaults])
        _fill(
            self.leads,
            [(row.platform, _text(row.nickname)) for row in public.leads.lead_traders],
        )
        authed = view.authed
        if authed is None:
            for table in (self.symbols, self.orders, self.twap, self.grids, self.copy_pos):
                table.setRowCount(0)
        else:
            _fill(
                self.symbols,
                [
                    (symbol, str(row.leverage), row.margin_mode)
                    for symbol, row in sorted(authed.symbols.symbol_settings.items())
                ],
            )
            _fill(self.orders, [_order_cells(row) for row in authed.open_orders.orders])
            _fill(self.twap, [_twap_cells(row) for row in authed.twap.strategies])
            _fill(self.grids, [_grid_cells(row) for row in authed.grids.bots])
            _fill(self.copy_pos, [_copy_cells(row) for row in authed.copy_positions.positions])
        signed = "signed reads loaded" if authed is not None else view.auth_note
        self.state.setText(
            f"{view.label} · book {len(public.book)} · marks {len(public.marks)} · "
            f"vaults {public.vaults.count} · leads {len(public.leads.lead_traders)} · "
            f"copy discovery {'on' if public.filters.enabled else 'off'} · {signed}"
        )
        self.refresh.setEnabled(True)
