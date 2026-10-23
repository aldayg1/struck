"""Pydantic boundary models for the Struck pipeline (spec §3.2).

Every model is strict, frozen and forbids unknown fields: an LLM or a human
that produces a wrong shape is REJECTED loudly (``ValidationError``), never
repaired. No floats: lovelace and token quantities are ``StrictInt``.
Milestone 1 implements the on-chain ``send`` leg only.
"""

from __future__ import annotations

from ..network import NetworkName

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from ..chain.models import Asset


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class SendOutput(_Strict):
    address: StrictStr
    lovelace: StrictInt
    assets: tuple[Asset, ...]


class OnchainSendLeg(_Strict):
    kind: Literal["send"]
    from_address: StrictStr
    outputs: tuple[SendOutput, ...] = Field(min_length=1)
    max_fee_lovelace: StrictInt
    validity_minutes: StrictInt = Field(gt=0)


class PlanProposal(_Strict):
    """What the planner (LLM or fixed input) may produce, and nothing else."""

    lane: Literal["onchain"]
    intent: StrictStr
    legs: tuple[OnchainSendLeg]
    rationale: StrictStr


class ApprovedOnchainPlan(_Strict):
    """The exact plan the human approves; serializes to Rust ``OnchainPlan``."""

    network: NetworkName  # the SELECTED network; Rust re-checks it against the selection
    from_address: StrictStr
    outputs: tuple[SendOutput, ...]
    max_fee_lovelace: StrictInt
    valid_until_slot: StrictInt

    @property
    def canonical_json(self) -> str:
        return self.model_dump_json()

    @property
    def plan_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


class CheckItem(_Strict):
    id: StrictStr
    expected: StrictStr
    actual: StrictStr
    ok: StrictBool


class CheckReport(_Strict):
    ok: StrictBool
    checks: tuple[CheckItem, ...]


class ApprovalRequest(_Strict):
    plan_hash: StrictStr
    plan: ApprovedOnchainPlan
    plan_check: CheckReport
    summary: tuple[StrictStr, ...]


class PlanDecision(_Strict):
    decision: Literal["approve", "reject"]
    plan_hash: StrictStr
    approver: StrictStr
    ts_ms: StrictInt


class UnsignedTx(_Strict):
    source: Literal["cardano-cli", "pycardano-koios"]
    cbor_hex: StrictStr
    tx_id: StrictStr
    build_stdout: StrictStr


class Explanation(_Strict):
    tx_id: StrictStr
    lines: tuple[StrictStr, ...]
    spends_lovelace: StrictInt
    pays_lovelace: StrictInt
    change_lovelace: StrictInt
    fee_lovelace: StrictInt
    congruence: CheckReport


class SignedSummary(_Strict):
    """The SIGNED tx as decoded from its signed CBOR (Rust ``describe_signed``):
    every post-signature view renders this, never the pre-signature header."""

    tx_id: StrictStr
    vkey_witnesses: StrictInt
    other_witnesses: StrictInt
    signer_key_hashes: tuple[StrictStr, ...]
    wallet_key_hash: StrictStr
    key_hash_matches: StrictBool
    signature_verifies: StrictBool
    check: CheckReport
    lines: tuple[StrictStr, ...]


class SignRequest(_Strict):
    """The ONLY thing the pipeline hands a wallet: shown, never signed here."""

    tx_id: StrictStr
    cbor_hex: StrictStr
    plan_hash: StrictStr
    explanation_sha256: StrictStr
    receipts_dir: StrictStr


class SignResult(_Strict):
    """Resume schema of the signature interrupt (CIP-30 ``signTx`` witness
    set). Milestone 1 stops at the request: nothing resumes it here."""

    witness_set_hex: StrictStr
