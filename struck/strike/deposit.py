"""Strike deposit quote and unsigned build (strike-deposit skill).

``POST /v2/deposit/quote`` prices the deposit. ``POST /v2/deposit/build-tx``
returns the unsigned transaction the human's own wallet signs. The API
wallet does not move funds.

``confirm_body`` is the ``POST /v2/deposit`` body from the strike-deposit
skill (``request_id`` plus ``tx_hash``, at least 10 characters).
``DepositClient.confirm`` posts that body. ``deposit_cli`` does not call it.
Confirm runs only after the human has submitted the signed transaction and
has a tx hash.
"""

from __future__ import annotations

import json
from typing import Literal

import struck_py
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from ..callhistory import Served, sha256_hex
from ..chain.models import ChainUtxo
from ..plan.models import Explanation
from .account import StrikeAuthedClient

Chain = Literal["ethereum", "solana", "cardano"]
DepositMode = Literal["balance", "collateral"]
_HEX = set("0123456789abcdefABCDEF")


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class EvmTxParams(_Strict):
    """Ethereum ``unsigned_tx`` / ``approval_tx`` (``format: evm_tx_params``)."""

    from_address: StrictStr = Field(alias="from")
    to: StrictStr
    data: StrictStr
    value: StrictStr
    gas: StrictStr


class DepositQuoteBody(_Strict):
    account_id: StrictStr
    request_id: StrictStr
    blockchain: Chain
    asset_symbol: StrictStr
    asset_amount: StrictStr
    usd_value: StrictStr
    exchange_rate: StrictStr
    confirmations: StrictInt
    timestamp: StrictInt
    expiration_at: StrictInt


class DepositQuote(_Strict):
    request_id: StrictStr
    quote: DepositQuoteBody
    signature: StrictStr
    deposit_address: StrictStr
    confirmations_required: StrictInt


class BuiltDeposit(_Strict):
    blockchain: Chain
    unsigned_tx: StrictStr | EvmTxParams
    format: Literal["cardano_cbor", "solana_base64", "evm_tx_params"]
    expires_at: StrictInt | None = None
    approval_tx: EvmTxParams | None = None

    @model_validator(mode="after")
    def _format_matches(self) -> "BuiltDeposit":
        if self.format == "evm_tx_params":
            if not isinstance(self.unsigned_tx, EvmTxParams):
                raise ValueError("evm_tx_params: unsigned_tx must be the tx params object")
        elif not isinstance(self.unsigned_tx, str):
            raise ValueError(f"{self.format}: unsigned_tx must be a string")
        if self.approval_tx is not None and self.format != "evm_tx_params":
            raise ValueError("approval_tx is only served for evm_tx_params")
        return self


def _positive_amount(asset_amount: str) -> None:
    """Smallest-unit integer, greater than zero. Rust parses it. No arithmetic here."""
    if not struck_py.compare_quantities(asset_amount, "gt", "0"):
        raise ValueError("asset_amount must be a positive integer in the smallest unit")


def quote_body(
    blockchain: Chain, asset_symbol: str, asset_amount: str, mode: DepositMode | None
) -> bytes:
    """Exact ``POST /v2/deposit/quote`` body. Absent ``mode`` is omitted."""
    _positive_amount(asset_amount)
    if not asset_symbol:
        raise ValueError("asset_symbol is required")
    body: dict[str, str] = {
        "blockchain": blockchain,
        "asset_symbol": asset_symbol,
        "asset_amount": asset_amount,
    }
    if mode is not None:
        body["mode"] = mode
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def _utxo_hex(utxo: str) -> None:
    if len(utxo) < 2 or len(utxo) % 2 != 0 or any(c not in _HEX for c in utxo):
        raise ValueError("Cardano utxos must be even-length hex from the wallet")


def cip30_utxos(utxos: tuple[ChainUtxo, ...]) -> tuple[str, ...]:
    """The CIP-30 ``getUtxos()`` hex strings Strike's build-tx expects, built
    with stock pycardano (``UTxO.to_cbor()``; csl ``TransactionUnspentOutput``
    shape, round-trips via ``UTxO.from_cbor``). No local CBOR, no arithmetic:
    a duplicate (policy, name) inside one utxo is refused, never summed."""

    from pycardano import (  # stock serialization; imported here to keep module import light
        Address,
        Asset,
        AssetName,
        MultiAsset,
        TransactionInput,
        TransactionOutput,
        UTxO,
        Value,
    )
    from pycardano.hash import PolicyId, TransactionId

    out: list[str] = []
    for u in utxos:
        pairs: dict[bytes, dict[bytes, int]] = {}
        for a in u.assets:
            policy, name = bytes.fromhex(a.policy_hex), bytes.fromhex(a.name_hex)
            if name in pairs.setdefault(policy, {}):
                raise ValueError(
                    f"utxo {u.tx_hash}#{u.index}: duplicate asset (policy, name); refusing"
                )
            pairs[policy][name] = a.qty
        multi_asset = MultiAsset(
            {PolicyId(policy): Asset({AssetName(name): qty}) for policy, names in pairs.items() for name, qty in names.items()}
        )
        spent = UTxO(
            TransactionInput(TransactionId(bytes.fromhex(u.tx_hash)), u.index),
            TransactionOutput(Address.from_primitive(u.address), Value(u.lovelace, multi_asset)),
        )
        out.append(spent.to_cbor().hex())
    return tuple(out)


