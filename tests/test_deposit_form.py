"""K03 deposit form, offscreen. Oracles are the ``DepositPlanProposal``
contract (``struck.strike.deposit``), the official strike-deposit skill (quote
body: ``blockchain``/``asset_symbol``/``asset_amount`` smallest-unit integer/
``mode`` with balance the omitted default; Cardano assets ``ADA, USDM, USDCx,
iUSD``; the depositing wallet's address is ``user_address``), the Rust
quantity rule (``money.rs``: ASCII digits only, no sign, no decimal, > 0) and
the deposit CLI's cardano gate (``asset_amount.isdigit()``, wallet address ==
``user_address``). Wallet rows are PUBLIC ``WalletInfo`` metadata (constructed
directly where a store is not needed); the locked/empty probes run the REAL
worker body against the conftest temp store. No network, no mocks, no
invented API JSON: the render and proposal paths run for real.
"""

from __future__ import annotations

import hashlib
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from struck.network import PROFILES
from struck.secrets import SecretError
from struck.strike.deposit import DepositPlanProposal, quote_body
from struck.ui.deposit_form import (
    MODE_COLLATERAL,
    DepositForm,
    _WalletPayload,
    _WalletRow,
)
from struck.wallets import (
    WalletInfo,
    derive,
    import_mnemonic,
    list_wallets,
)
from tests.conftest import unlock_test_store

PHRASE = " ".join(["abandon"] * 23 + ["art"])  # public BIP-39 test vector (test_wallets.py)
APP = QApplication.instance() or QApplication([])


def _addr(network: str) -> str:
    """A real derived address for constructed PUBLIC rows (public metadata)."""
    return derive(PHRASE, PROFILES[network], "standard")[1]


class _Capture:
    """The integrator-side callback: records the proposal, sends nothing."""

    def __init__(self) -> None:
        self.proposals: list[DepositPlanProposal] = []

    def __call__(self, plan: DepositPlanProposal) -> None:
        self.proposals.append(plan)


def _form() -> tuple[DepositForm, _Capture]:
    capture = _Capture()
    form = DepositForm(capture)
    APP.processEvents()
    return form, capture


def _show_public_rows(form: DepositForm, *rows: WalletInfo, network: str = "preprod") -> None:
    """The Worker 'done' slot, fed with PUBLIC WalletInfo rows (name, network,
    address only — the metadata ``list_wallets`` serves)."""
    payload = _WalletPayload(
        label=PROFILES[network].label,
        network=network,
        wallets=tuple(
            _WalletRow(name=r.name, network=r.network, address=r.address) for r in rows
        ),
        error="",
    )
    form._show(payload.model_dump_json())
    APP.processEvents()


# ---- the locked store: the exact fail-closed reason ----------------------
def test_locked_store_shows_the_exact_fail_closed_reason() -> None:
    """The REAL worker body against the locked temp store (conftest): the
    store's own SecretError text, verbatim; an empty DISABLED selector."""
    form, capture = _form()
    with pytest.raises(SecretError) as refused:
        list_wallets()  # the store's own refusal is the oracle
    payload = _WalletPayload.model_validate_json(form._fetch_wallets())
    assert payload.error == str(refused.value)
    assert payload.wallets == ()
    form._show(payload.model_dump_json())
    assert form.wallet.count() == 0 and not form.wallet.isEnabled()
    assert form.state.text() == f"wallet list unavailable: {refused.value}"
    assert "is locked" in form.state.text() and "unlock it in the Settings tab" in form.state.text()
    form.propose()  # nothing can be proposed from a locked store
    assert capture.proposals == []
    assert "REJECTED: no wallet on preprod is selected" in form.log.toPlainText()
    assert form.refresh.isEnabled()


def test_empty_unlocked_store_is_loud_not_silent() -> None:
    unlock_test_store()
    form, _ = _form()
    payload = _WalletPayload.model_validate_json(form._fetch_wallets())
    assert payload.error == "" and payload.wallets == ()
    form._show(payload.model_dump_json())
    assert form.wallet.count() == 0 and form.wallet.isEnabled()
    assert form.state.text().startswith("NO wallets imported")
    assert form.excluded_note.text() == ""


# ---- the selector: public rows, wrong network excluded loudly ------------
def test_selector_fills_from_public_rows_and_excludes_wrong_network() -> None:
    form, _ = _form()
    pp, mn = _addr("preprod"), _addr("mainnet")
    _show_public_rows(
        form,
        WalletInfo(name="pp", network="preprod", address=pp),
        WalletInfo(name="mn", network="mainnet", address=mn),
    )
    assert form.wallet.isEnabled()
    assert [form.wallet.itemText(i) for i in range(form.wallet.count())] == ["pp"]
    assert not form.wallet.isEditable()  # a selector, never a free-typed name
    assert form.address_note.text() == f"pays from {pp} (preprod)"
    assert form.excluded_note.text() == "EXCLUDED (not on preprod): mn (mainnet)"
    assert form.state.text() == "1 wallet(s) on preprod; 1 excluded as wrong network"


