"""First-run registration: ONE dialogue, every field visible at once.

Network + master password + Strike API wallet (paste or GENERATE) on a single
page — no step wizard hiding fields, no refusal text scolding anyone. Shown by
``struck.ui.app`` until registration is COMPLETE (the private marker
``<user config dir>/registered.json``): a half-finished registration (a saved
network but no API wallet) re-opens the dialogue, not the main window.
"Delete this store and start over" (explicit confirm; it destroys EVERY secret
inside) is the retry path. Cancelling still exits loudly; there is no default
network anywhere. Every write goes through the exact production paths:
``save_network_setting`` (``network.json``, 0600, atomic), ``struck.secrets``
(the ONE encrypted store), ``struck.strike.venue_auth`` save/generate.
Layout is 100% ``struck.ui.layout`` factories; buttons are the compact dialog
size; the whole page scrolls rather than truncate.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QWidget,
)
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from ..config import CONFIG_PATH, load_config
from ..network import (
    NETWORKS,
    NetworkError,
    NetworkProfile,
    load_network_setting,
    profile,
    save_network_setting,
)
from ..paths import user_config_dir
from ..private_file import read_private, write_private
from ..secrets import (
    SECRETS_PATH,
    SecretError,
    has_secret,
    is_unlocked,
    lock,
    store_exists,
    unlock,
)
from ..strike.auth import StrikeCredentialsError
from ..strike.venue_auth import entry_for, save_account
from . import theme
from .layout import LAYOUT
from .panels import _mono, ui_log
from .venue_auth_dialog import StrikeConnectForm


def fresh_config_json() -> str:
    """The endpoints file registration writes on a machine that has none: the
    public Koios chain reader only. No local node is configured (``node_socket``
    null everywhere): the node backend refuses loudly until a real socket and
    cardano-cli path are filled in by hand; the loopback Kupo/Ogmios URLs are
    required fields the Koios backend never calls."""
    payload: dict[str, object] = {
        "chain_backend": "koios",
        "node_socket": {"preprod": None, "mainnet": None},
        "cardano_cli_path": "/usr/bin/cardano-cli",
        "kupo_url": "http://127.0.0.1:1442",
        "ogmios_url": "http://127.0.0.1:1337",
        "timeout_s": 20,
    }
    return json.dumps(payload, indent=2) + "\n"


class _Registered(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    network: StrictStr


REGISTRATION_PATH = user_config_dir() / "registered.json"


def registration_complete() -> bool:
    """True only when registration FINISHED once on this machine (the private
    marker exists and parses). A saved network alone is NOT completion."""
    try:
        data = read_private(REGISTRATION_PATH)
    except ValueError:
        return False
    if data is None:
        return False
    try:
        _Registered.model_validate_json(data)
    except ValidationError:
        return False
    return True


def _mark_registered(name: str) -> None:
    write_private(
        REGISTRATION_PATH,
        (_Registered(network=profile(name).name).model_dump_json() + "\n").encode("utf-8"),
    )


def _confirm_delete_store(parent: QWidget) -> QMessageBox.StandardButton:
    return QMessageBox.question(
        parent,
        "Delete the secret store?",
        f"This PERMANENTLY DELETES {SECRETS_PATH} and EVERY secret inside "
        "(Strike API wallets, Cardano wallet seeds, LLM keys). There is no undo.\n\n"
        "Delete it and start registration over?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )


class RegistrationDialog(QDialog):
    """One page: network, master password, Strike API wallet, Register.

    The API card builds when a network is picked (keys are per-environment);
    the network choice is saved the moment it is picked (the Settings-tab
    mechanism — applies on the next start), Register only completes the rest.
    """

    def __init__(self, reason: str, *, config_path: Path = CONFIG_PATH) -> None:
        super().__init__()
        self.setModal(True)
        self.setWindowTitle("Struck · Set up")
        self.config_path = config_path
        self._form: StrikeConnectForm | None = None
        self._new_store = not store_exists()
        cards: list[QWidget] = []
        if not config_path.is_file():
            cards.append(self._build_endpoints_card())
        self._network_card = self._build_network_card()
        cards.append(self._network_card)
        self._build_master_card()
        cards.append(self._master_card)
        cards.append(self._build_api_card())
        register = LAYOUT.button("Register", "primary", object_name="reg_register")
        register.clicked.connect(self._register)
        cancel = LAYOUT.button("Cancel", "danger", object_name="reg_cancel")
        cancel.clicked.connect(self.reject)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        logo = QLabel()
        logo.setPixmap(QPixmap(str(theme.LOGO_PATH)).scaledToHeight(28))
        brand = QLabel("STRUCK")
        brand.setObjectName("brand")
        header = LAYOUT.hbox(LAYOUT.GAP_TIGHT)
        header.addWidget(logo)
        header.addWidget(brand)
        header.addWidget(QLabel("Set up"))
        header.addStretch(1)
        page, column = LAYOUT.page(cards)
        column.addLayout(header)
        # Buttons/status/why stay OUTSIDE the scroll: always visible, never cut.
        root = LAYOUT.vbox(LAYOUT.GAP, LAYOUT.MARGIN)
        root.addWidget(LAYOUT.scroll(page))
        root.addLayout(LAYOUT.dialog_row(register, cancel))
        root.addWidget(self.status)
        if reason:
            why = QLabel(f"why: {reason}")
            why.setObjectName("muted")
            why.setWordWrap(True)
            root.addWidget(why)
        self.setLayout(root)
        LAYOUT.fit_dialog(self)
        initial = self._picked_profile()
        if initial is not None:
            self._build_form(initial)  # a saved network: the API card starts built

    # -- local endpoints (only on a machine whose config file is missing) ----
    def _build_endpoints_card(self) -> QWidget:
        box, v = LAYOUT.card("Local endpoints file (config/struck.json — missing here)")
        explain = QLabel(
            "No local endpoints file. 'Write it' creates the PUBLIC Koios chain reader "
            "below (no local node, nothing to install); Register uses it too. To run a "
            "local node later, edit config/struck.json by hand — the node backend "
            "refuses loudly until a real socket is configured."
        )
        explain.setWordWrap(True)
        self._config_view = _mono()
        self._config_view.setPlainText(fresh_config_json())
        self._config_view.setMaximumHeight(120)
        write = LAYOUT.button("Write it", object_name="reg_write_config")
        write.clicked.connect(self._write_config)
        self._config_status = QLabel("")
        self._config_status.setWordWrap(True)
        v.addWidget(explain)
        v.addWidget(self._config_view)
        v.addWidget(write)
        v.addWidget(self._config_status)
        return box

    def _write_config(self) -> None:
        try:
            # write_private creates the parent (0700) itself: no mkdir here, so
            # the private-dir checks see only properly-mode'd directories.
            write_private(self.config_path, fresh_config_json().encode("utf-8"))
        except (ValueError, OSError) as exc:
            self._config_status.setText(ui_log("registration", f"[NOT WRITTEN: {exc}]"))
            return
        self._config_status.setText(ui_log("registration", f"[WROTE {self.config_path}]"))
        self._config_view.setVisible(False)

    # -- the ONE network setting ---------------------------------------------
    def _build_network_card(self) -> QWidget:
        box, v = LAYOUT.card("Network")
        self.network = QComboBox()
        self.network.setObjectName("reg_network")
        self.network.addItem("choose a network…", None)
        for name in NETWORKS:
            p = profile(name)
            self.network.addItem(p.label, p.name)
        saved = self._saved_setting()
        if saved is not None:
            # No signal yet: the API host does not exist during construction.
            self.network.blockSignals(True)
            self.network.setCurrentIndex(NETWORKS.index(saved) + 1)
            self.network.blockSignals(False)
        self.network.currentIndexChanged.connect(self._pick_network)
        self._net_status = QLabel("")
        self._net_status.setWordWrap(True)
        v.addWidget(self.network)
        v.addWidget(self._net_status)
        return box

    @staticmethod
    def _saved_setting() -> str | None:
        try:
            return load_network_setting()
        except NetworkError:
            return None

    def _picked_profile(self) -> NetworkProfile | None:
        data: object = self.network.currentData()
        if isinstance(data, str):
            return profile(data)
        return None

    def _pick_network(self, index: int) -> None:
        """A pick is saved AT ONCE (the Settings-tab mechanism), and the API
        card builds for that environment — keys are per-environment."""
        p = self._picked_profile()
        if p is None or index == 0:
            return
        try:
            save_network_setting(p.name)
        except (NetworkError, ValueError, OSError) as exc:
            self._net_status.setText(ui_log("registration", f"[NOT SAVED: {exc}]"))
            return
        self._net_status.setText(ui_log("registration", f"[saved {p.name}]"))
        self._build_form(p)

    # -- master password of the ONE encrypted secret store --------------------
    def _build_master_card(self) -> QGroupBox:
        box, v = LAYOUT.card("Master password")
        self._master_card = box
        rows = LAYOUT.hbox(LAYOUT.GAP_TIGHT)
        self.pw = QLineEdit()
        self.pw.setObjectName("reg_pw")
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw2 = QLineEdit()
        self.pw2.setObjectName("reg_pw2")
        self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw2.setPlaceholderText("repeat")
        self._pw2_label = QLabel("repeat")
        rows.addWidget(QLabel("password"))
        rows.addWidget(self.pw, 1)
        rows.addWidget(self._pw2_label)
        rows.addWidget(self.pw2, 1)
        unlock_btn = LAYOUT.button(
            "Unlock / create the store", object_name="reg_unlock", variant="primary"
        )
        unlock_btn.clicked.connect(self._unlock_clicked)
        self._start_over = LAYOUT.button(
            "Delete this store and start over", variant="danger", object_name="reg_start_over"
        )
        self._start_over.setToolTip(
            "Destroys EVERY secret inside (Strike keys, wallet seeds, LLM keys)"
        )
        self._start_over.clicked.connect(self._start_over_clicked)
        buttons = LAYOUT.hbox(LAYOUT.GAP_TIGHT)
        buttons.addWidget(unlock_btn)
        buttons.addWidget(self._start_over)
        buttons.addStretch(1)
        self._master_status = QLabel("")
        self._master_status.setWordWrap(True)
        facts = QLabel(
            f"One encrypted file ({SECRETS_PATH}, Argon2id + AES-GCM) holds every secret: "
            "the Strike API wallet, wallet seeds, LLM keys. The password exists only in "
            "your head: lose it and every secret inside is gone."
        )
        facts.setObjectName("muted")
        facts.setWordWrap(True)
        v.addLayout(rows)
        v.addLayout(buttons)
        v.addWidget(self._master_status)
        v.addWidget(facts)
        self._sync_master_card()
        return box

    def _pw_placeholder(self) -> None:
        self.pw.setPlaceholderText(
            "choose a master password (masked; never stored, never logged)"
            if self._new_store
            else "master password (masked)"
        )

    def _sync_master_card(self) -> None:
        """Card title / repeat field / start-over visibility match the state."""
        self._master_card.setTitle(
            "Master password — create the store"
            if self._new_store
            else "Master password — unlock the existing store"
        )
        self._pw2_label.setVisible(self._new_store)
        self.pw2.setVisible(self._new_store)
        self._start_over.setVisible(not self._new_store)
        self._pw_placeholder()

    def _unlock_clicked(self) -> None:
        """Unlock (or create) the store NOW, so Generate/Save in the API card
        work on the same page — no order imposed, no page flipping."""
        master = self.pw.text()
        repeat = self.pw2.text()
        self.pw.clear()
        self.pw2.clear()
        if not self.pw2.isHidden() and master != repeat:
            self._master_status.setText("the two passwords differ; nothing was created")
            return
        if not master:
            self._master_status.setText("type the master password first")
            return
        try:
            unlock(master)
        except SecretError as exc:
            self._master_status.setText(ui_log("registration", f"[STORE NOT OPEN: {exc}]"))
            return
        self._new_store = False  # it exists now; the card flips to unlock mode
        self._sync_master_card()
        self._master_status.setText(
            ui_log("registration", "[STORE OPEN: you can paste or generate the API wallet]")
        )
        if self._form is None:  # a fresh store (start over): the API card rebuilds
            picked = self._picked_profile()
            self._api_host.setVisible(True)
            if picked is not None:
                self._build_form(picked)

    def _start_over_clicked(self) -> None:
        """The retry path (explicit, destructive, named, GUARDED — operator
        request 2026-10-22): the live asset audit runs FIRST; a store that
        still holds balances/positions/open-order handles/chain wallets/
        triggers is refused BY NAME (nothing deleted). When the audit is
        CLEAR, Rule 10 confirmation lists the contents; then the store file
        and its lock are deleted and the card flips to create mode."""
        from ..reset import audit as reset_audit, report_text as reset_report_text  # lazy: no cycle

        try:
            cfg = load_config()
        except Exception as exc:  # noqa: BLE001  no endpoints/network yet
            self._master_status.setText(
                ui_log(
                    "registration",
                    f"[START OVER REFUSED: the endpoints/network are not loadable ({exc}) — "
                    "the audit cannot prove the store is empty; fix or write the endpoints file first]",
                )
            )
            return
        try:
            report = reset_audit(cfg)
        except Exception as exc:  # noqa: BLE001  a failed audit never deletes
            self._master_status.setText(
                ui_log("registration", f"[START OVER REFUSED: audit failed: {exc}]")
            )
            return
        if not report.clear:
            self._master_status.setText(
                ui_log("registration", f"[START OVER REFUSED — ASSETS PRESENT]\n{reset_report_text(report)}")
            )
            return
        if _confirm_delete_store(self) != QMessageBox.StandardButton.Yes:
            return
        try:
            lock()
            SECRETS_PATH.unlink(missing_ok=True)
        except (SecretError, OSError) as exc:
            self._master_status.setText(ui_log("registration", f"[NOT DELETED: {exc}]"))
            return
        self._new_store = True
        self._form = None  # any stored account went with the file
        self._api_host.setVisible(False)
        self._api_hint.setText(
            "Store deleted. Set a new master password above (Unlock / create the store) "
            "— the API card comes back with it."
        )
        self._sync_master_card()
        self._master_status.setText(
            ui_log("registration", "[STORE DELETED — choose a new master password]")
        )

    # -- the Strike API wallet (paste or generate; built per picked network) --
    def _build_api_card(self) -> QWidget:
        box, v = LAYOUT.card("Strike API wallet")
        self._api_hint = QLabel(
            "Pick a network above — then paste your API wallet or generate one here."
        )
        self._api_hint.setObjectName("muted")
        self._api_hint.setWordWrap(True)
        self._api_host = QWidget()
        v.addWidget(self._api_hint)
        v.addWidget(self._api_host)
        return box

    def _build_form(self, p: NetworkProfile) -> None:
        old = self._form
        if old is not None:
            old.deleteLater()
        self._form = StrikeConnectForm(self._api_host, fixed_network=p)
        self._api_hint.setVisible(False)  # the form's own intro takes over
        self._api_host.setVisible(True)
        host = self._api_host.layout()
        if host is None:
            host = LAYOUT.vbox(LAYOUT.GAP, LAYOUT.MARGIN)
            self._api_host.setLayout(host)
        host.addWidget(self._form)

    # -- Register: validate everything, in order, on one click ----------------
    def _register(self) -> None:
        if not self.config_path.is_file():
            try:
                write_private(self.config_path, fresh_config_json().encode("utf-8"))
            except (ValueError, OSError) as exc:
                self.status.setText(ui_log("registration", f"[NOT REGISTERED: {exc}]"))
                return
        p = self._picked_profile()
        if p is None:
            self.status.setText("pick a network first")
            return
        if not is_unlocked():
            master = self.pw.text()
            repeat = self.pw2.text()
            self.pw.clear()
            self.pw2.clear()
            if not self.pw2.isHidden() and master != repeat:
                self.status.setText("the two passwords differ; nothing was created")
                return
            if not master:
                self.status.setText("type the master password (or press Unlock / create the store)")
                return
            try:
                unlock(master)
            except SecretError as exc:
                self.status.setText(ui_log("registration", f"[NOT REGISTERED: {exc}]"))
                return
        form = self._form
        if form is None:
            self.status.setText("pick a network first")
            return
        try:
            if not has_secret(entry_for(form.environment)):
                pub = form.public_key.text().strip()
                priv = form.private_key.text().strip()
                form.private_key.clear()
                if not pub or not priv:
                    raise StrikeCredentialsError(
                        "paste or generate the Strike API wallet first (public + private)"
                    )
                save_account(
                    form.environment,
                    form.label.text().strip() or "struck-registration",
                    pub,
                    priv,
                    form.wallet.text().strip(),
                    replace=form.replace.isChecked(),
                )
        except (StrikeCredentialsError, SecretError) as exc:
            self.status.setText(ui_log("registration", f"[NOT REGISTERED: {exc}]"))
            return
        try:
            _mark_registered(p.name)
        except (ValueError, OSError) as exc:
            self.status.setText(ui_log("registration", f"[NOT REGISTERED: {exc}]"))
            return
        self.status.setText(
            ui_log(
                "registration",
                f"[REGISTERED on {p.name}: network saved, store open, API wallet stored]",
            )
        )
        self.accept()
