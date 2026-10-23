"""The Struck pipeline as a LangGraph ``StateGraph`` (spec §3.1, on-chain lane):

  propose_plan -> check_plan -> approve_plan (interrupt: HUMAN) ->
  verify_approval -> build_tx -> congruence -> write_receipts ->
  sign_and_submit (a wallet from Struck's encrypted store, ``struck.wallets``)
  | signature_request (interrupt: an external wallet signs; no stored wallet)

* Signing happens ONLY after the human approval of this plan hash AND the Rust
  tx-matches-plan check; the signed tx is re-checked in Rust (tx id unchanged,
  one vkey witness, key hash of from_address, signature over the tx id) before
  it is submitted on the selected backend. The wallet's network must be the
  selected network.

* Every edge carries a strict Pydantic model (``struck.plan.models``).
* Every money check is Rust (``struck_py``): plan check, coin selection,
  Pallas decode, tx-vs-plan congruence, explanation.
* Chain I/O goes through ONE ``ChainBackend`` chosen by explicit config
  (``node`` | ``koios``, ``struck.chain.backends``); no cross-backend checks,
  no fallback.
* A failed check routes to END with ``rejected`` set; nothing is retried or
  repaired. Each ``interrupt()`` node has no side effects (LangGraph re-runs
  a node on resume).
"""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict, cast
from zoneinfo import ZoneInfo

import struck_py
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pycardano import (
    Transaction,
    TransactionWitnessSet,
    VerificationKey,
    VerificationKeyWitness,
)
from pydantic import TypeAdapter

from .callhistory import sha256_hex
from .chain.backends import ChainBackend
from .chain.models import ChainUtxo
from .config import RECEIPTS_DIR
from .plan.models import (
    ApprovalRequest,
    ApprovedOnchainPlan,
    CheckReport,
    Explanation,
    PlanDecision,
    PlanProposal,
    SignedSummary,
    SignRequest,
    SignResult,
    UnsignedTx,
)
from .plan.planner import Planner
from .wallets import WalletSigner

CDMX = ZoneInfo("America/Mexico_City")
_UTXOS = TypeAdapter(tuple[ChainUtxo, ...])


class StruckState(TypedDict, total=False):
    """Checkpointed state. Models are stored as their canonical JSON and
    re-validated (strict) by every node that reads them: the checkpoint
    serializer never reconstructs a model unvalidated."""

    request: str
    proposal_json: str
    plan_json: str
    wallet_utxos_json: str
    plan_check_json: str
    ttl_wallclock: str
    decision_json: str
    receipts_dir: str
    unsigned_json: str
    explanation_json: str
    backend_receipts_json: str
    sign_request_json: str
    signature_json: str
    signed_check_json: str
    signed_summary_json: str
    submitted_tx_id: str
    rejected: str


def proposal_of(state: StruckState) -> PlanProposal:
    return PlanProposal.model_validate_json(state["proposal_json"])


def plan_of(state: StruckState) -> ApprovedOnchainPlan:
    return ApprovedOnchainPlan.model_validate_json(state["plan_json"])


def plan_check_of(state: StruckState) -> CheckReport:
    return CheckReport.model_validate_json(state["plan_check_json"])


def decision_of(state: StruckState) -> PlanDecision:
    return PlanDecision.model_validate_json(state["decision_json"])


def unsigned_of(state: StruckState) -> UnsignedTx:
    return UnsignedTx.model_validate_json(state["unsigned_json"])


def explanation_of(state: StruckState) -> Explanation:
    return Explanation.model_validate_json(state["explanation_json"])


def signed_summary_of(state: StruckState) -> SignedSummary:
    return SignedSummary.model_validate_json(state["signed_summary_json"])


def signed_view_lines(summary: SignedSummary, explanation: Explanation) -> tuple[str, ...]:
    """Post-signature explanation: the SIGNED header (witnesses, signer key hash
    vs the wallet's payment key, signature over the body hash, tx id) followed by
    the body lines (what is spent and created). The pre-signature header line
    (``SIGNATURE REQUEST (awaiting signature) ...``) is dropped."""
    return (*summary.lines, *explanation.lines[1:])


def sign_request_of(state: StruckState) -> SignRequest:
    return SignRequest.model_validate_json(state["sign_request_json"])


def wallet_utxos_of(state: StruckState) -> tuple[ChainUtxo, ...]:
    return _UTXOS.validate_json(state["wallet_utxos_json"])


_RECEIPTS = TypeAdapter(dict[str, str])