def test_only_wrong_network_wallets_is_a_loud_refusal() -> None:
    form, capture = _form()
    _show_public_rows(form, WalletInfo(name="mn", network="mainnet", address=_addr("mainnet")))
    assert form.wallet.count() == 0
    assert form.state.text().startswith("NO wallet on preprod")
    assert "mn (mainnet)" in form.excluded_note.text()
    form.amount.setText("1000000")
    form.rationale.setPlainText("r")
    form.propose()
    assert capture.proposals == []
    assert "REJECTED: no wallet on preprod is selected" in form.log.toPlainText()


# ---- the proposal: the strict model, as the CLI/pipeline consume it ------
def test_valid_input_emits_the_strict_proposal_the_cli_needs() -> None:
    unlock_test_store()
    import_mnemonic("pp", PHRASE, "preprod")
    (info,) = list_wallets()  # the real public row is the oracle
    form, capture = _form()
    form._show(form._fetch_wallets())  # the REAL worker body over the temp store
    APP.processEvents()
    assert form.wallet.count() == 1 and form.wallet.currentText() == "pp"
    form.asset.setCurrentText("ADA")
    form.amount.setText("5000000")
    form.rationale.setPlainText("fund the Strike testnet account from pp")
    form.propose()
    assert len(capture.proposals) == 1
    got = capture.proposals[0]
    expected = DepositPlanProposal(
        lane="deposit",
        intent="deposit ADA from wallet 'pp' to Strike testnet on Cardano preprod",
        blockchain="cardano",
        asset_symbol="ADA",
        asset_amount="5000000",
        user_address=info.address,  # the CLI refuses any other address
        mode=None,  # omitted = Strike's documented balance default
        rationale="fund the Strike testnet account from pp",
    )
    assert got == expected
    # the model contract: strict parse round-trip and the documented hash
    assert DepositPlanProposal.model_validate_json(got.model_dump_json()) == got
    assert got.plan_hash == hashlib.sha256(got.model_dump_json().encode()).hexdigest()
    # the deposit_cli gates: the cardano lane, integer lovelace text
    assert got.blockchain == "cardano" and got.asset_amount.isdigit()
    # the quote path consumes exactly these values (mode omitted = default)
    body: dict[str, str] = {
        "blockchain": "cardano",
        "asset_symbol": "ADA",
        "asset_amount": "5000000",
    }
    assert quote_body(got.blockchain, got.asset_symbol, got.asset_amount, got.mode) == json.dumps(
        body, separators=(",", ":")
    ).encode("utf-8")
    log = form.log.toPlainText()
    assert got.plan_hash in log and "NOTHING quoted, signed or sent" in log
    assert "5.000000 ADA" in log  # Rust format_lovelace display (ADA only)


def test_mode_and_asset_come_from_the_model_domain() -> None:
    form, capture = _form()
    _show_public_rows(form, WalletInfo(name="pp", network="preprod", address=_addr("preprod")))
    # No asset list is served by Strike (probe: /v2/deposit/* 404 on mainnet) —
    # the asset is a FREE-TYPED field validated only by Strike's quote.
    assert form.asset.isEditable()
    assert form.asset.count() == 0
    form.asset.setCurrentText("USDM")
    form.mode.setCurrentText(MODE_COLLATERAL)
    form.amount.setText("2000000")
    form.rationale.setPlainText("collateral custody")
    form.propose()
    got = capture.proposals[0]
    assert got.mode == "collateral" and got.asset_symbol == "USDM"
    body: dict[str, str] = {
        "blockchain": "cardano",
        "asset_symbol": "USDM",
        "asset_amount": "2000000",
        "mode": "collateral",
    }
    assert quote_body(got.blockchain, got.asset_symbol, got.asset_amount, got.mode) == json.dumps(
        body, separators=(",", ":")
    ).encode("utf-8")
    assert "ADA)" not in form.log.toPlainText()  # lovelace display is ADA-only


# ---- loud rejections: no repair, no silent retry --------------------------
@pytest.mark.parametrize("bad", ["1,5", "1.5", "-5", "0", "", "12 34", "٤"])
def test_non_integer_or_non_positive_amounts_are_rejected_loudly(bad: str) -> None:
    form, capture = _form()
    _show_public_rows(form, WalletInfo(name="pp", network="preprod", address=_addr("preprod")))
    form.amount.setText(bad)
    form.rationale.setPlainText("r")
    form.propose()
    assert capture.proposals == []
    log = form.log.toPlainText()
    assert "REJECTED" in log and "asset_amount" in log
    assert "integer" in log  # the Rust quantity rule speaks for itself


def test_missing_rationale_and_missing_wallet_are_rejected_loudly() -> None:
    form, capture = _form()
    form.amount.setText("5000000")
    form.rationale.setPlainText("r")
    form.propose()  # no wallet loaded at all
    assert "REJECTED: no wallet on preprod is selected" in form.log.toPlainText()
    _show_public_rows(form, WalletInfo(name="pp", network="preprod", address=_addr("preprod")))
    form.rationale.clear()
    form.propose()
    assert "REJECTED: rationale is required" in form.log.toPlainText()
    assert capture.proposals == []


def test_worker_failure_names_the_reason(tmp_path: Path) -> None:
    form, _ = _form()
    form._failed("ConnectError: refused")
    assert form.state.text().startswith("FAILED: ConnectError")
    assert form.refresh.isEnabled()
    assert "FAILED" in (tmp_path / "ui_events.log").read_text(encoding="utf-8")