def confirm_body(request_id: str, tx_hash: str) -> bytes:
    """Exact ``POST /v2/deposit`` body. Not sent by this module."""
    if not request_id:
        raise ValueError("request_id is required")
    if len(tx_hash) < 10:
        raise ValueError("tx_hash must be at least 10 characters")
    body: dict[str, str] = {"request_id": request_id, "tx_hash": tx_hash}
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def build_body(request_id: str, user_address: str, utxos: tuple[str, ...] | None) -> bytes:
    """Exact ``POST /v2/deposit/build-tx`` body. ``utxos`` is Cardano-only."""
    if not request_id or not user_address:
        raise ValueError("request_id and user_address are required")
    body: dict[str, object] = {"request_id": request_id, "user_address": user_address}
    if utxos is not None:
        if len(utxos) == 0:
            raise ValueError("Cardano build-tx needs at least one utxo")
        for utxo in utxos:
            _utxo_hex(utxo)
        body["utxos"] = list(utxos)
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


class DepositPlanProposal(_Strict):
    """What a fired ``strike_deposit`` trigger writes. No quote has been asked."""

    lane: Literal["deposit"]
    intent: StrictStr
    blockchain: Chain
    asset_symbol: StrictStr
    asset_amount: StrictStr
    user_address: StrictStr
    mode: DepositMode | None = None
    rationale: StrictStr

    @property
    def plan_hash(self) -> str:
        """Hash of this proposal's canonical JSON. The human quotes this hash."""
        return sha256_hex(self.model_dump_json().encode("utf-8"))


class DepositConfirm(_Strict):
    """``POST /v2/deposit`` response, as the strike-deposit skill serves it."""

    request_id: StrictStr
    status: StrictStr
    tx_hash: StrictStr
    message: StrictStr


def review_cardano_deposit(
    cbor_hex: str,
    user_address: str,
    deposit_lovelace: int,
    max_fee_lovelace: int,
    valid_until_slot: int,
    inputs_json: str,
    ttl_wallclock: str,
    network_json: str,
) -> Explanation:
    """The three facts a human must see before any signature: plain lines,
    and the congruence report. The CBOR and the Pallas JSON are the other two,
    decoded by the caller from ``cbor_hex``. This does not quote or sign.
    """
    return Explanation.model_validate_json(
        struck_py.deposit_explain(
            cbor_hex,
            user_address,
            deposit_lovelace,
            max_fee_lovelace,
            valid_until_slot,
            inputs_json,
            ttl_wallclock,
            network_json,
        )
    )


class DepositClient:
    """Quote, build-tx, and confirm. Does not sign and does not submit the chain tx."""

    def __init__(self, authed: StrikeAuthedClient) -> None:
        self._authed = authed

    def quote(
        self,
        blockchain: Chain,
        asset_symbol: str,
        asset_amount: str,
        mode: DepositMode | None = None,
    ) -> tuple[Served, DepositQuote]:
        served = self._authed.request(
            "POST", "/v2/deposit/quote", quote_body(blockchain, asset_symbol, asset_amount, mode)
        )
        return served, DepositQuote.model_validate_json(served.content)

    def build_tx(
        self,
        request_id: str,
        user_address: str,
        blockchain: Chain,
        utxos: tuple[str, ...] | None = None,
    ) -> tuple[Served, BuiltDeposit]:
        if blockchain == "cardano" and not utxos:
            raise ValueError("Cardano build-tx needs the wallet's utxos (CIP-30 hex)")
        if blockchain != "cardano" and utxos is not None:
            raise ValueError("utxos are only sent for Cardano")
        served = self._authed.request(
            "POST", "/v2/deposit/build-tx", build_body(request_id, user_address, utxos)
        )
        built = BuiltDeposit.model_validate_json(served.content)
        if built.blockchain != blockchain:
            raise ValueError(
                f"build-tx returned {built.blockchain}, the quote chain was {blockchain}"
            )
        return served, built

    def quote_and_build(
        self, proposal: DepositPlanProposal, utxos: tuple[str, ...] | None
    ) -> tuple[Served, DepositQuote, Served, BuiltDeposit]:
        """``POST /v2/deposit/quote`` then ``POST /v2/deposit/build-tx``.

        A quote starts a deposit. Call this only after the human has typed
        ``quote <plan_hash>`` on a real terminal.
        """
        quote_served, quote = self.quote(
            proposal.blockchain, proposal.asset_symbol, proposal.asset_amount, proposal.mode
        )
        build_served, built = self.build_tx(
            quote.request_id, proposal.user_address, proposal.blockchain, utxos
        )
        return quote_served, quote, build_served, built

    def confirm(self, request_id: str, tx_hash: str) -> tuple[Served, DepositConfirm]:
        """``POST /v2/deposit``. The tx must already be submitted by the human's wallet."""
        served = self._authed.request("POST", "/v2/deposit", confirm_body(request_id, tx_hash))
        return served, DepositConfirm.model_validate_json(served.content)