def backend_receipts_of(state: StruckState) -> dict[str, str]:
    return _RECEIPTS.validate_json(state["backend_receipts_json"])


def utxos_json(utxos: tuple[ChainUtxo, ...]) -> str:
    return _UTXOS.dump_json(utxos).decode("utf-8")


def _write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _pretty(json_text: str) -> str:
    """Indent a JSON document for a receipt (key order and values unchanged)."""
    return json.dumps(cast("object", json.loads(json_text)), indent=2)


class Pipeline:
    def __init__(
        self,
        planner: Planner,
        backend: ChainBackend,
        receipts_root: Path = RECEIPTS_DIR,
        signer: WalletSigner | None = None,
    ) -> None:
        self._planner = planner
        self._backend = backend
        self._receipts_root = receipts_root
        self._signer = signer
        if signer is not None and signer.network != backend.network.name:
            raise ValueError(
                f"NETWORK MISMATCH: wallet '{signer.name}' is a {signer.network} wallet; the "
                f"selected network is {backend.network.name}. Nothing is run."
            )

    @property
    def signs(self) -> bool:
        return self._signer is not None

    # N1 ─ the LLM (or a fixed plan) proposes; schema errors raise loudly.
    def propose_plan(self, state: StruckState) -> StruckState:
        return {"proposal_json": self._planner.propose(state["request"]).model_dump_json()}

    # N2 ─ deterministic plan check (Rust) against live chain state.
    def check_plan(self, state: StruckState) -> StruckState:
        leg = proposal_of(state).legs[0]
        view = self._backend.wallet_view(leg.from_address, leg.validity_minutes)
        net = self._backend.network.name
        plan = ApprovedOnchainPlan(
            network=net,
            from_address=leg.from_address,
            outputs=leg.outputs,
            max_fee_lovelace=leg.max_fee_lovelace,
            valid_until_slot=view.valid_until_slot,
        )
        report = CheckReport.model_validate_json(
            struck_py.validate_onchain_plan(
                plan.canonical_json,
                utxos_json(view.utxos),
                view.tip_slot,
                self._backend.network.rust_json(),
            )
        )
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_dir = self._receipts_root / f"{stamp}-{self._backend.name}-send-{plan.plan_hash[:12]}"
        out_dir.mkdir(parents=True, exist_ok=False)
        # Receipts of the plan as checked, written BEFORE any approval: the
        # plan the human is asked about, its check and the UTxO snapshot.
        _write(out_dir, "chain.backend.txt", self._backend.name)
        _write(out_dir, "plan.proposal.json", _pretty(state["proposal_json"]))
        _write(out_dir, "plan.pending.json", _pretty(plan.canonical_json))
        _write(out_dir, "plan.check.json", _pretty(report.model_dump_json()))
        _write(out_dir, "wallet.utxos.json", _pretty(utxos_json(view.utxos)))
        update: StruckState = {
            "plan_json": plan.canonical_json,
            "wallet_utxos_json": utxos_json(view.utxos),
            "plan_check_json": report.model_dump_json(),
            "ttl_wallclock": view.ttl_wallclock,
            "receipts_dir": str(out_dir),
        }
        if not report.ok:
            failed = [c.id for c in report.checks if not c.ok]
            update["rejected"] = f"plan check failed: {failed}"
        elif self._signer is not None and self._signer.address != plan.from_address:
            update["rejected"] = (
                f"wallet '{self._signer.name}' ({self._signer.address}) does not own "
                f"from_address {plan.from_address}; nothing to approve"
            )
        return update

    # N3 ─ HUMAN plan approval. interrupt() is the only effect in this node.
    def approve_plan(self, state: StruckState) -> StruckState:
        plan = plan_of(state)
        summary = tuple(
            f"Pay {struck_py.format_lovelace(o.lovelace)}"
            + (f" + {len(o.assets)} token kind(s)" if o.assets else "")
            + f" to {o.address}"
            for o in plan.outputs
        ) + (
            f"From wallet {plan.from_address}",
            f"Fee bound {struck_py.format_lovelace(plan.max_fee_lovelace)}",
            f"Valid until slot {plan.valid_until_slot} ({state['ttl_wallclock']})",
        )
        request = ApprovalRequest(
            plan_hash=plan.plan_hash, plan=plan, plan_check=plan_check_of(state), summary=summary
        )
        decision = interrupt(request.model_dump_json(), response_schema=PlanDecision)
        return {"decision_json": decision.model_dump_json()}

    # Deterministic approval check: the decision must name THIS plan's hash.
    def verify_approval(self, state: StruckState) -> StruckState:
        decision = decision_of(state)
        if decision.decision != "approve":
            return {"rejected": f"plan rejected by {decision.approver}"}
        if not hmac.compare_digest(decision.plan_hash, plan_of(state).plan_hash):
            return {"rejected": "approval names a different plan hash; nothing is built"}
        return {}

    # N4c ─ build the UNSIGNED tx on the configured backend (inputs chosen
    # by the Rust coin selection over the checked UTxO snapshot).
    def build_tx(self, state: StruckState) -> StruckState:
        plan = plan_of(state)
        refs = struck_py.select_inputs_for_plan(plan.canonical_json, state["wallet_utxos_json"])
        unsigned = self._backend.build_unsigned(
            plan, refs, wallet_utxos_of(state), Path(state["receipts_dir"])
        )
        return {"unsigned_json": unsigned.model_dump_json()}

    # N5c+N6c ─ Rust/Pallas decode vs the APPROVED plan, then the explanation.
    def congruence(self, state: StruckState) -> StruckState:
        unsigned = unsigned_of(state)
        explanation = Explanation.model_validate_json(
            struck_py.explain_tx(
                unsigned.cbor_hex,
                plan_of(state).canonical_json,
                state["wallet_utxos_json"],
                state["ttl_wallclock"],
                self._backend.network.rust_json(),
            )
        )
        extra = self._backend.validate_unsigned(unsigned, Path(state["receipts_dir"]))
        update: StruckState = {
            "explanation_json": explanation.model_dump_json(),
            "backend_receipts_json": json.dumps(extra),
        }
        if explanation.tx_id != unsigned.tx_id:
            update["rejected"] = (
                f"tx id mismatch: Pallas {explanation.tx_id} vs {unsigned.source} {unsigned.tx_id}"
            )
        elif not explanation.congruence.ok:
            failed = [c.id for c in explanation.congruence.checks if not c.ok]
            update["rejected"] = f"congruence failed: {failed}"
        return update

    def write_receipts(self, state: StruckState) -> StruckState:
        out = Path(state["receipts_dir"])
        unsigned = unsigned_of(state)
        explanation = explanation_of(state)
        plan = plan_of(state)
        _write(out, "plan.proposal.json", _pretty(state["proposal_json"]))
        _write(out, "plan.approved.json", _pretty(plan.canonical_json))
        _write(out, "plan.check.json", _pretty(state["plan_check_json"]))
        _write(out, "plan.decision.json", _pretty(state["decision_json"]))
        _write(out, "wallet.utxos.json", _pretty(state["wallet_utxos_json"]))
        _write(out, "tx.cbor.hex", unsigned.cbor_hex)
        _write(out, "tx.decoded.pallas.json", _pretty(struck_py.decode_tx(unsigned.cbor_hex)))
        _write(out, "tx.build.stdout.txt", unsigned.build_stdout)
        for name, text in backend_receipts_of(state).items():
            _write(out, name, text)
        _write(out, "congruence.json", _pretty(explanation.congruence.model_dump_json()))
        _write(out, "explanation.json", _pretty(explanation.model_dump_json()))
        words = "\n".join(explanation.lines)
        _write(out, "explanation.txt", words)
        request = SignRequest(
            tx_id=unsigned.tx_id,
            cbor_hex=unsigned.cbor_hex,
            plan_hash=plan.plan_hash,
            explanation_sha256=sha256_hex(words.encode("utf-8")),
            receipts_dir=str(out),
        )
        _write(out, "sign.request.json", _pretty(request.model_dump_json()))
        return {"sign_request_json": request.model_dump_json()}

    # N7c ─ sign the EXACT checked CBOR with the stored wallet, re-check, submit.
    def sign_and_submit(self, state: StruckState) -> StruckState:
        signer = self._signer
        if signer is None:  # unreachable: the graph only routes here with a signer
            return {"rejected": "no wallet configured; nothing signed"}
        out = Path(state["receipts_dir"])
        plan, unsigned = plan_of(state), unsigned_of(state)
        net = self._backend.network
        if not (signer.network == plan.network == net.name):
            return {
                "rejected": f"NETWORK MISMATCH: wallet {signer.network}, plan {plan.network}, "
                f"selected {net.name}; nothing signed"
            }
        if signer.address != plan.from_address:
            return {"rejected": f"wallet '{signer.name}' does not own {plan.from_address}"}
        if explanation_of(state).tx_id != unsigned.tx_id:
            return {"rejected": "tx id differs from the congruence-checked tx; nothing signed"}
        tx = Transaction.from_cbor(unsigned.cbor_hex)
        body_hash = tx.transaction_body.hash()
        if body_hash.hex() != unsigned.tx_id:
            return {"rejected": f"body hash {body_hash.hex()} != checked tx id; nothing signed"}
        vkey, signature = signer.sign(body_hash)
        tx.transaction_witness_set = TransactionWitnessSet(
            vkey_witnesses=[VerificationKeyWitness(VerificationKey.from_primitive(vkey), signature)]
        )
        signed_hex = tx.to_cbor_hex()
        report = CheckReport.model_validate_json(
            struck_py.verify_signed_tx(signed_hex, unsigned.tx_id, plan.from_address)
        )
        _write(out, "signed.check.json", _pretty(report.model_dump_json()))
        summary = SignedSummary.model_validate_json(
            struck_py.describe_signed_tx(
                signed_hex, unsigned.tx_id, plan.from_address, net.rust_json()
            )
        )
        words = signed_view_lines(summary, explanation_of(state))
        _write(out, "signed.explanation.json", _pretty(summary.model_dump_json()))
        _write(out, "signed.explanation.txt", "\n".join(words))
        if not report.ok:
            failed = [c.id for c in report.checks if not c.ok]
            return {
                "signed_check_json": report.model_dump_json(),
                "signed_summary_json": summary.model_dump_json(),
                "rejected": f"signed-tx check failed: {failed}; nothing submitted",
            }
        _write(out, "tx.signed.cbor.hex", signed_hex)  # public: body + vkey + signature
        tx_id, reply = self._backend.submit(signed_hex)
        _write(out, f"submit.{self._backend.name}.response.txt", reply.decode("utf-8", "replace"))
        receipt = {
            "tx_id": tx_id,
            "expected_tx_id": unsigned.tx_id,
            "backend": self._backend.name,
            "network": net.name,
            "wallet": signer.name,
            "plan_hash": plan.plan_hash,
            "submitted_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _write(out, "submitted.json", json.dumps(receipt, indent=2))
        update: StruckState = {
            "signed_check_json": report.model_dump_json(),
            "signed_summary_json": summary.model_dump_json(),
            "submitted_tx_id": tx_id,
        }
        if tx_id != unsigned.tx_id:
            update["rejected"] = (
                f"SUBMITTED, but {self._backend.name} answered tx id {tx_id}, not the checked "
                f"{unsigned.tx_id}: see {out}/submitted.json"
            )
        return update

    # N7c (no stored wallet) ─ the signature request for an external wallet.
    def signature_request(self, state: StruckState) -> StruckState:
        signature = interrupt(state["sign_request_json"], response_schema=SignResult)
        return {"signature_json": signature.model_dump_json()}


def _route_after_check(state: StruckState) -> Literal["approve_plan", "__end__"]:
    return "__end__" if "rejected" in state else "approve_plan"


def _route_after_approval(state: StruckState) -> Literal["build_tx", "__end__"]:
    return "__end__" if "rejected" in state else "build_tx"


def _route_after_congruence(state: StruckState) -> Literal["write_receipts", "__end__"]:
    return "__end__" if "rejected" in state else "write_receipts"


def build_graph(
    pipeline: Pipeline,
) -> CompiledStateGraph[StruckState, None, StruckState, StruckState]:
    graph = StateGraph(StruckState)
    graph.add_node("propose_plan", pipeline.propose_plan)
    graph.add_node("check_plan", pipeline.check_plan)
    graph.add_node("approve_plan", pipeline.approve_plan)
    graph.add_node("verify_approval", pipeline.verify_approval)
    graph.add_node("build_tx", pipeline.build_tx)
    graph.add_node("congruence", pipeline.congruence)
    graph.add_node("write_receipts", pipeline.write_receipts)
    last = "sign_and_submit" if pipeline.signs else "signature_request"
    graph.add_node(last, pipeline.sign_and_submit if pipeline.signs else pipeline.signature_request)
    graph.add_edge(START, "propose_plan")
    graph.add_edge("propose_plan", "check_plan")
    graph.add_conditional_edges("check_plan", _route_after_check)
    graph.add_edge("approve_plan", "verify_approval")
    graph.add_conditional_edges("verify_approval", _route_after_approval)
    graph.add_edge("build_tx", "congruence")
    graph.add_conditional_edges("congruence", _route_after_congruence)
    graph.add_edge("write_receipts", last)
    graph.add_edge(last, END)
    return graph.compile(checkpointer=InMemorySaver())
