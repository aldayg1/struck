"""Strike API-wallet accounts (struck.strike.venue_auth) and the masked connect
dialog (struck.ui.venue_auth_dialog). Keys: RFC 8032 section 7.1 TEST 1 (a
public test vector) or generated here; Strike is a MOCK transport only."""

from __future__ import annotations

import io
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
import pytest
from PySide6.QtWidgets import QApplication, QLineEdit

from struck import secrets as s
from struck.callhistory import CallFailed, RecordedHttp
from struck.config import load_config
from struck.network import NetworkProfile
from struck.strike import venue_auth as va
from struck.strike.auth import ApiWallet, StrikeCredentialsError, wallet_from_hex
from struck.ui import venue_auth_dialog as dlg
from tests.conftest import put_secret, unlock_test_store

SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
_APP = QApplication.instance() or QApplication([])


class _Venue:
    """Mock Strike: /v2/account answers 200 only for the registered public key."""

    def __init__(self, registered: str) -> None:
        self.registered = registered
        self.seen: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        if req.url.path == "/v2/time":
            return httpx.Response(
                200, content=f'{{"serverTime":{time.time_ns() // 1_000_000}}}'.encode()
            )
        if req.url.path == "/v2/account":
            key_header = cast("str | None", req.headers.get("X-API-Wallet-Public-Key"))
            if key_header == self.registered:
                return httpx.Response(200, content=b'{"user_id":"u-1"}')
            return httpx.Response(401, content=b'{"error":"unauthorized"}')
        return httpx.Response(404)


def _http(tmp_path: Path, venue: _Venue) -> RecordedHttp:
    http = RecordedHttp(5, tmp_path / "calls.jsonl")
    http._client = httpx.Client(transport=httpx.MockTransport(venue))
    return http


# -- key pair validation -------------------------------------------------------
def test_validate_keypair_accepts_seed_or_full_key_and_returns_the_seed() -> None:
    assert va.validate_keypair(PUB, SEED) == (PUB, SEED)
    assert va.validate_keypair(PUB.upper(), (SEED + PUB).upper()) == (PUB, SEED)


@pytest.mark.parametrize(
    ("pub", "priv", "why"),
    [
        ("ab" * 31, SEED, "public key must be 64 hex"),
        (PUB, "ab" * 40, "private key must be 64 or 128 hex"),
        ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA", SEED, "SSH-format"),
        (PUB, "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----", "SSH-format"),
        ("00" * 32, SEED, "does not belong"),
    ],
)
def test_validate_keypair_refuses(pub: str, priv: str, why: str) -> None:
    with pytest.raises(StrikeCredentialsError, match=why):
        va.validate_keypair(pub, priv)


def test_environment_follows_the_selected_network() -> None:
    from struck.network import profile

    assert va.environment_of(profile("preprod")) == "testnet"
    assert va.environment_of(profile("mainnet")) == "mainnet"
    assert profile("preprod").strike_api_keys_url == "https://testnet.strikefinance.org/api-keys"
    assert profile("mainnet").strike_api_keys_url == "https://app.strikefinance.org/api-keys"


# -- store -------------------------------------------------------------------
def test_save_is_one_record_per_environment_and_refuses_overwrite() -> None:
    unlock_test_store()
    a = va.save_account("testnet", "t1", PUB, SEED, "your-wallet")
    assert "**" in repr(a.private_key_hex) and SEED not in repr(a)
    record = cast("dict[str, object]", json.loads(s.get_secret("strike_api_testnet")))
    assert record["public_key_hex"] == PUB and record["wallet"] == "your-wallet"
    with pytest.raises(StrikeCredentialsError, match="already stored"):
        va.save_account("testnet", "t2", PUB, SEED)
    other = va.generate_account("testnet", "t3", replace=True)
    assert va.load_account("testnet").public_key_hex == other.public_key_hex != PUB
    assert not s.has_secret("strike_api_mainnet")  # environments never mix
    with pytest.raises(StrikeCredentialsError, match="strike_api_mainnet"):
        va.wallet_for("mainnet")


def test_generated_key_signs_for_its_own_public_key() -> None:
    unlock_test_store()
    a = va.generate_account("testnet", "gen")
    assert re.fullmatch(r"[0-9a-f]{64}", a.public_key_hex)
    w = va.wallet_for("testnet")
    assert w.public_key_hex == a.public_key_hex


def test_legacy_entries_are_named_never_used_then_imported() -> None:
    put_secret(s.STRIKE_PUBLIC, PUB)
    put_secret(s.STRIKE_PRIVATE, SEED)
    with pytest.raises(StrikeCredentialsError, match="import-legacy"):
        va.wallet_for("testnet")  # no fallback onto the network-less pair
    va.import_legacy("testnet", "old")
    assert va.wallet_for("testnet").public_key_hex == PUB
    assert not s.has_secret(s.STRIKE_PUBLIC) and not s.has_secret(s.STRIKE_PRIVATE)


# -- verify (signed GET /v2/account) -------------------------------------------
def test_verify_200_only_for_the_registered_key_and_no_secret_recorded(tmp_path: Path) -> None:
    profile = load_config().profile
    venue = _Venue(PUB)
    http = _http(tmp_path, venue)
    assert va.verify(wallet_from_hex(PUB, SEED), profile, http) == '{"user_id":"u-1"}'
    acc = [r for r in venue.seen if r.url.path == "/v2/account"][0]
    assert str(acc.url).startswith(profile.strike_api_url)
    assert acc.method == "GET" and re.fullmatch(
        r"[0-9a-f]{128}", acc.headers["X-API-Wallet-Signature"]
    )
    import nacl.signing

    other = nacl.signing.SigningKey.generate()
    with pytest.raises(CallFailed, match="HTTP 401"):
        va.verify(
            wallet_from_hex(other.verify_key.encode().hex(), other.encode().hex()), profile, http
        )
    log = (tmp_path / "calls.jsonl").read_text()
    assert SEED not in log and other.encode().hex() not in log
    assert acc.headers["X-API-Wallet-Signature"] not in log and "X-API-Wallet" not in log


