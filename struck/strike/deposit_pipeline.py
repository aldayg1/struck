"""Desktop deposit lane: separate human approval, signature and confirm gates."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TypedDict

import struck_py
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pycardano import Transaction, TransactionWitnessSet, VerificationKey, VerificationKeyWitness
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from ..callhistory import RecordedHttp, sha256_hex
from ..chain.backends import make_backend
from ..config import RECEIPTS_DIR, load_config
from ..network import NetworkName, canonical_address
from ..pipeline import utxos_json
from ..plan.models import CheckReport, Explanation, PlanDecision
from ..wallets import load_signer
from .account import StrikeAuthedClient
from .deposit import DepositClient, DepositPlanProposal, cip30_utxos, review_cardano_deposit
from .deposit_cli import DEPOSIT_VALIDITY_MINUTES
from .venue_auth import wallet_from_secrets


class DepositReview(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    proposal: DepositPlanProposal
    network: NetworkName
    wallet: StrictStr
    max_fee_lovelace: StrictInt

    @property
    def plan_hash(self) -> str:
        return sha256_hex(self.model_dump_json().encode())


class DepositState(TypedDict, total=False):
    review_json: str
    decision_json: str
    receipts_dir: str
    unsigned_cbor: str
    explanation_json: str
    request_id: str
    expiration_at: int
    tx_id: str
    confirm_json: str


def _review(state: DepositState) -> DepositReview:
    review = DepositReview.model_validate_json(state["review_json"])
    if review.network != load_config().network:
        raise ValueError("Deposit approval belongs to a different network")
    return review


def _write(state: DepositState, name: str, content: str | bytes) -> None:
    path = Path(state["receipts_dir"]) / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


def _decision(state: DepositState, stage: str) -> DepositReview:
    review = _review(state)
    decision = PlanDecision.model_validate_json(state["decision_json"])
    if decision.decision != "approve" or decision.plan_hash != review.plan_hash:
        raise ValueError(f"Deposit {stage} refused: approval does not match the plan")
    if not decision.approver.strip():
        raise ValueError("An approver name is required")
    _write(state, f"{stage}.decision.json", decision.model_dump_json())
    return review


def _gate(state: DepositState) -> DepositState:
    decision = interrupt(state["review_json"], response_schema=PlanDecision)
    return {"decision_json": decision.model_dump_json()}


def _prepare(state: DepositState) -> DepositState:
    review = _review(state)
    proposal = review.proposal
    cfg = load_config()
    if proposal.blockchain != "cardano" or proposal.asset_symbol != "ADA":
        raise ValueError("The checked desktop deposit signer currently supports Cardano ADA only")
    canonical_address(proposal.user_address, cfg.profile)
    if not struck_py.compare_quantities(proposal.asset_amount, "gt", "0"):
        raise ValueError("Deposit amount must be positive")
    if review.max_fee_lovelace <= 0:
        raise ValueError("Set a positive fee bound")
    out = RECEIPTS_DIR / f"deposit-{time.time_ns()}-{review.plan_hash[:12]}"
    out.mkdir(parents=True, exist_ok=False)
    prepared: DepositState = {"receipts_dir": str(out)}
    _write(prepared, "plan.proposal.json", review.model_dump_json(indent=2))
    return prepared


def _build(state: DepositState) -> DepositState:
    review = _decision(state, "approve")
    cfg = load_config()
    proposal = review.proposal
    signer = load_signer(review.wallet, cfg.network)
    if signer.address != proposal.user_address:
        raise ValueError("Selected wallet does not match the approved deposit address")
    http = RecordedHttp(cfg.timeout_s)
    try:
        backend = make_backend(cfg, cfg.chain_backend, http)
        backend.check_network()
        view = backend.wallet_view(proposal.user_address, DEPOSIT_VALIDITY_MINUTES)
        inputs = utxos_json(view.utxos)
        _write(state, "wallet.utxos.json", inputs)
        client = DepositClient(StrikeAuthedClient(http, cfg.strike_api_url, wallet_from_secrets()))
        served, quote = client.quote(
            proposal.blockchain, proposal.asset_symbol, proposal.asset_amount, proposal.mode
        )
        _write(state, "deposit.quote.json", served.content)
        if (quote.quote.asset_amount != proposal.asset_amount
                or quote.quote.asset_symbol != proposal.asset_symbol
                or quote.quote.blockchain != proposal.blockchain
                or quote.quote.request_id != quote.request_id):
            raise ValueError("Served quote differs from the approved deposit")
        served_build, built = client.build_tx(
            quote.request_id, proposal.user_address, proposal.blockchain, cip30_utxos(view.utxos)
        )
        _write(state, "deposit.build-tx.json", served_build.content)
        if built.format != "cardano_cbor" or not isinstance(built.unsigned_tx, str):
            raise ValueError("Strike did not return Cardano CBOR")
        explanation = review_cardano_deposit(
            built.unsigned_tx, proposal.user_address, int(proposal.asset_amount),
            review.max_fee_lovelace, view.valid_until_slot, inputs,
            view.ttl_wallclock, cfg.profile.rust_json(),
        )
        _write(state, "explanation.json", explanation.model_dump_json(indent=2))
        _write(state, "tx.decoded.json", struck_py.decode_tx(built.unsigned_tx))
        _write(state, "tx.unsigned.cbor.hex", built.unsigned_tx)
        if not explanation.congruence.ok:
            raise ValueError("Deposit congruence failed; see the receipt checks")
        return {
            "unsigned_cbor": built.unsigned_tx,
            "explanation_json": explanation.model_dump_json(),
            "request_id": quote.request_id,
            "expiration_at": quote.quote.expiration_at,
        }
    finally:
        http.close()


def _submit(state: DepositState) -> DepositState:
    review = _decision(state, "sign")
    if time.time_ns() // 1_000_000 >= state["expiration_at"]:
        raise ValueError("Deposit quote expired; no signature requested")
    cfg = load_config()
    signer = load_signer(review.wallet, cfg.network)
    if signer.address != review.proposal.user_address:
        raise ValueError("Signing wallet does not match the approved address")
    explanation = Explanation.model_validate_json(state["explanation_json"])
    if not explanation.congruence.ok:
        raise ValueError("Deposit congruence has not passed")
    tx = Transaction.from_cbor(state["unsigned_cbor"])
    body_hash = tx.transaction_body.hash()
    if body_hash.hex() != explanation.tx_id:
        raise ValueError("Transaction changed after review")
    vkey, signature = signer.sign(body_hash)
    tx.transaction_witness_set = TransactionWitnessSet(
        vkey_witnesses=[VerificationKeyWitness(VerificationKey.from_primitive(vkey), signature)]
    )
    signed = tx.to_cbor_hex()
    check = CheckReport.model_validate_json(
        struck_py.verify_signed_tx(signed, explanation.tx_id, signer.address)
    )
    _write(state, "signed.check.json", check.model_dump_json())
    if not check.ok:
        raise ValueError("Signed transaction verification failed")
    _write(state, "tx.signed.cbor.hex", signed)
    _write(state, "tx.id.txt", explanation.tx_id)
    attempt = Path(state["receipts_dir"]) / "submission.started"
    with attempt.open("x") as marker:
        marker.write(explanation.tx_id)
    http = RecordedHttp(cfg.timeout_s)
    try:
        tx_id, response = make_backend(cfg, cfg.chain_backend, http).submit(signed)
        _write(state, "submit.response.txt", response)
        if tx_id != explanation.tx_id:
            raise ValueError("Submission returned another transaction ID; reconcile before any further action")
        receipt: dict[str, str] = {
            "tx_id": tx_id, "network": cfg.network, "request_id": state["request_id"],
            "plan_hash": review.plan_hash,
        }
        _write(state, "submitted.json", json.dumps(receipt))
        return {"tx_id": tx_id}
    finally:
        http.close()


def _confirm(state: DepositState) -> DepositState:
    _decision(state, "confirm")
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        client = DepositClient(StrikeAuthedClient(http, cfg.strike_api_url, wallet_from_secrets()))
        served, result = client.confirm(state["request_id"], state["tx_id"])
        _write(state, "deposit.confirm.json", served.content)
        return {"confirm_json": result.model_dump_json()}
    finally:
        http.close()


def build_deposit_graph() -> CompiledStateGraph[DepositState, None, DepositState, DepositState]:
    graph = StateGraph(DepositState)
    graph.add_node("prepare", _prepare)
    graph.add_node("approve", _gate)
    graph.add_node("build", _build)
    graph.add_node("sign", _gate)
    graph.add_node("submit", _submit)
    graph.add_node("confirm", _gate)
    graph.add_node("notify", _confirm)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "approve")
    graph.add_edge("approve", "build")
    graph.add_edge("build", "sign")
    graph.add_edge("sign", "submit")
    graph.add_edge("submit", "confirm")
    graph.add_edge("confirm", "notify")
    graph.add_edge("notify", END)
    return graph.compile(checkpointer=InMemorySaver())
