"""The Strike deposit proposal editor (K03): one strict ``DepositPlanProposal``
builder — nothing else.

The human picks an imported Cardano wallet ON THE PINNED NETWORK from a
selector filled with PUBLIC ``WalletInfo`` rows (``struck.wallets.list_wallets``:
name, network, address only — never a mnemonic, never a free-typed name),
enters the amount as INTEGER base-unit text, and picks asset/mode from the
domains the model and the official strike-deposit skill define. A valid form
emits ONE strict proposal through the typed callback; the integrator wires
that to the existing human gates (``struck.strike.deposit_cli``: ``quote`` ->
``sign`` -> ``confirm``). This form never quotes, signs, submits, builds a
transaction, or does money arithmetic: the Rust core owns money
(``struck_py.compare_quantities`` is the same quantity check the quote path
runs; ``struck_py.format_lovelace`` is the same display formatting the CLI
uses), and the Strike quote/build-tx calls stay behind the human's word.

``blockchain`` is fixed to the ``cardano`` lane: Struck's wallet store holds
only Cardano wallets, ``user_address`` comes from the selected wallet, and the
existing deposit CLI wires the Cardano lane only (it refuses every other
chain). Locked/empty/wrong-network store states are loud, never a silent
empty list. Layout comes from the canonical ``LAYOUT`` factory
(``struck.ui.layout``); the page scrolls so fields never truncate and the
action row stays visible.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import struck_py
from pydantic import BaseModel, ConfigDict, StrictStr
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPlainTextEdit, QWidget

from ..config import load_config
from ..secrets import SecretError
from ..strike.deposit import DepositMode, DepositPlanProposal
from ..wallets import WalletError, WalletInfo, list_wallets
from .layout import LAYOUT
from .panels import Worker, network_badge, ui_log

# Strike serves NO deposit asset list (live probe 2026-10-22: /v2/assets,
# /v2/deposit/assets, /v2/deposit/config all 404 on mainnet — see
# receipts/markets-diff-20261022/DIAGNOSIS.md). The asset is therefore a
# free-typed field validated ONLY by Strike's quote response: an unsupported
# asset is Strike's 400 at quote time, reported loudly, never guessed here.

# The skill's ``mode``: absent = Strike's documented default (balance), which
# ``quote_body`` omits from the POST body; or explicit ``collateral``.
MODE_DEFAULT: Final = "balance (omitted — Strike's documented default)"
MODE_COLLATERAL: Final = "collateral"

LANE_BLOCKCHAIN: Final = "cardano"


class _WalletRow(BaseModel):
    """One PUBLIC ``WalletInfo`` row: name, network, address — nothing else."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: StrictStr
    network: StrictStr
    address: StrictStr


