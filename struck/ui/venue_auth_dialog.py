"""Strike API-wallet connect form: paste, import or GENERATE an Ed25519 key
pair into Struck's encrypted secret store, verified by a signed
``GET /v2/account``.

PORT (re-typed, not imported) of 
(HEAD 2d47b95dd, sha256 a8d5cefa…6c35aa; see the golden provenance record). Kept: "Open
Strike API-keys page", label / public key / MASKED private key fields, a
designated wallet, "Verify with venue" (signed GET /v2/account; 200 only for a
registered key) before Save, the 401 hint, the private key is never echoed back.
Changed: PySide6; the form ASKS for the key and the human picks preprod
or mainnet (the key is typed, never hardcoded); the store is
``struck.strike.venue_auth`` (cryptfile, one entry per environment); Save
refuses to overwrite a stored account unless "replace" is ticked; Verify with
empty key fields verifies the STORED account for the picked environment;
fields are cleared after Save; "Generate a new key pair here" (stock PyNaCl)
makes registration possible with NO terminal: the pair is stored encrypted at
once and only the PUBLIC key is shown, for the venue's API-keys page; the form
is embeddable (``StrikeConnectForm``) so the first-run registration dialogue
(``struck.ui.registration``) hosts it with the network FIXED to the chosen one.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QTextEdit,
    QWidget,
)

from ..callhistory import CallFailed, RecordedHttp
from ..config import load_config
from ..network import NETWORKS, NetworkProfile, profile
from ..secrets import SecretError, has_secret
from ..strike.auth import ApiWallet, StrikeCredentialsError, wallet_from_hex
from ..strike.venue_auth import (
    entry_for,
    environment_of,
    generate_account,
    load_account,
    save_account,
    validate_keypair,
    verify,
    wallet_for,
)
from .layout import LAYOUT

UNKNOWN_KEY = (
    "The venue does not know this key. Register the PUBLIC key on the API-keys page "
    "first (the private key never leaves this machine)."
)


def verify_live(wallet: ApiWallet, net: NetworkProfile) -> str:
    """The real check: signed GET /v2/account on the profile's own Strike host."""
    http = RecordedHttp(load_config().timeout_s)
    try:
        return verify(wallet, net, http)
    finally:
        http.close()


class _VerifyThread(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, job: Callable[[], str]) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            self.done.emit(self._job())
        except (CallFailed, StrikeCredentialsError, SecretError, OSError, ValueError) as exc:
            self.failed.emit(str(exc))


