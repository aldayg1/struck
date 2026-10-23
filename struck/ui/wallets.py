"""Encrypted Cardano wallet management; sends use the existing approval panel."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from .layout import LAYOUT
from .venue_auth_dialog import StrikeConnectDialog
from ..config import load_config
from ..secrets import SecretError, has_secret
from ..strike.venue_auth import environment_of, entry_for
from ..wallets import WalletError, WalletInfo, WalletType, import_mnemonic, list_wallets


class WalletsPanel(QWidget):
    """Only public wallet metadata leaves the encrypted store for display."""

    wallets_changed = Signal()

    def __init__(self, open_wallet: Callable[[WalletInfo], None]) -> None:
        super().__init__()
        self._open_wallet = open_wallet
        self._wallets: tuple[WalletInfo, ...] = ()
        self._dialog: StrikeConnectDialog | None = None
        self.strike_state = QLabel("")
        self.strike_state.setWordWrap(True)
        self.strike_create = QPushButton("CREATE STRIKE WALLET")
        self.strike_create.setObjectName("create_strike")
        self.strike_create.clicked.connect(self.open_strike_dialog)
        self.strike_use = QPushButton("Use existing Strike API keys…")
        self.strike_use.clicked.connect(self.open_strike_dialog)
        strike_form = LAYOUT.form()
        strike_form.addRow(self.strike_state)
        strike_row = LAYOUT.hbox()
        strike_row.addWidget(self.strike_create)
        strike_row.addWidget(self.strike_use)
        strike_form.addRow(strike_row)
        self.strike_group = QGroupBox(
            "STRIKE WALLET — the account your orders, strategies and deposits sign with"
        )
        self.strike_group.setLayout(strike_form)
        self.state = QLabel("Unlock the secret store in Settings to load wallets.")
        self.state.setWordWrap(True)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Network", "Address"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        refresh = QPushButton("Refresh wallets")
        refresh.clicked.connect(self.refresh)
        inspect = QPushButton("Inspect holdings / prepare send")
        inspect.clicked.connect(self.inspect_selected)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Wallet name")
        self.mnemonic = QLineEdit()
        self.mnemonic.setEchoMode(QLineEdit.EchoMode.Password)
        self.mnemonic.setPlaceholderText("BIP-39 phrase — stored encrypted")
        self.expected_address = QLineEdit()
        self.expected_address.setPlaceholderText("Optional expected address; mismatch refuses import")
        self.wallet_type = QComboBox()
        self.wallet_type.addItems(["standard", "enterprise"])
        store = QPushButton("Import into encrypted store")
        store.clicked.connect(self.import_wallet)
        form = LAYOUT.form()
        form.addRow("Network", QLabel(load_config().profile.label))
        form.addRow("Name", self.name)
        form.addRow("Address type", self.wallet_type)
        form.addRow("Recovery phrase", self.mnemonic)
        form.addRow("Expected address", self.expected_address)
        form.addRow(store)
        group = QGroupBox("Import Cardano wallet")
        group.setLayout(form)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        title = QLabel("WALLETS · Your agents. Your rules.")
        title.setObjectName("brand")
        layout.addWidget(title)
        layout.addWidget(self.strike_group)
        layout.addWidget(self.state)
        layout.addWidget(refresh)
        layout.addWidget(self.table, 1)
        layout.addWidget(inspect)
        layout.addWidget(group)

    def open_strike_dialog(self) -> None:
        """CREATE STRIKE WALLET / use existing keys: the connect dialogue
        generates, stores encrypted, and verifies with a signed Strike read."""
        try:
            has_secret(entry_for(environment_of(load_config().profile)))
        except SecretError:
            self.strike_state.setText(
                "The secret store is LOCKED. Type your master password in Settings "
                "(Unlock secret store), then press CREATE STRIKE WALLET again."
            )
            return
        if self._dialog is None:
            self._dialog = StrikeConnectDialog(self)
        self._dialog.show()
        self.refresh_strike()

    def refresh_strike(self) -> None:
        try:
            stored = has_secret(entry_for(environment_of(load_config().profile)))
        except SecretError:
            self.strike_state.setText(
                "STRIKE WALLET: the store is locked — unlock in Settings, then CREATE STRIKE WALLET."
            )
            return
        if stored:
            self.strike_state.setText(
                f"STRIKE WALLET: stored for {load_config().profile.label}. "
                "Use 'Use existing Strike API keys…' to verify it with a signed read."
            )
        else:
            self.strike_state.setText(
                "STRIKE WALLET: none yet for this network. Press CREATE STRIKE WALLET "
                "(generates the key pair, stores it encrypted, verifies it on Strike)."
            )

    def refresh(self) -> None:
        self.refresh_strike()
        self.table.setRowCount(0)
        self._wallets = ()
        try:
            self._wallets = list_wallets()
        except (SecretError, WalletError) as exc:
            self.state.setText(str(exc))
            return
        self.table.setRowCount(len(self._wallets))
        for row, wallet in enumerate(self._wallets):
            for column, value in enumerate((wallet.name, wallet.network, wallet.address)):
                self.table.setItem(row, column, QTableWidgetItem(value))
        network = load_config().network
        selected = next((i for i, wallet in enumerate(self._wallets) if wallet.network == network), None)
        if selected is not None:
            self.table.selectRow(selected)
        self.table.resizeColumnsToContents()
        self.state.setText(
            f"{len(self._wallets)} wallet(s). Active network: {load_config().profile.label}"
            if self._wallets
            else "Store unlocked; no Cardano wallets imported. Import a wallet below."
        )

    def import_wallet(self) -> None:
        mnemonic = self.mnemonic.text()
        self.mnemonic.clear()
        wallet_type: WalletType = "enterprise" if self.wallet_type.currentText() == "enterprise" else "standard"
        try:
            wallet = import_mnemonic(
                self.name.text().strip(),
                mnemonic,
                load_config().network,
                wallet_type,
                expect_address=self.expected_address.text().strip() or None,
            )
        except (SecretError, WalletError) as exc:
            self.state.setText(f"Import refused: {exc}")
            return
        finally:
            mnemonic = ""
        self.refresh()
        self.wallets_changed.emit()
        self.state.setText(f"Imported {wallet.name} on {wallet.network}: {wallet.address}")

    def inspect_selected(self) -> None:
        index = self.table.currentRow()
        if not 0 <= index < len(self._wallets):
            self.state.setText("Select a wallet first.")
            return
        wallet = self._wallets[index]
        if wallet.network != load_config().network:
            self.state.setText(
                f"Network mismatch: {wallet.name} belongs to {wallet.network}. "
                "Select that network in Settings and restart before using this wallet."
            )
            return
        self._open_wallet(wallet)