class _WalletPayload(BaseModel):
    """What crosses the thread boundary: text only (the Worker pattern)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    label: str  # the pinned network's label at fetch time
    network: str  # the pinned network's name at fetch time
    wallets: tuple[_WalletRow, ...]  # every public row, as served
    error: str  # "" or the exact fail-closed reason (locked/corrupt store)


def _rows_of(wallets: tuple[WalletInfo, ...]) -> tuple[_WalletRow, ...]:
    return tuple(
        _WalletRow(name=w.name, network=w.network, address=w.address) for w in wallets
    )


class DepositForm(QWidget):
    """A reusable deposit PROPOSAL form. The callback receives strict
    ``DepositPlanProposal`` values; nothing on this form sends anything."""

    def __init__(self, propose: Callable[[DepositPlanProposal], None]) -> None:
        super().__init__()
        self._propose = propose
        self._rows: tuple[_WalletRow, ...] = ()  # on-network rows, combo order
        self._worker: Worker | None = None
        cfg = load_config()

        # 01 — the paying wallet: a selector of imported wallets on the pinned
        # network, filled from PUBLIC metadata only. Never a free-typed name.
        self.state = QLabel(f"Not loaded — press Refresh wallets ({cfg.profile.label} is pinned).")
        self.state.setWordWrap(True)
        self.refresh = LAYOUT.button("Refresh wallets", "default", compact=False)
        self.refresh.clicked.connect(self.load)
        self.wallet = QComboBox()  # not editable: a selector, not a text field
        self.wallet.setPlaceholderText("no wallet on the pinned network is loaded yet")
        self.wallet.currentIndexChanged.connect(self._show_address)
        self.address_note = QLabel("(no wallet selected)")
        self.address_note.setObjectName("muted")
        self.address_note.setWordWrap(True)
        self.excluded_note = QLabel("")
        self.excluded_note.setObjectName("muted")
        self.excluded_note.setWordWrap(True)
        wallet_card, wallet_inner = LAYOUT.card("01 — Wallet (imported, on the pinned network)")
        wallet_form = LAYOUT.form()
        wallet_form.addRow("wallet", self.wallet)
        wallet_inner.addWidget(self.state)
        wallet_inner.addWidget(self.refresh)
        wallet_inner.addLayout(wallet_form)
        wallet_inner.addWidget(self.address_note)
        wallet_inner.addWidget(self.excluded_note)

        # 02 — the deposit fields, straight from the DepositPlanProposal model.
        self.asset = QComboBox()  # editable: no asset list is served by Strike
        self.asset.setEditable(True)
        self.asset.setPlaceholderText("asset — free-typed; Strike's quote validates it")
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("asset_amount — smallest units, digits only (ADA: lovelace)")
        self.mode = QComboBox()  # not editable: the model's two documented modes
        self.mode.addItems([MODE_DEFAULT, MODE_COLLATERAL])
        self.rationale = QPlainTextEdit()
        self.rationale.setPlaceholderText(
            "rationale — plain words the human reads at the approval gate"
        )
        deposit_card, deposit_inner = LAYOUT.card(
            f"02 — Deposit (DepositPlanProposal, {LANE_BLOCKCHAIN} lane; "
            "a quote STARTS a deposit)"
        )
        deposit_form = LAYOUT.form()
        deposit_form.addRow("blockchain", QLabel(f"{LANE_BLOCKCHAIN} (the stored wallets' chain)"))
        deposit_form.addRow("asset_symbol", self.asset)
        deposit_form.addRow("asset_amount", self.amount)
        deposit_form.addRow("mode", self.mode)
        deposit_form.addRow("rationale", self.rationale)
        deposit_inner.addLayout(deposit_form)

        self.log = QPlainTextEdit()
        self.log.setObjectName("mono")
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        self.propose_btn = LAYOUT.button("Propose deposit", "primary", compact=False)
        self.propose_btn.clicked.connect(self.propose)
        hint = QLabel(
            "A proposal only: quoting, signing and submitting stay behind the human's "
            "quote/sign/confirm gates (deposit_cli)."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)

        page, _ = LAYOUT.page([wallet_card, deposit_card])
        outer = LAYOUT.vbox()
        outer.addWidget(network_badge(cfg.profile))
        outer.addWidget(LAYOUT.scroll(page), 1)  # the page scrolls; fields never truncate
        action = LAYOUT.hbox(LAYOUT.GAP_TIGHT)
        action.addWidget(hint, 1)
        action.addWidget(self.propose_btn)  # outside the scroll: always visible
        outer.addLayout(action)
        outer.addWidget(self.log)
        self.setLayout(outer)

    # ---- wallet load (public metadata, off the UI thread) ----------------
    def load(self) -> None:
        """``list_wallets()`` through the stock Worker: PUBLIC rows only. A
        locked store surfaces its exact fail-closed reason, never a silent
        empty list."""
        if self._worker is not None and self._worker.isRunning():
            return
        self._fill(())
        self.excluded_note.clear()
        self.wallet.setEnabled(False)
        self.refresh.setEnabled(False)
        self.state.setText(f"Reading wallets on {load_config().profile.label} …")
        self._worker = Worker(self._fetch_wallets)
        self._worker.done.connect(self._show)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _fetch_wallets(self) -> str:
        """The real Worker body: the public store read, as a text payload."""
        cfg = load_config()
        try:
            rows, error = _rows_of(list_wallets()), ""
        except (SecretError, WalletError) as exc:  # the exact fail-closed reason
            rows, error = (), str(exc)
        return _WalletPayload(
            label=cfg.profile.label, network=cfg.network, wallets=rows, error=error
        ).model_dump_json()

    def _failed(self, reason: str) -> None:
        self._fill(())
        self.wallet.setEnabled(False)
        self.refresh.setEnabled(True)
        self.state.setText(ui_log("deposit", f"FAILED: {reason}"))

    def _show(self, payload: str) -> None:
        """The Worker 'done' slot: fill the selector with on-network wallets,
        exclude the wrong-network ones loudly, or show the store's refusal."""
        view = _WalletPayload.model_validate_json(payload)
        if view.error:
            self._fill(())
            self.wallet.setEnabled(False)
            self.state.setText(ui_log("deposit", f"wallet list unavailable: {view.error}"))
            self.refresh.setEnabled(True)
            return
        self.wallet.setEnabled(True)
        self._fill(tuple(w for w in view.wallets if w.network == view.network))
        excluded = tuple(w for w in view.wallets if w.network != view.network)
        if excluded:
            names = ", ".join(f"{w.name} ({w.network})" for w in excluded)
            self.excluded_note.setText(f"EXCLUDED (not on {view.network}): {names}")
        else:
            self.excluded_note.setText("")
        if self._rows:
            suffix = f"; {len(excluded)} excluded as wrong network" if excluded else ""
            self.state.setText(f"{len(self._rows)} wallet(s) on {view.network}{suffix}")
        elif excluded:
            self.state.setText(
                f"NO wallet on {view.network}: every imported wallet is on another network "
                f"(see the exclusion note). Import a {view.network} wallet in the Wallets tab."
            )
        else:
            self.state.setText(
                f"NO wallets imported: the store is unlocked but empty — import one in the "
                f"Wallets tab ({view.label})."
            )
        self.refresh.setEnabled(True)

    def _fill(self, rows: tuple[_WalletRow, ...]) -> None:
        self._rows = rows
        self.wallet.blockSignals(True)
        self.wallet.clear()
        self.wallet.addItems([w.name for w in rows])
        # A non-editable combo with a placeholder keeps index -1 after
        # addItems (Qt 6.12): select the first on-network wallet explicitly.
        self.wallet.setCurrentIndex(0 if rows else -1)
        self.wallet.blockSignals(False)
        self._show_address()

    def _selected_wallet(self) -> _WalletRow | None:
        index = self.wallet.currentIndex()
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def _show_address(self) -> None:
        row = self._selected_wallet()
        self.address_note.setText(
            f"pays from {row.address} ({row.network})"
            if row is not None
            else "(no wallet selected)"
        )

    # ---- the one action: build a STRICT proposal --------------------------
    def _report(self, text: str) -> None:
        self.log.appendPlainText(ui_log("deposit", text))

    def propose(self) -> None:
        """Build ONE strict ``DepositPlanProposal`` and hand it to the
        callback. Invalid input is REJECTED loudly — no repair, no retry,
        no fake-valid values."""
        cfg = load_config()
        row = self._selected_wallet()
        if row is None:
            self._report(
                f"REJECTED: no wallet on {cfg.network} is selected — press Refresh wallets "
                "and pick one (a free-typed wallet name is never accepted)"
            )
            return
        if row.network != cfg.network:  # defence in depth; the selector never offers it
            self._report(
                f"REJECTED: NETWORK MISMATCH: wallet '{row.name}' is a {row.network} wallet; "
                f"the pinned network is {cfg.network}. Nothing proposed."
            )
            return
        amount = self.amount.text().strip()
        try:
            # The same stock Rust quantity check the quote path runs: ASCII
            # digits only, no sign, no decimal point, greater than zero.
            if not struck_py.compare_quantities(amount, "gt", "0"):
                self._report(
                    f"REJECTED: asset_amount {amount!r} must be a positive integer in the "
                    "smallest unit (digits only — ADA: lovelace)"
                )
                return
        except ValueError as exc:
            self._report(f"REJECTED: asset_amount: {exc}")
            return
        rationale = self.rationale.toPlainText().strip()
        if not rationale:
            self._report("REJECTED: rationale is required (plain words for the human approval)")
            return
        mode: DepositMode | None = (
            "collateral" if self.mode.currentText() == MODE_COLLATERAL else None
        )
        asset = self.asset.currentText()
        try:
            proposal = DepositPlanProposal(
                lane="deposit",
                intent=(
                    f"deposit {asset} from wallet '{row.name}' to Strike "
                    f"{cfg.profile.strike_venue} on {cfg.profile.cardano_label}"
                ),
                blockchain=LANE_BLOCKCHAIN,
                asset_symbol=asset,
                asset_amount=amount,
                user_address=row.address,
                mode=mode,
                rationale=rationale,
            )
        except ValueError as exc:  # pydantic ValidationError: loud, never repaired
            self._report(f"REJECTED: {exc}")
            return
        shown = proposal.asset_amount
        if asset == "ADA":
            # Rust display formatting of an already-checked integer (as deposit_cli does)
            shown += f" ({struck_py.format_lovelace(int(proposal.asset_amount))})"
        self._report(
            f"proposed {proposal.intent} — asset_amount {shown}; plan_hash "
            f"{proposal.plan_hash} -> human approval; NOTHING quoted, signed or sent "
            "from this form"
        )
        self._propose(proposal)
