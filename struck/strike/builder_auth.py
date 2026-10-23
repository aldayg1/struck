"""Strike builder connect: bind a stored Struck Cardano wallet to a Strike
account (strike-auth skill "Builder Connect Flow").

Two PUBLIC calls (no API-wallet headers, no chain writes):

* ``POST /auth/builder/request-signature`` — the served challenge
  (``nonce`` + ``message_to_sign``, verbatim; the challenge lives 5 minutes
  and is single-use).
* ``POST /auth/builder/verify-signature`` — Strike verifies the wallet
  signature, creates the Strike account keyed by that wallet if it has none,
  revokes this account's earlier API wallets for the same builder code,
  registers the FRESH public key as a non-expiring API wallet and approves
  the builder with ``fee_share_bps`` as the user's max.

The Cardano wallet signature is CIP-30 ``signData`` (CIP-8 COSE_Sign1, the
same structure a browser wallet produces; Strike's reference client sends
``"<coseSign1 hex>:<coseKey hex>"``). The protected header carries the EdDSA
algorithm and the raw address bytes; the payload is the served message
verbatim; the payment key signs the RFC 9052 Sig_structure. ``cbor2`` is the
stock CBOR codec (already a pinned dependency); pycardano supplies the
address parts. No secret leaves this module: the COSE key holds only the
PUBLIC verification key.

Nothing here moves money. The wallet signature is a consent the human types
at a real terminal (``struck.strike.venue_auth connect``).
"""

from __future__ import annotations

import json
from typing import Literal

import cbor2
from pycardano import Address
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from ..callhistory import RecordedHttp, Served
from ..wallets import WalletSigner

BuilderChain = Literal["ethereum", "solana", "cardano"]
FEE_SHARE_BPS = 0  # the user's approved max builder fee: zero, never a paid lane


class _Served(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)


class BuilderChallenge(_Served):
    """``POST /auth/builder/request-signature`` as served. Sign verbatim."""

    nonce: StrictStr
    message_to_sign: StrictStr
    message: StrictStr
    referral_code: StrictStr | None = None


class BuilderReferral(_Served):
    status: StrictStr
    code: StrictStr | None = None


class BuilderConnectResult(_Served):
    """``POST /auth/builder/verify-signature`` as served."""

    account_id: StrictStr
    nickname: StrictStr
    avatar_url: StrictStr
    builder_code: StrictStr
    fee_share_bps: StrictInt
    api_wallet_id: StrictInt
    api_wallet_public_key: StrictStr
    api_wallet_expired_at: StrictStr
    referral: BuilderReferral


def request_body(
    address: str, chain: BuilderChain, code: str, fee_share_bps: int, public_key_hex: str
) -> bytes:
    """Exact ``POST /auth/builder/request-signature`` body (strict server-side:
    unknown fields are rejected)."""
    if not address or not code or not public_key_hex:
        raise ValueError("address, code and public_key are required")
    if not 0 <= fee_share_bps <= 100:
        raise ValueError("fee_share_bps must be 0-100")
    body: dict[str, object] = {
        "address": address,
        "chain": chain,
        "code": code,
        "fee_share_bps": fee_share_bps,
        "public_key": public_key_hex,
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def verify_body(nonce: str, wallet_signature: str) -> bytes:
    """Exact ``POST /auth/builder/verify-signature`` body."""
    if not nonce or not wallet_signature:
        raise ValueError("nonce and wallet_signature are required")
    body: dict[str, str] = {"nonce": nonce, "wallet_signature": wallet_signature}
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def address_bytes(address_bech32: str) -> bytes:
    """The raw on-chain bytes of a Shelley bech32 address (header + payment +
    staking; ``Address.__bytes__``), as the CIP-8 ``address`` protected-header
    value."""
    parsed = Address.from_primitive(address_bech32)
    if parsed.is_byron:
        raise ValueError(
            f"{address_bech32} is a Byron address; CIP-30 signData needs a Shelley "
            "bech32 address (base or enterprise)"
        )
    return bytes(parsed)


def cip30_sign_data(signer: WalletSigner, message: str) -> str:
    """CIP-30 ``signData(address, hex(message))`` as Strike expects it:
    ``"<coseSign1 hex>:<coseKey hex>"``.

    The protected header is ``{alg: EdDSA, address: <raw address bytes>}``, the
    payload is the message verbatim, and the stored wallet's payment key signs
    the RFC 9052 Sig_structure ``["Signature1", protected, b"", payload]``.
    """
    header: dict[int | str, int | bytes] = {1: -8, "address": address_bytes(signer.address)}
    protected = cbor2.dumps(header)
    payload = message.encode("utf-8")
    sig_structure: list[str | bytes] = ["Signature1", protected, b"", payload]
    vkey, signature = signer.sign(cbor2.dumps(sig_structure))
    cose_sign1 = cbor2.dumps([protected, {}, payload, signature])
    cose_key = cbor2.dumps({1: 1, 3: -8, -1: 6, -2: vkey})  # OKP / EdDSA / Ed25519
    return f"{cose_sign1.hex()}:{cose_key.hex()}"


class BuilderConnectClient:
    """The two public builder-connect calls. Signs nothing with an API wallet."""

    def __init__(self, http: RecordedHttp, base_url: str) -> None:
        self._http = http
        self._base = base_url

    def request_challenge(
        self,
        address: str,
        chain: BuilderChain,
        code: str,
        fee_share_bps: int,
        public_key_hex: str,
    ) -> tuple[Served, BuilderChallenge]:
        served = self._http.request(
            "strike-builder",
            "POST",
            self._base + "/auth/builder/request-signature",
            json_body=request_body(address, chain, code, fee_share_bps, public_key_hex),
        )
        return served, BuilderChallenge.model_validate_json(served.content)

    def verify(self, nonce: str, wallet_signature: str) -> tuple[Served, BuilderConnectResult]:
        served = self._http.request(
            "strike-builder",
            "POST",
            self._base + "/auth/builder/verify-signature",
            json_body=verify_body(nonce, wallet_signature),
        )
        return served, BuilderConnectResult.model_validate_json(served.content)
