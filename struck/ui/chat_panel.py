"""Chat dock: THE conversational plan-making interface (spec §7.3 chat,
LLM-as-interface, triggers/bots). Stock PySide6 widgets only.

The SAME harness as ``python -m struck.chat.cli`` (struck/chat/assistant.py:
the ported 
structured ChatReply + InMemorySaver history). Every reply is shown readably
(legs / outputs / trigger tables, never JSON). A proposal's ONLY action is
'Review', which hands it to an EXISTING path where the human decides:

  strike        -> Approval tab (Run to approval -> check -> YOUR approval -> ...)
  trigger       -> Bots tab editor -> Add (strict validation); fired = new proposal
  onchain_send  -> On-chain send builder (Approval tab) -> the send_cli path

This panel has no approve, sign or send control and calls none.
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from pydantic import TypeAdapter
from PySide6.QtWidgets import (
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QWidget,
)

from ..chat.assistant import ChatAssistant, ChatRejected, ChatReply, Turn
from ..config import LlmConfig
from ..plan.models import PlanProposal
from ..plan.providers import LlmConfigError, chat_model, llm_config_of
from ..strike.models import StrikePlanProposal
from ..triggers.models import (
    CompositeCondition,
    Condition,
    NotCondition,
    StrikeDepositAction,
    StrikeOrderAction,
    TriggerSpec,
)
from .layout import LAYOUT
from .panels import (
    LEG_COLUMNS,
    Worker,
    _button,
    _table,
    fill_table,
    strike_leg_rows,
    ui_log,
)
from struck_py import format_lovelace


_SCALARS: TypeAdapter[dict[str, str | int | bool]] = TypeAdapter(dict[str, str | int | bool])


def _cond_rows(c: Condition, depth: int = 0) -> list[tuple[str, ...]]:
    pad = "  " * depth
    if isinstance(c, CompositeCondition):
        rows: list[tuple[str, ...]] = [
            (f"{pad}condition", f"{c.logic.upper()} of {len(c.children)}")
        ]
        for child in c.children:
            rows += _cond_rows(child, depth + 1)
        return rows
    if isinstance(c, NotCondition):
        return [(f"{pad}condition", "NOT"), *_cond_rows(c.child, depth + 1)]
    try:
        fields = _SCALARS.validate_json(c.model_dump_json(exclude_none=True, exclude={"kind"}))
        detail = "  ".join(f"{k}={v}" for k, v in fields.items())
    except ValueError:
        detail = c.model_dump_json(exclude_none=True, exclude={"kind"})
    return [(f"{pad}condition", f"{c.kind}  {detail}")]


def trigger_rows(spec: TriggerSpec) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = [("label", spec.label), *_cond_rows(spec.condition)]
    for a in spec.actions:
        if isinstance(a, StrikeOrderAction):
            # Same columns as the Approval tab (every LLM-controlled field, as given).
            row = strike_leg_rows((a.leg,))[0]
            cols: tuple[str, ...] = LEG_COLUMNS
            for i in range(1, len(cols)):
                rows.append((f"action strike_order · {cols[i]}", row[i]))
        elif isinstance(a, StrikeDepositAction):
            amount = (
                f"{a.pct_of_incoming}% of the incoming payment"
                if a.pct_of_incoming is not None
                else f"{a.asset_amount} {a.asset_symbol}"
            )
            rows.append(
                (
                    "action",
                    f"strike_deposit {amount} on {a.blockchain} from {a.user_address}",
                )
            )
        else:
            rows.append(("action", f"wallet_send from {a.from_address}"))
            for o in a.outputs:
                if o.pct_of_incoming is not None:
                    amount = f"{o.pct_of_incoming}% of the incoming payment"
                elif o.lovelace == "incoming":
                    amount = "incoming amount"
                elif isinstance(o.lovelace, int):
                    amount = f"{format_lovelace(o.lovelace)} ADA"
                else:
                    amount = "no amount"
                rows.append(("  to", f"{o.address}  {amount}"))
            rows.append(
                (
                    "  max fee / validity",
                    f"{format_lovelace(a.max_fee_lovelace)} ADA / {a.validity_minutes} min",
                )
            )
    rows += [
        ("rearm", str(spec.rearm).lower()),
        ("cooldown_s", str(spec.cooldown_s)),
        ("policy_id", spec.policy_id or ""),
    ]
    return rows


def onchain_rows(plan: PlanProposal) -> list[tuple[str, ...]]:
    leg = plan.legs[0]
    rows: list[tuple[str, ...]] = [("from", leg.from_address)]
    for i, o in enumerate(leg.outputs):
        assets = (
            ", ".join(f"{a.qty} × {a.policy_hex}.{a.name_hex}" for a in o.assets)
            if o.assets
            else ""
        )
        rows.append((f"output {i}", f"{o.address}  {format_lovelace(o.lovelace)} ADA  {assets}"))
    rows += [
        ("max fee", f"{format_lovelace(leg.max_fee_lovelace)} ADA"),
        ("valid for", f"{leg.validity_minutes} min"),
    ]
    return rows


class ChatPanel(QWidget):
    def __init__(
        self,
        review_strike: Callable[[StrikePlanProposal], None],
        review_trigger: Callable[[TriggerSpec], None],
        review_onchain: Callable[[PlanProposal], None],
    ) -> None:
        super().__init__()
        self._review = (review_strike, review_trigger, review_onchain)
        self.model_factory: Callable[[LlmConfig], BaseChatModel] = chat_model
        self.assistant: ChatAssistant | None = None
        self._llm: LlmConfig | None = None
        self.state = QLabel("")
        self.state.setObjectName("muted")
        self.state.setWordWrap(True)
        self.history = LAYOUT.vbox()
        self.history.addStretch(1)
        body = QWidget()
        body.setLayout(self.history)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self._scroll = scroll
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText(
            "Tell Struck what you want: a send, a Strike order, a bot/trigger…"
        )
        self.input.setMaximumHeight(80)
        self.ask_btn = _button("Ask", "primary")
        self.ask_btn.clicked.connect(self.ask)
        row = LAYOUT.hbox()
        row.addWidget(self.input, 1)
        row.addWidget(self.ask_btn)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        layout.addWidget(QLabel("Chat · the LLM only PROPOSES; you review and approve"))
        layout.addWidget(self.state)
        layout.addWidget(scroll, 1)
        layout.addLayout(row)
        self._worker: Worker | None = None
        self._turn: Turn | None = None
        self.cards: list[QWidget] = []
        self.refresh_state()

    # ---- state ----------------------------------------------------------
    def refresh_state(self) -> bool:
        try:
            llm = llm_config_of()
            model = self.model_factory(llm)
        except (LlmConfigError, ValueError, OSError) as exc:
            # Loud, like every other panel state: the reason is on the label.
            self.state.setText(ui_log("chat", f"[LLM NOT CONFIGURED: {exc}]"))
            return False
        if llm != self._llm or self.assistant is None:
            self.assistant = ChatAssistant(model)  # a provider change starts a new thread
            self._llm = llm
        self.state.setText(
            f"[LLM READY: {llm.provider} / {llm.model}{' @ ' + llm.base_url if llm.base_url else ''}]"
        )
        return True

    # ---- turns ----------------------------------------------------------
    def _card(self, title: str, widget: QWidget | None = None, text: str = "") -> QGroupBox:
        box = QGroupBox(title)
        v = LAYOUT.vbox()
        box.setLayout(v)
        if text:
            label = QLabel(text)
            label.setWordWrap(True)
            v.addWidget(label)
        if widget is not None:
            v.addWidget(widget)
        self.history.insertWidget(self.history.count() - 1, box)
        self.cards.append(box)
        return box

    def ask(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            self.state.setText(ui_log("chat", "NOT DONE: the message is empty"))
            return
        if not self.refresh_state() or self.assistant is None:
            return
        assistant = self.assistant
        self._card("you", text=text)
        self.input.clear()
        self.ask_btn.setEnabled(False)
        self.state.setText(ui_log("chat", f"thinking ({self._llm.provider if self._llm else ''})…"))

        def run() -> str:
            self._turn = assistant.ask(text)
            return "ok"

        self._worker = Worker(run)
        self._worker.done.connect(self._answered)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _failed(self, text: str) -> None:
        self.ask_btn.setEnabled(True)
        self._card("REJECTED", text=ui_log("chat", f"REJECTED: {text}"))
        self.state.setText(f"Planner failed: {text}")

    def _answered(self, _text: str) -> None:
        self.ask_btn.setEnabled(True)
        self.refresh_state()
        if self._turn is not None:
            self.show_turn(self._turn)

    def show_turn(self, turn: Turn) -> None:
        if turn.tool_calls:
            self._card(
                "tools (read-only, served data)",
                text="\n".join(f"{n}({a})" for n, a in turn.tool_calls),
            )
        self.show_reply(turn.reply)

    def show_reply(self, reply: ChatReply) -> QPushButton | None:
        if reply.question is not None:
            self._card("Struck asks", text=reply.question)
            ui_log("chat", f"question: {reply.question}")
            return None
        review = _button("Review", "default")
        if reply.strike is not None:
            plan = reply.strike
            t = _table(LEG_COLUMNS)
            fill_table(t, strike_leg_rows(plan.legs))
            review.clicked.connect(lambda: self._review[0](plan))
            title, text, table = "Strike proposal", f"{plan.intent}\n{plan.rationale}", t
        elif reply.trigger is not None:
            spec = reply.trigger
            table = self._kv(trigger_rows(spec))
            review.clicked.connect(lambda: self._review[1](spec))
            title, text = "Trigger / bot proposal", spec.label
        else:
            assert reply.onchain is not None
            onchain = reply.onchain
            table = self._kv(onchain_rows(onchain))
            review.clicked.connect(lambda: self._review[2](onchain))
            title, text = "On-chain send proposal", f"{onchain.intent}\n{onchain.rationale}"
        box = self._card(title, table, text)
        layout = box.layout()
        if layout is not None:
            layout.addWidget(review)
        ui_log("chat", f"{reply.kind} proposal shown; Review hands it to the existing path")
        return review

    @staticmethod
    def _kv(rows: list[tuple[str, ...]]) -> QTableWidget:
        t = _table(("field", "value"))
        fill_table(t, rows)
        return t


__all__ = ["ChatPanel", "ChatRejected"]
