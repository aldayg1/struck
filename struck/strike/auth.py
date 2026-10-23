"""Strike API-wallet request signing, exactly as Strike specifies it.

Spec: strike-finance-skills @30ba679 ``skills/strike-auth/SKILL.md``
"API Wallet Request Signing" (:181-213) and ``openapi/trade-api.yaml``
``components.securitySchemes.ApiWalletAuth``:

* headers ``X-API-Wallet-Public-Key`` (64 lowercase hex, as registered),
  ``X-API-Wallet-Signature`` (hex Ed25519, 128 chars),
  ``X-API-Wallet-Timestamp`` (Unix SECONDS), ``X-API-Wallet-Nonce`` (16-128
  chars, single use; a UUID works);
* message ``{METHOD}:{PATH}:{TIMESTAMP}:{NONCE}:{BODY_HASH}``, PATH = path
  plus ``?`` + raw query when present, BODY_HASH = lowercase hex SHA-256 of
  the exact body bytes (SHA-256 of empty input when there is no body);
* Ed25519 over the UTF-8 message (stock PyNaCl, the libsodium binding).

The key is the operator's OWN Strike API wallet for the selected network's
Strike environment, read only from Struck's encrypted secret store
(``struck.strike.venue_auth``: entry ``strike_api_<testnet|mainnet>``); no
environment variable is read. It is never logged, printed, written to receipts
or call history. Missing or inconsistent key = ``StrikeCredentialsError``
naming the entry and the exact ``python -m struck.strike.venue_auth`` command;
there is no fallback.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field

from nacl.signing import SigningKey


EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class StrikeCredentialsError(RuntimeError):
    """The Strike API wallet is not configured (or is inconsistent)."""


@dataclass(frozen=True)
class ApiWallet:
    public_key_hex: str
    _signing_key: SigningKey = field(repr=False)

    def __repr__(self) -> str:
        return f"ApiWallet(public_key_hex={self.public_key_hex!r}, private_key=<redacted>)"

    def sign(self, message: bytes) -> str:
        return self._signing_key.sign(message).signature.hex()


def wallet_from_hex(public_key_hex: str, private_key_hex: str) -> ApiWallet:
    if not _HEX64.fullmatch(public_key_hex):
        raise StrikeCredentialsError(
            "the Strike API wallet public key must be the 64 lowercase hex chars of the raw Ed25519 public key, exactly as registered with Strike"
        )
    if not _HEX64.fullmatch(private_key_hex.lower()):
        raise StrikeCredentialsError(
            "the Strike API wallet private key must be the 64 hex chars of the 32-byte Ed25519 private key (seed)"
        )
    key = SigningKey(bytes.fromhex(private_key_hex))
    derived = key.verify_key.encode().hex()
    if derived != public_key_hex:
        raise StrikeCredentialsError(
            "the Strike API wallet private key does not belong to the public key (derived public key differs)"
        )
    return ApiWallet(public_key_hex, key)


def body_sha256(body: bytes | None) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def signing_message(
    method: str, path_with_query: str, timestamp_s: int, nonce: str, body: bytes | None
) -> str:
    return f"{method.upper()}:{path_with_query}:{timestamp_s}:{nonce}:{body_sha256(body)}"


def new_nonce() -> str:
    return str(uuid.uuid4())


def signed_headers(
    wallet: ApiWallet,
    method: str,
    path_with_query: str,
    body: bytes | None,
    timestamp_s: int,
    nonce: str,
) -> dict[str, str]:
    if not 16 <= len(nonce) <= 128:
        raise ValueError("X-API-Wallet-Nonce must be 16-128 chars")
    message = signing_message(method, path_with_query, timestamp_s, nonce, body)
    return {
        "X-API-Wallet-Public-Key": wallet.public_key_hex,
        "X-API-Wallet-Signature": wallet.sign(message.encode("utf-8")),
        "X-API-Wallet-Timestamp": str(timestamp_s),
        "X-API-Wallet-Nonce": nonce,
    }


def logon_params(wallet: ApiWallet, timestamp_ms: int) -> dict[str, object]:
    """User-stream ``session.logon`` params.

    The signed ASCII is ``session.logon:{timestamp_ms}:{apiKey}``. Strike
    TESTNET answered that body with status 200. The skill line
    ``apiKey=<PUBLIC>&timestamp=<ms>`` drew no logon answer on that socket.
    """
    message = f"session.logon:{timestamp_ms}:{wallet.public_key_hex}"
    return {
        "apiKey": wallet.public_key_hex,
        "signature": wallet.sign(message.encode("ascii")),
        "timestamp": timestamp_ms,
    }
