"""Key-free proof of the Strike API-wallet signing (skills/strike-auth/SKILL.md
:181-213 @30ba679) and of parsing the spec's own /v2/account and /v2/positions
example payloads (openapi/user-api.yaml). The key is a THROWAWAY test vector
(RFC 8032 section 7.1 TEST 1), never a real wallet."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from nacl.signing import VerifyKey

from struck.callhistory import RecordedHttp
from struck.strike.account import (
    Account,
    PositionsResponse,
    ServedStrikeAccount,
    StrikeAuthedClient,
)
from struck.strike.auth import (
    EMPTY_BODY_SHA256,
    StrikeCredentialsError,
    signed_headers,
    signing_message,
    wallet_from_hex,
)
from struck.strike.venue_auth import entry_for, save_account, wallet_from_secrets

from tests.conftest import unlock_test_store

GOLDEN = Path(__file__).parent / "golden"
SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
RFC_SIG_EMPTY = (
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065"
    "224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)
NONCE = "0192f0c1-7b8e-7c3a-9d61-2f1e4b5a6c7d"


def test_ed25519_is_the_rfc8032_vector() -> None:
    assert wallet_from_hex(PUB, SEED).sign(b"") == RFC_SIG_EMPTY


def test_empty_body_hash_is_the_spec_constant() -> None:
    assert hashlib.sha256(b"").hexdigest() == EMPTY_BODY_SHA256


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "POST",
            "/v2/order",
            b'{"symbol":"ETH-USD","side":"buy","type":"limit","size":"0.010","price":"2400.00"}',
        ),
        (
            "POST",
            "/v2/order/strategy",
            b'{"strategy_id":"s","symbol":"ETH-USD","side":"buy","type":"limit","size":"0.010"}',
        ),
        ("POST", "/v2/leverage", b'{"symbol":"ETH-USD","leverage":5}'),
        ("DELETE", "/v2/order/cancel", b'{"symbol":"ETH-USD","client_order_id":"x"}'),
        ("GET", "/v2/positions", None),
        ("GET", "/v2/account", None),
        ("GET", "/v2/symbols", None),
        ("GET", "/v2/openOrders?symbol=BTC-USD", None),
    ],
)
def test_signed_headers_match_spec(method: str, path: str, body: bytes | None) -> None:
    w = wallet_from_hex(PUB, SEED)
    h = signed_headers(w, method, path, body, 1775914565, NONCE)
    assert set(h) == {
        "X-API-Wallet-Public-Key",
        "X-API-Wallet-Signature",
        "X-API-Wallet-Timestamp",
        "X-API-Wallet-Nonce",
    }
    assert h["X-API-Wallet-Public-Key"] == PUB
    assert h["X-API-Wallet-Timestamp"] == "1775914565"
    assert h["X-API-Wallet-Nonce"] == NONCE
    sig = h["X-API-Wallet-Signature"]
    assert len(sig) == 128 and sig == sig.lower()
    body_hash = hashlib.sha256(body).hexdigest() if body is not None else EMPTY_BODY_SHA256
    expected = f"{method}:{path}:1775914565:{NONCE}:{body_hash}"
    assert signing_message(method, path, 1775914565, NONCE, body) == expected
    VerifyKey(bytes.fromhex(PUB)).verify(expected.encode("utf-8"), bytes.fromhex(sig))


def test_missing_account_fails_loudly_naming_the_entry_and_the_command() -> None:
    unlock_test_store()
    with pytest.raises(StrikeCredentialsError) as e:
        wallet_from_secrets()
    msg = str(e.value)
    assert entry_for("testnet") in msg and "python -m struck.strike.venue_auth" in msg


def test_env_wallet_vars_are_ignored_even_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    unlock_test_store()
    monkeypatch.setenv("STRUCK_STRIKE_API_WALLET_PUBLIC_KEY", PUB)
    monkeypatch.setenv("STRUCK_STRIKE_API_WALLET_PRIVATE_KEY", SEED)
    with pytest.raises(StrikeCredentialsError, match="not set"):
        wallet_from_secrets()
    save_account("testnet", "t", PUB, SEED)
    assert wallet_from_secrets().public_key_hex == PUB


def test_mismatched_key_fails_loudly() -> None:
    unlock_test_store()
    with pytest.raises(StrikeCredentialsError, match="does not belong"):
        save_account("testnet", "t", "00" * 32, SEED)


def test_private_key_never_in_repr() -> None:
    assert SEED not in repr(wallet_from_hex(PUB, SEED))


def test_spec_examples_parse_as_served() -> None:
    acc = Account.model_validate_json((GOLDEN / "strike_spec_account_example.json").read_bytes())
    assert (acc.wallet_balance, acc.unrealized_pnl, acc.maintenance_margin) == (
        "10000",
        "250.5",
        "500",
    )
    pos = PositionsResponse.model_validate_json(
        (GOLDEN / "strike_spec_positions_example.json").read_bytes()
    ).positions[0]
    assert (pos.upnl, pos.liquidation_price, pos.bankruptcy_price, pos.maintenance_margin) == (
        "-8.48714642",
        "156750.9643582120454",
        "157758.0419616276541",
        "30.69022933",
    )


def test_wire_request_is_signed_and_history_has_no_secrets(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    positions = (GOLDEN / "strike_spec_positions_example.json").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v2/time":
            return httpx.Response(200, content=b'{"serverTime":1775914566000}')
        return httpx.Response(200, content=positions)

    log = tmp_path / "call_history.jsonl"
    http = RecordedHttp(5, log)
    http._client = httpx.Client(transport=httpx.MockTransport(handler))
    client = StrikeAuthedClient(
        http, "https://api-v2-testnet.strikefinance.org", wallet_from_hex(PUB, SEED)
    )
    value = ServedStrikeAccount(client).position_field("BTC-USD", "liquidation_price")
    assert value == "156750.9643582120454"
    req = seen[-1]
    ts = int(req.headers["X-API-Wallet-Timestamp"])
    msg = f"GET:/v2/positions:{ts}:{req.headers['X-API-Wallet-Nonce']}:{EMPTY_BODY_SHA256}"
    VerifyKey(bytes.fromhex(PUB)).verify(
        msg.encode(), bytes.fromhex(req.headers["X-API-Wallet-Signature"])
    )
    assert ts <= 1775914566 - 1
    history = log.read_text(encoding="utf-8")
    assert req.headers["X-API-Wallet-Signature"] not in history and SEED not in history


@pytest.mark.parametrize(
    "path",
    ["v2/order", "//evil.example/v2/order", "https://evil.example/v2/order",
     "/v2/x://y", "\\\\evil\\v2", "/@evil.example/v2", ""],
)  # fmt: skip
def test_signed_request_path_must_be_on_the_pinned_host(path: str, tmp_path: Path) -> None:
    sent: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, content=b'{"serverTime":1775914566000}')

    http = RecordedHttp(5, tmp_path / "calls.jsonl")
    http._client = httpx.Client(transport=httpx.MockTransport(handler))
    client = StrikeAuthedClient(
        http, "https://api-v2-testnet.strikefinance.org", wallet_from_hex(PUB, SEED)
    )
    sent.clear()  # the /v2/time clock read at construction
    with pytest.raises(ValueError, match="refused"):
        client.request("GET", path)
    assert sent == []  # nothing left the process
