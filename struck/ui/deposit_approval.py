"""Human-operated deposit approval, raw transaction review and submission."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Protocol, TypedDict, cast

import struck_py
from langgraph.types import Command
from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit, QPushButton, QWidget

from ..config import load_config
from ..plan.models import Explanation, PlanDecision
from ..strike.deposit import DepositPlanProposal
from ..strike.deposit_pipeline import DepositReview, DepositState, build_deposit_graph
from ..wallets import list_wallets
from .layout import LAYOUT
from .panels import Worker


class _ThreadConfig(TypedDict):
    configurable: dict[str, str]


class _Snapshot(Protocol):
    @property
    def values(self) -> DepositState: ...

    @property
    def next(self) -> tuple[str, ...]: ...


class _Graph(Protocol):
    def invoke(self, input: DepositState | Command[str], config: _ThreadConfig) -> object: ...

    def get_state(self, config: _ThreadConfig) -> _Snapshot: ...


class DepositApproval(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.state = QLabel("Select a wallet and amount in Deposit, then review the proposal here.")
        self.state.setWordWrap(True)
        self.approver = QLineEdit()
        self.approver.setPlaceholderText("Your name")
        self.fee = QLineEdit()
        self.fee.setPlaceholderText("Maximum network fee in lovelace")
        self.prepare = QPushButton("Check deposit plan")
        self.action = QPushButton("Approve deposit plan")
        self.action.setEnabled(False)
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        layout.addWidget(self.state)
        layout.addWidget(self.approver)
        layout.addWidget(self.fee)
        layout.addWidget(self.raw, 1)
        layout.addWidget(self.prepare)
        layout.addWidget(self.action)
        self.prepare.clicked.connect(self.check)
        self.action.clicked.connect(self.advance)
        self._proposal: DepositPlanProposal | None = None
        self._review: DepositReview | None = None
        self._graph: _Graph | None = None
        self._thread = ""
        self._worker: Worker | None = None

    def load_proposal(self, proposal: DepositPlanProposal) -> None:
        if self._worker is not None and self._worker.isRunning():
            raise ValueError("Wait for the current deposit operation to finish")
        self._proposal = proposal
        self._review = None
        self._graph = None
        self.action.setEnabled(False)
        self.prepare.setEnabled(True)
        self.fee.setEnabled(True)
        self.state.setText(f"NETWORK: {load_config().network} · Set a fee bound and check this proposal.")
        self.raw.setPlainText(proposal.model_dump_json(indent=2))

    def check(self) -> None:
        try:
            if self._proposal is None:
                raise ValueError("Create a deposit proposal first")
            cfg = load_config()
            wallets = [w for w in list_wallets()
                       if w.network == cfg.network and w.address == self._proposal.user_address]
            if not wallets:
                raise ValueError("The proposal address has no imported wallet on this network")
            review = DepositReview(
                proposal=self._proposal, network=cfg.network, wallet=wallets[0].name,
                max_fee_lovelace=int(self.fee.text()),
            )
            self._review = review
            self._graph = cast("_Graph", build_deposit_graph())
            self._thread = f"deposit-ui-{time.time_ns()}"
            graph = self._graph
            thread = self._thread

            def run() -> str:
                graph.invoke({"review_json": review.model_dump_json()},
                             {"configurable": {"thread_id": thread}})
                return "ok"

            self._run(run)
        except (ValueError, RuntimeError) as exc:
            self._failed(str(exc))

    def _run(self, job: "Callable[[], str]") -> None:
        self.prepare.setEnabled(False)
        self.action.setEnabled(False)
        self.fee.setEnabled(False)
        self._worker = Worker(job)
        self._worker.done.connect(self._updated)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def advance(self) -> None:
        graph, review, thread = self._graph, self._review, self._thread
        if graph is None or review is None:
            self._failed("Check the deposit plan first")
            return
        if not self.approver.text().strip():
            self.state.setText("Enter your name before approving.")
            return
        decision = PlanDecision(
            decision="approve", plan_hash=review.plan_hash,
            approver=self.approver.text().strip(), ts_ms=time.time_ns() // 1_000_000,
        )

        def run() -> str:
            command: Command[str] = Command(
                resume=cast("dict[str, object]", json.loads(decision.model_dump_json()))
            )
            graph.invoke(command, {"configurable": {"thread_id": thread}})
            return "ok"

        self.state.setText("Processing the approved step…")
        self._run(run)

    def _updated(self, _result: str) -> None:
        if self._graph is None or self._review is None:
            return
        snapshot = self._graph.get_state({"configurable": {"thread_id": self._thread}})
        values = snapshot.values
        stage = snapshot.next[0] if snapshot.next else "done"
        text = self._review.model_dump_json(indent=2)
        if "explanation_json" in values:
            explanation = Explanation.model_validate_json(values["explanation_json"])
            text += "\n\n" + "\n".join(explanation.lines)
            text += "\n\nRAW CBOR\n" + values["unsigned_cbor"]
            text += "\n\nDECODED JSON\n" + struck_py.decode_tx(values["unsigned_cbor"])
        if "tx_id" in values:
            text += "\n\nSUBMITTED TX ID: " + values["tx_id"]
        if "confirm_json" in values:
            text += "\n\nSTRIKE RESPONSE\n" + values["confirm_json"]
        self.raw.setPlainText(text)
        labels = {
            "approve": "Approve plan and request quote",
            "sign": "Sign and submit reviewed transaction",
            "confirm": "Confirm deposit with Strike",
        }
        self.action.setText(labels.get(stage, "Deposit submitted; credit pending"))
        self.action.setEnabled(stage in labels)
        self.state.setText(
            f"NETWORK: {self._review.network} · {stage}\n"
            f"Plan hash: {self._review.plan_hash}\nReceipts: {values['receipts_dir']}"
        )

    def _failed(self, reason: str) -> None:
        self.state.setText(f"Deposit stopped: {reason}. No automatic retry.")
        self.action.setEnabled(False)