class StrikeConnectForm(QWidget):
    """Register / import / generate THE Strike API wallet of a network, in-app.

    ``fixed_network`` (the registration dialogue) locks the environment to the
    network chosen there and hides the picker; otherwise the human picks
    preprod or mainnet here (default: the running network)."""

    account_saved = Signal()
    network_picked = Signal(str)  # the picked profile's label (dialog titles)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        wallet_names: tuple[str, ...] = (),
        verifier: Callable[[ApiWallet, NetworkProfile], str] = verify_live,
        fixed_network: NetworkProfile | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = fixed_network if fixed_network is not None else load_config().profile
        self.environment = environment_of(self.profile)
        self._verifier = verifier
        self._thread: _VerifyThread | None = None
        self.intro = QLabel("")
        self.intro.setWordWrap(True)
        self.open_btn = LAYOUT.button("Open Strike API-keys page", object_name="open_api_keys")
        self.open_btn.setToolTip(self.profile.strike_api_keys_url)
        self.open_btn.clicked.connect(self.open_registration)
        self.label = QLineEdit()
        self.label.setPlaceholderText("account label, e.g. your-wallet")
        self.public_key = QLineEdit()
        self.public_key.setPlaceholderText("64 hex chars: raw Ed25519 public key")
        self.private_key = QLineEdit()
        self.private_key.setObjectName("strike_private_key")
        self.private_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.private_key.setPlaceholderText(
            "64 or 128 hex chars: private key (masked; never shown)"
        )
        self.wallet = QLineEdit()
        self.wallet.setPlaceholderText("Struck wallet it belongs to (optional)")
        if wallet_names:
            self.wallet.setText(wallet_names[0] if len(wallet_names) == 1 else "")
            self.wallet.setToolTip("wallets: " + ", ".join(wallet_names))
        self.replace = QCheckBox("replace the stored account (its private key is then gone)")
        form = LAYOUT.form()
        self.network = QComboBox()
        self.network.setObjectName("strike_network")
        for name in NETWORKS:
            self.network.addItem(profile(name).label)
        if fixed_network is None:
            running = load_config().network
            if running in NETWORKS:
                self.network.setCurrentIndex(NETWORKS.index(running))
            self.network.currentIndexChanged.connect(self._on_network)
            form.addRow("Network", self.network)
        else:
            # A fixed network (registration) must work on a machine where
            # load_config() still refuses: no config query on this path.
            self.network.setVisible(False)
        self.generate_btn = LAYOUT.button(
            "Generate a new key pair here", object_name="generate_strike"
        )
        self.generate_btn.setToolTip(
            "stock PyNaCl, OS randomness: the pair is stored encrypted at once; "
            "only the PUBLIC key is shown, for the venue's API-keys page"
        )
        self.generate_btn.clicked.connect(self.generate_clicked)
        form.addRow("Account label", self.label)
        form.addRow("Public key", self.public_key)
        form.addRow("Private key", self.private_key)
        form.addRow("Struck wallet", self.wallet)
        form.addRow("", self.generate_btn)
        form.addRow("", self.replace)
        self.stored = QLabel("")
        self.stored.setObjectName("mono")
        self.stored.setWordWrap(True)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.account_view = QTextEdit()
        self.account_view.setReadOnly(True)
        self.account_view.setVisible(False)
        self.account_view.setMaximumHeight(140)
        self.verify_btn = LAYOUT.button("Verify wallet on Strike (signed read)", object_name="verify_strike")
        self.verify_btn.clicked.connect(self.verify_clicked)
        self.save_btn = LAYOUT.button("Save account", object_name="save_strike")
        self.save_btn.clicked.connect(self.save_clicked)
        buttons = LAYOUT.hbox(LAYOUT.GAP_TIGHT)
        buttons.addWidget(self.verify_btn)
        buttons.addWidget(self.save_btn)
        buttons.addStretch(1)
        root = LAYOUT.vbox()
        self.setLayout(root)
        root.addWidget(self.intro)
        root.addWidget(self.open_btn)
        root.addLayout(form)
        root.addWidget(self.stored)
        root.addWidget(self.status)
        root.addWidget(self.account_view)
        root.addLayout(buttons)
        self._apply(self.profile)

    def _apply(self, net: NetworkProfile) -> None:
        """Show the picked network's rules. The key is never filled in for them."""
        self.profile = net
        self.environment = environment_of(net)
        self.intro.setText(
            f"{net.label}. Paste or generate the Ed25519 API wallet for this network. "
            "Register the public key on the venue's API-keys page first (or use "
            "'Generate a new key pair here'). The pair is stored "
            f"encrypted as {entry_for(self.environment)}. Nothing is hardcoded, and the "
            "private key is never shown again."
        )
        self.open_btn.setToolTip(net.strike_api_keys_url)
        self.network_picked.emit(net.label)
        self.show_stored()

    def _on_network(self, index: int) -> None:
        if 0 <= index < len(NETWORKS):
            self._apply(profile(NETWORKS[index]))

    def show_stored(self) -> None:
        """Metadata of the stored account only (never the private key)."""
        try:
            if not has_secret(entry_for(self.environment)):
                self.stored.setText(f"stored: none ({entry_for(self.environment)} missing)")
                return
            a = load_account(self.environment)
        except (SecretError, StrikeCredentialsError) as exc:
            self.stored.setText(f"stored: unavailable ({exc})")
            return
        self.stored.setText(
            f"stored: '{a.label}' wallet {a.wallet or '-'} public key {a.public_key_hex}"
        )

    def open_registration(self) -> None:
        QDesktopServices.openUrl(QUrl(self.profile.strike_api_keys_url))

    def _entered(self) -> tuple[str, str] | None:
        """(public, seed) when both key fields are filled; None when both are empty."""
        pub = self.public_key.text().strip()
        priv = self.private_key.text().strip()
        if not pub and not priv:
            return None
        if not pub or not priv:
            raise StrikeCredentialsError("paste both the public and the private key")
        return validate_keypair(pub, priv)

    def generate_clicked(self) -> None:
        """A NEW key pair made HERE (no terminal): stored encrypted at once; only
        the public key is shown, to register on the venue's API-keys page."""
        label = self.label.text().strip()
        if not label:
            self.status.setText("NOT GENERATED: give the account a label first")
            return
        try:
            account = generate_account(
                self.environment,
                label,
                self.wallet.text().strip(),
                replace=self.replace.isChecked(),
            )
        except (StrikeCredentialsError, SecretError) as exc:
            self.status.setText(f"NOT GENERATED: {exc}")
            return
        self.replace.setChecked(False)
        self.account_view.setVisible(True)
        self.account_view.setPlainText(account.public_key_hex)
        self.status.setText(
            f"GENERATED '{account.label}' (private key stored encrypted, never shown). "
            f"Register this PUBLIC key at {self.profile.strike_api_keys_url}, "
            "then press Verify wallet on Strike."
        )
        self.show_stored()
        self.account_saved.emit()

    def verify_clicked(self) -> None:
        try:
            pair = self._entered()
            wallet = wallet_for(self.environment) if pair is None else wallet_from_hex(*pair)
        except (StrikeCredentialsError, SecretError) as exc:
            self.status.setText(str(exc))
            return
        what = "the stored account" if pair is None else "the entered key pair (not saved yet)"
        self.status.setText(
            f"verifying {what}: signed GET /v2/account on {self.profile.strike_api_url}…"
        )
        self.verify_btn.setEnabled(False)
        self.network.setEnabled(False)
        chosen, verifier = self.profile, self._verifier
        self._thread = _VerifyThread(lambda: verifier(wallet, chosen))
        self._thread.done.connect(self._verified)
        self._thread.failed.connect(self._refused)
        self._thread.start()

    def _verified(self, body: str) -> None:
        self.verify_btn.setEnabled(True)
        self.network.setEnabled(True)
        self.account_view.setVisible(True)
        self.account_view.setPlainText(body[:2000])
        self.status.setText(
            f"VERIFIED: {self.profile.strike_api_url} answered 200 to the signed GET /v2/account."
        )

    def _refused(self, message: str) -> None:
        self.verify_btn.setEnabled(True)
        self.network.setEnabled(True)
        self.account_view.setVisible(False)
        self.status.setText(
            f"NOT VERIFIED: {UNKNOWN_KEY} ({message})"
            if "401" in message
            else f"NOT VERIFIED: {message}"
        )

    def save_clicked(self) -> None:
        label = self.label.text().strip()
        try:
            pair = self._entered()
            if pair is None:
                raise StrikeCredentialsError("paste both the public and the private key")
            if not label:
                raise StrikeCredentialsError("give the account a label")
            account = save_account(
                self.environment,
                label,
                pair[0],
                pair[1],
                self.wallet.text().strip(),
                replace=self.replace.isChecked(),
            )
        except (StrikeCredentialsError, SecretError) as exc:
            self.status.setText(f"NOT SAVED: {exc}")
            return
        finally:
            self.private_key.clear()
        self.public_key.clear()
        self.replace.setChecked(False)
        self.status.setText(
            f"SAVED: '{account.label}' as {entry_for(self.environment)} (private key not shown)"
        )
        self.show_stored()
        self.account_saved.emit()


class StrikeConnectDialog(QDialog):
    """Connect Strike API wallet (the Settings tab's entry point to the form)."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        wallet_names: tuple[str, ...] = (),
        verifier: Callable[[ApiWallet, NetworkProfile], str] = verify_live,
    ) -> None:
        super().__init__(parent)
        self.form = StrikeConnectForm(self, wallet_names=wallet_names, verifier=verifier)
        self.form.network_picked.connect(self._retitled)
        self.setWindowTitle(f"Connect Strike API wallet ({self.form.profile.label})")
        root = LAYOUT.vbox()
        self.setLayout(root)
        root.addWidget(self.form)
        LAYOUT.fit_dialog(self)

    def _retitled(self, label: str) -> None:
        self.setWindowTitle(f"Connect Strike API wallet ({label})")
