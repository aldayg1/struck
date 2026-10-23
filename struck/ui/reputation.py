"""Public reputation evidence: served performance and balances, without a score."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TypedDict

import struck_py
from pydantic import BaseModel, ConfigDict
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..callhistory import RecordedHttp
from ..config import load_config
from ..strike.account import Account, Balances
from ..strike.history import PositionHistory, PositionLife, StrikePublicReads
from .layout import LAYOUT
from .panels import Worker


class PublicIdentity(TypedDict):
    account_id: str | None
    vault_id: str | None
    blockchain_address: str | None


class Evidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    account: Account
    balances: Balances
    history: PositionHistory
    fetched_at: str
    source: str
    response_sha256: str


class ScoreRequest(BaseModel):
    """The rule input: served rows passed through verbatim; Rust reads the
    fields it needs and ignores the rest of each served record."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    rule: str
    window_start_ms: int
    window_end_ms: int | None
    history_exhausted: bool
    balances: Balances
    positions: list[PositionLife]


class ScoreComponents(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    outcome_reliability: str
    net_result: str
    loss_containment: str


class ScoreTotals(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    window_start_ms: int
    window_end_ms: int | None
    complete_closed_lives: int
    profitable: int
    losing: int
    break_even: int
    net_realized_pnl_sum: str
    worst_single_net_loss: str
    markets: list[str]


class ScoreEvidenceNote(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    history_exhausted: bool
    served_balances: list[str]
    notice: str


class ScoreReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    rule_version: str
    status: str
    reasons: list[str]
    score: int | None
    components: ScoreComponents | None
    totals: ScoreTotals
    evidence: ScoreEvidenceNote


class ReputationPanel(QWidget):
    """Read public evidence explicitly; no secrets, lending decisions or writes."""

    def __init__(self) -> None:
        super().__init__()
        self.selector = QComboBox()
        self.selector.addItems(["Account ID", "Vault ID", "Wallet address"])
        self.identity = QLineEdit()
        self.identity.setPlaceholderText("Enter the public account, vault or wallet to inspect")
        self.refresh = QPushButton("Load evidence")
        self.more = QPushButton("Next page")
        self.more.setEnabled(False)
        self.state = QLabel("Choose an identity. Reads use the selected network.")
        self.state.setWordWrap(True)
        self.holdings = QLabel("")
        self.holdings.setWordWrap(True)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.window_start = QLineEdit()
        self.window_start.setPlaceholderText("Window start (epoch ms, empty = all served history)")
        self.window_end = QLineEdit()
        self.window_end.setPlaceholderText("Window end (epoch ms, empty = open)")
        self.score = QLabel("")
        self.score.setWordWrap(True)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Market", "Side", "Status", "Net realized PnL", "ROI %",
            "Fees", "Funding PnL", "History completeness",
        ])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        note = QLabel(
            "Performance and balances are reported by Strike. Partial history is labeled. "
            "Balances are not proof of collateral locked for a loan. "
            "The score is rule struck-reputation-1 (Rust): served closed-life outcomes only, "
            "computed when ALL pages of the served history are loaded; it is not creditworthiness "
            "or lending eligibility."
        )
        note.setWordWrap(True)
        controls = LAYOUT.hbox()
        for widget in (self.selector, self.identity, self.refresh, self.more):
            controls.addWidget(widget)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        title = QLabel("REPUTATION · Real history. Real opportunity.")
        title.setObjectName("brand")
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addLayout(controls)
        windows = LAYOUT.hbox()
        windows.addWidget(self.window_start)
        windows.addWidget(self.window_end)
        layout.addLayout(windows)
        layout.addWidget(self.state)
        layout.addWidget(self.holdings)
        layout.addWidget(self.summary)
        layout.addWidget(self.score)
        layout.addWidget(self.table, 1)
        self._worker: Worker | None = None
        self._cursor: str | None = None
        self._loaded_identity: tuple[int, str] | None = None
        self._positions: dict[str, PositionLife] = {}
        self._seen_cursors: set[str] = set()
        self._pages = 0
        self._account_id: str | None = None
        self.refresh.clicked.connect(self.load)
        self.more.clicked.connect(self.next_page)

    def load(self) -> None:
        self._positions.clear()
        self._seen_cursors.clear()
        self._pages = 0
        self._account_id = None
        self._cursor = None
        self._loaded_identity = None
        self.score.clear()
        self._read()

    def next_page(self) -> None:
        current = (self.selector.currentIndex(), self.identity.text().strip())
        if current != self._loaded_identity:
            self._failed("Identity changed. Load evidence before requesting another page.")
            return
        self._read()

    def _read(self) -> None:
        identity = self.identity.text().strip()
        if not identity:
            self._failed("A public identity is required.")
            return
        cfg = load_config()
        index = self.selector.currentIndex()
        cursor = self._cursor
        self.refresh.setEnabled(False)
        self.more.setEnabled(False)
        self.selector.setEnabled(False)
        self.identity.setEnabled(False)
        self.table.setRowCount(0)
        self.holdings.clear()
        self.summary.clear()
        self.state.setText(f"Reading public evidence · {cfg.profile.label}")

        def fetch() -> str:
            http = RecordedHttp(cfg.timeout_s)
            try:
                client = StrikePublicReads(http, cfg.strike_api_url)
                selection: PublicIdentity = {
                    "account_id": identity if index == 0 else None,
                    "vault_id": identity if index == 1 else None,
                    "blockchain_address": identity if index == 2 else None,
                }
                _, account = client.account(**selection)
                _, balances = client.balances(**selection)
                served, history = client.position_history(**selection, cursor=cursor)
                return Evidence(
                    account=account, balances=balances, history=history,
                    fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    source=served.url,
                    response_sha256=served.sha256,
                ).model_dump_json()
            finally:
                http.close()

        self._loaded_identity = (index, identity)
        self._worker = Worker(fetch)
        self._worker.done.connect(self.show_evidence)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _window_ms(self) -> tuple[int, int | None]:
        """The declared evidence window. Empty means all served history."""
        start_text = self.window_start.text().strip()
        end_text = self.window_end.text().strip()
        start = int(start_text) if start_text else 0
        if start < 0:
            raise ValueError("Window start must be a non-negative epoch-ms integer.")
        end = int(end_text) if end_text else None
        if end is not None and (end < 0 or (start_text and end < start)):
            raise ValueError("Window end must be a non-negative epoch-ms integer >= start.")
        return start, end

    def _show_score(self, evidence: Evidence, *, history_exhausted: bool) -> None:
        """Rule struck-reputation-1 runs in Rust over the SERVED rows only.

        A score exists only when every page of the served history is loaded;
        anything else renders the rule's incomplete-evidence reasons verbatim.
        """
        try:
            start, end = self._window_ms()
        except ValueError as exc:
            self.score.setText(f"Score unavailable: {exc}")
            return
        try:
            report = ScoreReport.model_validate_json(
                struck_py.reputation_score(
                    ScoreRequest(
                        rule="struck-reputation-1",
                        window_start_ms=start,
                        window_end_ms=end,
                        history_exhausted=history_exhausted,
                        balances=evidence.balances,
                        positions=list(self._positions.values()),
                    ).model_dump_json()
                )
            )
        except (ValueError, RuntimeError) as exc:
            self.score.setText(f"Score rejected: {exc}")
            return
        if report.status != "scored" or report.components is None or report.score is None:
            self.score.setText(
                "Score: INCOMPLETE EVIDENCE — " + "; ".join(report.reasons)
            )
            return
        self.score.setText(
            f"Score {report.score}/100 · rule {report.rule_version} · "
            f"components (outcome {report.components.outcome_reliability} / "
            f"net result {report.components.net_result} / "
            f"containment {report.components.loss_containment}) · "
            f"complete closed lives {report.totals.complete_closed_lives} · "
            f"net realized PnL sum {report.totals.net_realized_pnl_sum} · "
            f"worst single loss {report.totals.worst_single_net_loss}\n"
            f"{report.evidence.notice}"
        )

    def _enable(self) -> None:
        self.refresh.setEnabled(True)
        self.selector.setEnabled(True)
        self.identity.setEnabled(True)

    def _failed(self, reason: str) -> None:
        self._enable()
        self.more.setEnabled(False)
        self.table.setRowCount(0)
        self.holdings.clear()
        self.summary.clear()
        self.score.clear()
        self.state.setText(f"Evidence unavailable: {reason}")

    def show_evidence(self, payload: str) -> None:
        try:
            evidence = Evidence.model_validate_json(payload)
            self._show_evidence(evidence)
        except (ValueError, RuntimeError) as exc:
            self._failed(str(exc))

    def _show_evidence(self, evidence: Evidence) -> None:
        history = evidence.history
        if history.has_more and not history.next_cursor:
            raise ValueError("Strike reported more history without a continuation cursor.")
        if history.has_more and history.next_cursor in self._seen_cursors:
            raise ValueError("Strike repeated a history cursor. Reload evidence.")
        if self._account_id is not None and evidence.account.account_id != self._account_id:
            raise ValueError("Resolved account changed during pagination. Reload evidence.")
        if any(row.account_id != evidence.account.account_id for row in history.positions):
            raise ValueError("Position history contains another account's records.")
        positions = dict(self._positions)
        for row in history.positions:
            previous = positions.get(row.id)
            if previous is not None and previous != row:
                raise ValueError("Position history changed during pagination. Reload evidence.")
            positions[row.id] = row
        complete = [row for row in positions.values() if row.data_complete]
        closed = [row for row in complete if row.status == "closed"]
        profitable = sum(
            struck_py.decimal_compare(row.net_realized_pnl, "gt", "0") for row in closed
        )
        losing = sum(
            struck_py.decimal_compare(row.net_realized_pnl, "lt", "0") for row in closed
        )
        flat = len(closed) - profitable - losing
        self._positions = positions
        self._account_id = evidence.account.account_id
        self._pages += 1
        if history.has_more:
            self._seen_cursors.add(history.next_cursor)
        self._enable()
        self._cursor = history.next_cursor if history.has_more else None
        self.more.setEnabled(history.has_more)
        self._show_score(evidence, history_exhausted=not history.has_more)
        coverage = "More history available" if history.has_more else "End of served history"
        self.state.setText(
            f"{coverage} · {self._pages} pages loaded · {evidence.fetched_at}\n"
            f"{evidence.source}\nResponse SHA-256: {evidence.response_sha256}"
        )
        self.summary.setText(
            f"{len(positions)} distinct position lives · {len(complete)} complete · "
            f"{len(positions) - len(complete)} partial\n"
            f"Complete closed lives: {len(closed)} · profitable {profitable} · "
            f"losing {losing} · break-even {flat}\n"
            "Outcomes use Strike's net realized PnL. Partial and still-open lives are excluded. "
            "These counts cover loaded pages only; end of history does not repair partial records."
        )
        self.holdings.setText(
            f"Account {evidence.account.account_id}\n" + "\n".join(
                f"{row.asset}: wallet {row.walletBalance} · available {row.availableBalance} "
                f"· unrealized PnL {row.unrealizedPnl}"
                for row in evidence.balances.root
            )
        )
        self.table.setRowCount(len(positions))
        for index, row in enumerate(positions.values()):
            cells = (
                row.symbol, row.side, row.status, row.net_realized_pnl, row.roi_pct,
                row.trading_fees, row.funding_pnl,
                "Complete" if row.data_complete else "Partial — tracking began after opening",
            )
            for column, value in enumerate(cells):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