# -- CLI -----------------------------------------------------------------------
def test_cli_generate_prints_only_the_public_key(capsys: pytest.CaptureFixture[str]) -> None:
    unlock_test_store()
    assert va.main(["--network", "preprod", "generate", "your-wallet", "--wallet", "w"]) == 0
    out = capsys.readouterr()
    lines = out.out.strip().splitlines()
    assert "https://testnet.strikefinance.org/api-keys" in lines[1]
    pub = lines[-1]
    assert re.fullmatch(r"[0-9a-f]{64}", pub)
    seed = va.load_account("testnet").private_key_hex.get_secret_value()
    assert seed not in out.out + out.err
    assert va.main(["--network", "preprod", "generate", "again"]) == 3  # no silent overwrite
    assert "already stored" in capsys.readouterr().err
    assert va.load_account("testnet").public_key_hex == pub
    assert va.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert pub in listed and seed not in listed


def test_cli_import_reads_the_private_key_from_stdin_not_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unlock_test_store()
    monkeypatch.setattr("sys.stdin", io.StringIO(SEED + "\n"))
    assert va.main(["--network", "mainnet", "import", "m", PUB]) == 0
    out = capsys.readouterr()
    assert SEED not in out.out + out.err and va.wallet_for("mainnet").public_key_hex == PUB
    assert not s.has_secret("strike_api_testnet")


# -- dialog (thin wrapper over StrikeConnectForm; the widgets live on d.form) ------
def _settle(d: dlg.StrikeConnectDialog) -> None:
    assert d.form._thread is not None
    d.form._thread.wait(5000)
    for _ in range(20):
        _APP.processEvents()


def _fake_verifier(registered: str) -> Callable[[ApiWallet, NetworkProfile], str]:
    def verifier(wallet: ApiWallet, profile: NetworkProfile) -> str:
        if wallet.public_key_hex != registered:
            raise CallFailed(
                f"strike-testnet-auth GET {profile.strike_api_url}/v2/account -> HTTP 401: {{}}"
            )
        return '{"user_id":"u-1"}'

    return verifier


def test_dialog_private_field_is_masked_and_cleared_after_save() -> None:
    unlock_test_store()
    d = dlg.StrikeConnectDialog()
    f = d.form
    assert f.environment == "testnet" and f.open_btn.toolTip().endswith(
        "testnet.strikefinance.org/api-keys"
    )
    assert f.private_key.echoMode() == QLineEdit.EchoMode.Password
    f.label.setText("your-wallet")
    f.public_key.setText(PUB)
    f.private_key.setText(SEED)
    f.save_clicked()
    assert f.status.text().startswith("SAVED:") and f.private_key.text() == ""
    assert PUB in f.stored.text() and SEED not in f.stored.text() + f.status.text()
    f.public_key.setText(PUB)
    f.private_key.setText(SEED)
    f.save_clicked()  # replace not ticked
    assert "already stored" in f.status.text() and f.private_key.text() == ""


def test_dialog_generates_a_pair_in_app_no_terminal() -> None:
    unlock_test_store()
    d = dlg.StrikeConnectDialog()
    f = d.form
    saved: list[bool] = []
    f.account_saved.connect(lambda: saved.append(True))
    f.generate_btn.click()  # no label yet: refused, nothing stored
    assert f.status.text().startswith("NOT GENERATED") and not saved
    f.label.setText("generated-here")
    f.generate_btn.click()
    assert saved and f.status.text().startswith("GENERATED 'generated-here'")
    shown = f.account_view.toPlainText()
    assert len(shown) == 64 and shown in f.stored.text()  # the PUBLIC key, for the venue page
    assert SEED not in f.status.text() + shown and s.has_secret("strike_api_testnet")


def test_dialog_verify_entered_pair_200_and_401() -> None:
    unlock_test_store()
    d = dlg.StrikeConnectDialog(verifier=_fake_verifier(PUB))
    f = d.form
    f.public_key.setText(PUB)
    f.private_key.setText(SEED)
    f.verify_clicked()
    _settle(d)
    assert f.status.text().startswith("VERIFIED")
    assert '"user_id"' in f.account_view.toPlainText()
    import nacl.signing

    k = nacl.signing.SigningKey.generate()
    f.public_key.setText(k.verify_key.encode().hex())
    f.private_key.setText(k.encode().hex())
    f.verify_clicked()
    _settle(d)
    assert dlg.UNKNOWN_KEY in f.status.text() and not s.has_secret("strike_api_testnet")


def test_dialog_verify_with_empty_fields_uses_the_stored_account() -> None:
    unlock_test_store()
    d = dlg.StrikeConnectDialog(verifier=_fake_verifier(PUB))
    f = d.form
    f.verify_clicked()
    assert "strike_api_testnet" in f.status.text()  # nothing stored -> named, no request
    va.save_account("testnet", "t", PUB, SEED)
    f.verify_clicked()
    _settle(d)
    assert f.status.text().startswith("VERIFIED")
