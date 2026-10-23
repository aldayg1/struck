"""Form BUILDERS (no JSON typing anywhere): each produces an EXISTING strict
model and hands it to the EXISTING check -> approval path.

* ``ModelForm``      : stock pydantic introspection (``model_fields`` +
  ``TypeAdapter.validate_strings``) renders one field per widget; the strict
  model then validates the whole result. No hand-written per-field parsing.
* ``TriggerBuilder`` : condition picker (the trigger models' leaf kinds) +
  action picker (strike_order StrikeLeg / wallet_send) -> ``TriggerSpec``
  -> BotsPanel Add (strict validation, store).
* ``OnchainSendBuilder`` : wallet UTxOs SERVED by the active chain backend
  (``make_backend(...).wallet_view``) -> recipient + lovelace + assets picked
  from those UTxOs -> ``PlanProposal`` -> the send pipeline's deterministic
  check (graph run to the approval interrupt). Approve shows the unsigned tx.
  Sign and submit is a second click, and only with a wallet already imported
  into Struck's store. The graph itself has no signer.
"""

from __future__ import annotations

import json
import time
import types
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Union, cast, get_args, get_origin

from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, TypeAdapter, ValidationError
from pydantic_core import PydanticUndefined
from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..callhistory import RecordedHttp
from ..chain.backends import WalletView, make_backend
from ..chain.models import Asset
from ..config import RECEIPTS_DIR, load_config
from ..pipeline import (
    Pipeline,
    StruckState,
    _pretty,
    build_graph,
    explanation_of,
    plan_check_of,
    plan_of,
    sign_request_of,
)
from ..plan.models import OnchainSendLeg, PlanDecision, PlanProposal, SendOutput
from ..plan.planner import FixedPlanner
from ..secrets import SecretError
from ..send_cli import _Graph, _ThreadConfig
from ..strike.models import StrikeLeg
from ..strike.deposit import review_cardano_deposit
from ..wallets import WalletError, list_wallets, load_signer
from ..triggers.models import (
    Condition,
    IncomingPaymentCondition,
    IntervalCondition,
    PriceCondition,
    ScheduleCondition,
    SendTo,
    StrikeDepositAction,
    StrikeOrderAction,
    StrikePositionCondition,
    TimeWindowCondition,
    TriggerSpec,
    WalletBalanceCondition,
    WalletSendAction,
)
from .layout import LAYOUT
from .panels import Worker, _button, _mono, ui_log
from struck_py import decode_tx, format_lovelace

_FormWidget = QLineEdit | QComboBox | QCheckBox


def _origin(annotation: object) -> object:
    return cast("object", get_origin(annotation))


def _args(annotation: object) -> tuple[object, ...]:
    return cast("tuple[object, ...]", get_args(annotation))


def _optional(annotation: object) -> tuple[object, bool]:
    origin = _origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in _args(annotation) if a is not type(None)]
        if len(args) != len(_args(annotation)):
            return (args[0] if len(args) == 1 else annotation), True
    return annotation, False


def _has_default(model: type[BaseModel], name: str) -> bool:
    """A field the strict model itself can fill (default or default_factory).
    An empty widget for it means 'keep the model default', never an error.
    pydantic types ``default``/``default_factory`` as Any: the exact-type
    casts are the sanctioned boundary squeeze, then plain ``is`` checks."""
    info = model.model_fields[name]
    default = cast("object", info.default)
    factory = cast("object | None", info.default_factory)
    return default is not PydanticUndefined or factory is not None


class ModelForm(QWidget):
    """One widget per scalar field of a strict pydantic model."""

    def __init__(self, model: type[BaseModel], skip: tuple[str, ...] = ("kind",)) -> None:
        super().__init__()
        self.model = model
        self.widgets: dict[str, tuple[_FormWidget, object, bool]] = {}
        form = LAYOUT.form()
        self.setLayout(form)
        for name, info in model.model_fields.items():
            if name in skip:
                continue
            ann, optional = _optional(cast("object", info.annotation))
            widget: _FormWidget
            if ann is bool:
                widget = QCheckBox()
            elif _origin(ann) is Literal:
                widget = QComboBox()
                widget.addItems(([""] if optional else []) + [str(v) for v in _args(ann)])
            else:
                widget = QLineEdit()
                hint = "comma-separated" if _origin(ann) is tuple else ""
                blank_ok = optional or _has_default(model, name)
                widget.setPlaceholderText(
                    " · ".join(h for h in (hint, "optional" if blank_ok else "") if h)
                )
            self.widgets[name] = (widget, ann, optional)
            form.addRow(name, widget)

    def set_text(self, name: str, text: str) -> None:
        widget = self.widgets[name][0]
        if isinstance(widget, QLineEdit):
            widget.setText(text)
        elif not isinstance(widget, QCheckBox):
            widget.setCurrentText(text)

    def values(self) -> dict[str, object]:
        """Typed field values (stock TypeAdapter.validate_strings); errors name the field."""
        out: dict[str, object] = {}
        for name, (widget, ann, optional) in self.widgets.items():
            if isinstance(widget, QCheckBox):
                out[name] = widget.isChecked()
                continue
            text = widget.text().strip() if isinstance(widget, QLineEdit) else widget.currentText()
            if not text:
                if optional:
                    out[name] = None
                    continue
                if _has_default(self.model, name):
                    continue  # empty field: the strict model's own default stands
                raise ValueError(f"{self.model.__name__}.{name} is required")
            try:
                if _origin(ann) is tuple:
                    item = TypeAdapter(cast("type[object]", _args(ann)[0]))
                    out[name] = [
                        cast(
                            "object",
                            item.dump_python(item.validate_strings(p.strip()), mode="json"),
                        )
                        for p in text.split(",")
                    ]
                else:
                    adapter = TypeAdapter(cast("type[object]", ann))
                    out[name] = cast(
                        "object", adapter.dump_python(adapter.validate_strings(text), mode="json")
                    )
            except ValidationError as exc:
                raise ValueError(
                    f"{self.model.__name__}.{name}: {cast('list[dict[str, object]]', exc.errors())[0]['msg']}"
                ) from exc
        return out


CONDITIONS: tuple[type[BaseModel], ...] = (
    PriceCondition,
    StrikePositionCondition,
    IncomingPaymentCondition,
    WalletBalanceCondition,
    TimeWindowCondition,
    ScheduleCondition,
    IntervalCondition,
)


def _kind(model: type[BaseModel]) -> str:
    return str(_args(cast("object", model.model_fields["kind"].annotation))[0])


class TriggerBuilder(QGroupBox):
    """Condition picker (+ and/or/xor/not group) + action picker -> TriggerSpec."""

    def __init__(self, submit: Callable[[TriggerSpec], None]) -> None:
        super().__init__("Trigger / bot builder")
        self._submit = submit
        self.label = QLineEdit()
        self.label.setPlaceholderText("label")
        self.condition_kind = QComboBox()
        self.conditions = QStackedWidget()
        self.condition_forms = [ModelForm(m) for m in CONDITIONS]
        for m, f in zip(CONDITIONS, self.condition_forms, strict=True):
            self.condition_kind.addItem(_kind(m))
            self.conditions.addWidget(f)
        self.condition_kind.currentIndexChanged.connect(self._pick_condition)
        self._held: Condition | None = None  # a proposed composite/not tree, kept as proposed
        self.action_kind = QComboBox()
        self.action_kind.addItems(["strike_order", "wallet_send"])
        self.action_stack = QStackedWidget()
        self.leg_form = ModelForm(StrikeLeg, skip=("tp", "sl"))
        self.send_form = ModelForm(WalletSendAction, skip=("kind", "outputs"))
        self.send_to = ModelForm(SendTo)
        send = QWidget()
        sv = LAYOUT.vbox()
        send.setLayout(sv)
        sv.addWidget(self.send_form)
        sv.addWidget(QLabel("output (lovelace: an integer or 'incoming')"))
        sv.addWidget(self.send_to)
        self.action_stack.addWidget(self.leg_form)
        self.action_stack.addWidget(send)
        self.action_kind.currentIndexChanged.connect(self.action_stack.setCurrentIndex)
        self._held_action: StrikeDepositAction | None = None
        self.action_kind.currentIndexChanged.connect(self._drop_held_action)
        self.rearm = QCheckBox("rearm")
        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 86400)
        self.policy = QLineEdit()
        self.policy.setPlaceholderText("policy_id (optional)")
        build = _button("Build → Add (validate)", "primary")
        build.clicked.connect(self.build)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        # Composite conditions (the existing CompositeCondition / NotCondition models):
        # pick a leaf above, "Add to group" collects it; the group combines them.
        self.group_logic = QComboBox()
        self.group_logic.addItems(["single", "and", "or", "xor", "not"])
        self.group_logic.setToolTip(
            "single = the picked condition; and=all, or=any, xor; not = negate ONE"
        )
        self.group: list[dict[str, object]] = []
        self.group_view = QLabel("group: (empty)")
        self.group_view.setWordWrap(True)
        add_child = _button("Add to group", "default")
        add_child.clicked.connect(self.add_to_group)
        clear_group = _button("Clear group", "default")
        clear_group.clicked.connect(self.clear_group)
        nest = _button("Nest group", "default")
        nest.setToolTip("combine the group with the chosen logic into ONE child of a new group")
        nest.clicked.connect(self.nest_group)
        group_row = LAYOUT.hbox()
        for g in (
            QLabel("combine"),
            self.group_logic,
            add_child,
            nest,
            clear_group,
            self.group_view,
        ):
            group_row.addWidget(g)
        group_row.addStretch(1)
        top = LAYOUT.hbox()
        for w in (
            self.label,
            QLabel("condition"),
            self.condition_kind,
            QLabel("action"),
            self.action_kind,
        ):
            top.addWidget(w)
        bottom = LAYOUT.hbox()
        for w2 in (self.rearm, QLabel("cooldown_s"), self.cooldown, self.policy, build):
            bottom.addWidget(w2)
        forms = LAYOUT.hbox()
        forms.addWidget(self.conditions, 1)
        forms.addWidget(self.action_stack, 1)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        layout.addLayout(top)
        layout.addLayout(forms)
        layout.addLayout(group_row)
        layout.addLayout(bottom)
        layout.addWidget(self.status)

    def _pick_condition(self, i: int) -> None:
        self.conditions.setCurrentIndex(i)
        if self._held is not None:
            self._held = None
            self.status.setText(
                ui_log(
                    "trigger-builder",
                    "proposed composite condition dropped: the picker's condition is used",
                )
            )

    def _leaf(self) -> dict[str, object]:
        i = self.condition_kind.currentIndex()
        return {"kind": _kind(CONDITIONS[i]), **self.condition_forms[i].values()}

    def add_to_group(self) -> None:
        try:
            leaf = self._leaf()
        except (ValueError, ValidationError) as exc:
            self.status.setText(ui_log("trigger-builder", f"NOT ADDED: {exc}"))
            return
        self._held = None
        self.group.append(leaf)
        self.group_view.setText("group: " + " | ".join(str(c["kind"]) for c in self.group))
        self.status.setText(
            ui_log("trigger-builder", f"added {leaf['kind']} to the group ({len(self.group)})")
        )

    def nest_group(self) -> None:
        if self.group_logic.currentText() == "single":
            self.status.setText(ui_log("trigger-builder", "NOT NESTED: pick and/or/xor/not first"))
            return
        try:
            child = cast("dict[str, object]", self._condition())
        except (ValueError, ValidationError) as exc:
            self.status.setText(ui_log("trigger-builder", f"NOT NESTED: {exc}"))
            return
        self.group = [child]
        self.group_logic.setCurrentText("single")
        self.group_view.setText(f"group: {child['kind']}(…)")
        self.status.setText(
            ui_log("trigger-builder", f"nested a {child['kind']} as one group child")
        )

    def clear_group(self) -> None:
        self.group = []
        self.group_view.setText("group: (empty)")

    def _condition(self) -> object:
        logic = self.group_logic.currentText()
        if self._held is not None and logic == "single" and not self.group:
            return cast("object", json.loads(self._held.model_dump_json(by_alias=True)))
        if logic == "single":
            if self.group:
                raise ValueError("the group has conditions: pick and/or/xor/not, or Clear group")
            return self._leaf()
        children = self.group or [self._leaf()]
        if logic == "not":
            if len(children) != 1:
                raise ValueError(
                    f"'not' negates exactly ONE condition; the group has {len(children)}"
                )
            return {"kind": "not", "child": children[0]}
        return {"kind": "composite", "logic": logic, "children": children}

    def _drop_held_action(self, _index: int) -> None:
        self._held_action = None

    def spec(self) -> TriggerSpec:
        condition = self._condition()
        action: dict[str, object]
        if self._held_action is not None:
            action = cast(
                "dict[str, object]", json.loads(self._held_action.model_dump_json())
            )
        elif self.action_kind.currentText() == "strike_order":
            action = {
                "kind": "strike_order",
                "leg": {**self.leg_form.values(), "tp": None, "sl": None},
            }
        else:
            action = {
                "kind": "wallet_send",
                **self.send_form.values(),
                "outputs": [self.send_to.values()],
            }
        raw = {
            "label": self.label.text().strip(),
            "condition": condition,
            "actions": [action],
            "rearm": self.rearm.isChecked(),
            "cooldown_s": self.cooldown.value(),
            "policy_id": self.policy.text().strip() or None,
        }
        return TriggerSpec.model_validate_json(json.dumps(raw))

    def build(self) -> None:
        try:
            spec = self.spec()
        except (ValueError, ValidationError) as exc:
            self.status.setText(ui_log("trigger-builder", f"REJECTED: {exc}"))
            return
        self.status.setText(ui_log("trigger-builder", f"built '{spec.label}': handed to Add"))
        self._submit(spec)

    def load(self, spec: TriggerSpec) -> None:
        """Show a (chat-proposed or stored) spec in the form for review/edit."""
        self.label.setText(spec.label)
        self.rearm.setChecked(spec.rearm)
        self.cooldown.setValue(spec.cooldown_s)
        self.policy.setText(spec.policy_id or "")
        self.clear_group()
        self.group_logic.setCurrentText("single")
        kinds = [_kind(m) for m in CONDITIONS]
        if spec.condition.kind in kinds:
            i = kinds.index(spec.condition.kind)
            self.condition_kind.setCurrentIndex(i)
            self._fill(self.condition_forms[i], spec.condition)
            self._held = None
        else:
            self._held = spec.condition
            self.status.setText(
                f"proposed '{spec.condition.kind}' condition kept as proposed "
                "(pick a condition to replace it with a single one)"
            )
        if not spec.actions:
            self._held_action = None
            self.status.setText(
                "this trigger is a sequence: each step holds its own action. "
                "Picking an action here replaces the sequence."
            )
            return
        a = spec.actions[0]
        if isinstance(a, StrikeDepositAction):
            self._held_action = a
            self.status.setText(
                "strike_deposit kept as proposed. Changing the action picker replaces it. "
                "A fire writes the proposal. It does not quote or sign."
            )
        elif isinstance(a, StrikeOrderAction):
            self._held_action = None
            self.action_kind.setCurrentIndex(0)
            self._fill(self.leg_form, a.leg)
        else:
            self._held_action = None
            self.action_kind.setCurrentIndex(1)
            self._fill(self.send_form, a)
            self._fill(self.send_to, a.outputs[0])

    @staticmethod
    def _fill(form: ModelForm, model: BaseModel) -> None:
        for name, (widget, _ann, _opt) in form.widgets.items():
            v: object = getattr(model, name)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(v))
            else:
                form.set_text(
                    name,
                    "" if v is None else ", ".join(map(str, v)) if isinstance(v, tuple) else str(v),
                )


class ChainIncluded(BaseModel):
    """``chain.included.json`` as written by the Koios inclusion receipt."""

    model_config = ConfigDict(strict=True, extra="allow", frozen=True)

    tx_hash: StrictStr
    block_height: StrictInt
    absolute_slot: StrictInt
    explorer: StrictStr


class OnchainSendBuilder(QWidget):
    """Recipient + amount + assets picked from SERVED wallet UTxOs -> PlanProposal
    -> the send pipeline. Check stops at the approval interrupt. Approve resumes
    that same graph and shows the raw CBOR, the Pallas JSON and the explanation.
    No wallet is attached here, so nothing is signed or submitted."""

    waiting = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.from_address = QLineEdit()
        self.from_address.setPlaceholderText(
            f"your {load_config().profile.address_prefix}… address (the wallet that pays)"
        )
        load = _button("Load wallet UTxOs (active backend)", "default")
        load.clicked.connect(self.load_wallet)
        self.utxos = QTableWidget(0, 4)
        self.utxos.setHorizontalHeaderLabels(("utxo", "ADA", "assets", ""))
        self.assets = QTableWidget(0, 3)
        self.assets.setHorizontalHeaderLabels(("send qty", "asset (served)", "available"))
        self.recipient = QLineEdit()
        self.recipient.setPlaceholderText(f"recipient {load_config().profile.address_prefix}…")
        self.lovelace = QLineEdit()
        self.lovelace.setPlaceholderText("amount in lovelace (integer)")
        self.ada = QLabel("= — ADA")
        self.lovelace.textChanged.connect(self._show_ada)
        self.max_fee = QLineEdit("1000000")
        self.validity = QSpinBox()
        self.validity.setRange(1, 1440)
        self.validity.setValue(30)
        check = _button("Build → check (stops at approval)", "primary")
        check.clicked.connect(self.check)
        self.approver = QLineEdit()
        self.approver.setPlaceholderText("your name (required to approve)")
        self.approve_btn = _button("Approve", "success")
        self.approve_btn.clicked.connect(lambda: self.decide("approve"))
        self.reject_btn = _button("Reject", "danger")
        self.reject_btn.clicked.connect(lambda: self.decide("reject"))
        self.approve_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        self.copy_btn = _button("Copy CBOR", "default")
        self.copy_btn.clicked.connect(self.copy_cbor)
        self.receipt_btn = _button("Show confirmed receipt…", "default")
        self.receipt_btn.setObjectName("show_receipt")
        self.receipt_btn.clicked.connect(self.open_receipt)
        self.wallet_pick = QComboBox()
        self.wallet_pick.setObjectName("wallet_pick")
        self._wallet_names: list[str] = []
        self.wallets_btn = _button("List imported wallets", "default")
        self.wallets_btn.clicked.connect(self.refresh_wallets)
        self.sign_btn = _button("Sign and submit", "warning")
        self.sign_btn.setObjectName("sign_submit")
        self.sign_btn.clicked.connect(self.sign_clicked)
        self.sign_btn.setEnabled(False)
        self.explain = _mono()
        self.explain.setMaximumHeight(160)
        self.cbor = _mono()
        self.cbor.setMaximumHeight(80)
        self.decoded = _mono()
        self.decoded.setMaximumHeight(140)
        self.log = _mono()
        self.log.setMaximumHeight(170)
        form = LAYOUT.form()
        row = LAYOUT.hbox()
        row.addWidget(self.from_address, 1)
        row.addWidget(load)
        form.addRow("from", row)
        amount = LAYOUT.hbox()
        amount.addWidget(self.lovelace, 1)
        amount.addWidget(self.ada)
        form.addRow("recipient", self.recipient)
        form.addRow("lovelace", amount)
        form.addRow("max fee lovelace", self.max_fee)
        form.addRow("validity minutes", self.validity)
        layout = LAYOUT.vbox()
        self.setLayout(layout)
        layout.addLayout(form)
        layout.addWidget(QLabel("Wallet UTxOs (served by the active chain backend)"))
        layout.addWidget(self.utxos, 1)
        layout.addWidget(QLabel("Assets to send (pick from the served UTxOs)"))
        layout.addWidget(self.assets, 1)
        layout.addWidget(check)
        self.wait = QLabel(
            "Nothing is waiting. Fill the form, then press Build → check. "
            "There is no signature until you approve."
        )
        self.wait.setObjectName("wait")
        self.wait.setWordWrap(True)
        layout.addWidget(self.wait)
        decide_row = LAYOUT.hbox()
        decide_row.addWidget(self.approver, 1)
        decide_row.addWidget(self.approve_btn)
        decide_row.addWidget(self.reject_btn)
        decide_row.addWidget(self.copy_btn)
        decide_row.addWidget(self.receipt_btn)
        layout.addLayout(decide_row)
        sign_row = LAYOUT.hbox()
        sign_row.addWidget(self.wallets_btn)
        sign_row.addWidget(self.wallet_pick, 1)
        sign_row.addWidget(self.sign_btn)
        layout.addLayout(sign_row)
        layout.addWidget(QLabel("Plain explanation (from the decoded tx)"))
        layout.addWidget(self.explain)
        layout.addWidget(QLabel("Raw CBOR hex"))
        layout.addWidget(self.cbor)
        layout.addWidget(QLabel("Decoded transaction JSON"))
        layout.addWidget(self.decoded)
        layout.addWidget(self.log)
        self._view: WalletView | None = None
        self._served_assets: list[Asset] = []
        self._worker: Worker | None = None
        self.last_state: StruckState | None = None
        self._graph: _Graph | None = None
        self._config: _ThreadConfig | None = None
        self._plan_hash: str | None = None

    def _set_wait(self, text: str) -> None:
        """The pause the human has to see. The log is not the prompt."""
        self.wait.setText(text)
        self.waiting.emit(text)
        if text.startswith("WAITING FOR YOU") or text.startswith("SIGNATURE REQUEST"):
            tabs = self.parent()
            while tabs is not None and not isinstance(tabs, QTabWidget):
                tabs = tabs.parent()
            if isinstance(tabs, QTabWidget):
                tabs.setCurrentWidget(self)

    def show_approval_wait(self, plan_hash: str) -> None:
        self._set_wait(
            "WAITING FOR YOU. Type your name in the box under this line and press Approve. "
            f"Plan {plan_hash}. No transaction is built, and there is no signature yet."
        )

    def show_signature_wait(self, tx_id: str) -> None:
        self._set_wait(
            f"SIGNATURE REQUEST for tx {tx_id}. The explanation and the raw CBOR are below. "
            "Pick an imported wallet and press Sign and submit. That click is the signature."
        )
        self.sign_btn.setEnabled(True)

    def _report(self, text: str) -> None:
        self.log.appendPlainText(ui_log("onchain-builder", text))

    def _failed(self, text: str) -> None:
        self._report(f"FAILED: {text}")

    def _show_ada(self, text: str) -> None:
        try:
            self.ada.setText(f"= {format_lovelace(int(text))} ADA")
        except ValueError:
            self.ada.setText("= (lovelace must be an integer)")

    def load_proposal(self, plan: PlanProposal) -> None:
        """Chat 'Review' for an on-chain send: fill the builder; nothing is checked yet.
        The builder holds ONE output; a multi-output proposal is refused loudly
        (never silently dropped): run it through send_cli instead."""
        leg = plan.legs[0]
        if len(leg.outputs) != 1 or leg.outputs[0].assets:
            path = (
                RECEIPTS_DIR
                / "chat"
                / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-onchain_send.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            self._report(
                f"NOT LOADED: the proposal has {len(leg.outputs)} output(s) / assets; the builder "
                f"form holds one ADA-only output and drops nothing. Saved {path}; check + approve it with "
                f"`uv run python -m struck.send_cli {path} --approver <you>` at a terminal"
            )
            return
        self.from_address.setText(leg.from_address)
        self.recipient.setText(leg.outputs[0].address)
        self.lovelace.setText(str(leg.outputs[0].lovelace))
        self.max_fee.setText(str(leg.max_fee_lovelace))
        self.validity.setValue(leg.validity_minutes)
        self._report(
            f"chat proposal loaded ({plan.intent}); press 'Load wallet UTxOs' then 'Build → check'"
        )

    def load_wallet(self) -> None:
        address = self.from_address.text().strip()
        if not address:
            self._report("NOT DONE: enter the paying address first")
            return
        cfg = load_config()

        def run() -> str:
            self._view = make_backend(
                cfg, cfg.chain_backend, RecordedHttp(cfg.timeout_s)
            ).wallet_view(address, self.validity.value())
            return cfg.chain_backend

        self._worker = Worker(run)
        self._worker.done.connect(self._loaded)
        self._worker.failed.connect(self._failed)
        self._report(f"loading UTxOs via backend {cfg.chain_backend}…")
        self._worker.start()

    def _loaded(self, backend: str) -> None:
        view = self._view
        if view is None:
            return
        self.utxos.setRowCount(len(view.utxos))
        seen: dict[tuple[str, str], int] = {}
        for i, u in enumerate(view.utxos):
            cells = (
                f"{u.tx_hash[:16]}…#{u.index}",
                "",
                ", ".join(f"{a.qty}×{a.name_hex}" for a in u.assets),
                "",
            )
            for j, c in enumerate(cells):
                self.utxos.setItem(i, j, QTableWidgetItem(c))
            self.utxos.setItem(i, 1, QTableWidgetItem(format_lovelace(u.lovelace)))
            for a in u.assets:
                seen[(a.policy_hex, a.name_hex)] = seen.get((a.policy_hex, a.name_hex), 0) + a.qty
        self._served_assets = [Asset(policy_hex=p, name_hex=n, qty=q) for (p, n), q in seen.items()]
        self.assets.setRowCount(len(self._served_assets))
        for i, a in enumerate(self._served_assets):
            self.assets.setItem(i, 0, QTableWidgetItem("0"))
            self.assets.setItem(i, 1, QTableWidgetItem(f"{a.policy_hex[:10]}….{a.name_hex}"))
            self.assets.setItem(i, 2, QTableWidgetItem(str(a.qty)))
        self.utxos.resizeColumnsToContents()
        self._report(
            f"{len(view.utxos)} UTxOs, {len(self._served_assets)} assets served by {backend} (tip slot {view.tip_slot})"
        )

    def proposal(self) -> PlanProposal:
        picked: list[Asset] = []
        for i, a in enumerate(self._served_assets):
            item = self.assets.item(i, 0)
            qty = int(item.text()) if item is not None and item.text().strip() else 0
            if qty:
                picked.append(Asset(policy_hex=a.policy_hex, name_hex=a.name_hex, qty=qty))
        leg = OnchainSendLeg(
            kind="send",
            from_address=self.from_address.text().strip(),
            outputs=(
                SendOutput(
                    address=self.recipient.text().strip(),
                    lovelace=int(self.lovelace.text()),
                    assets=tuple(picked),
                ),
            ),
            max_fee_lovelace=int(self.max_fee.text()),
            validity_minutes=self.validity.value(),
        )
        return PlanProposal(
            lane="onchain",
            intent="on-chain send (builder)",
            legs=(leg,),
            rationale="built in the on-chain send builder from served UTxOs",
        )

    def check(self) -> None:
        try:
            plan = self.proposal()
        except (ValueError, ValidationError) as exc:
            self._report(f"REJECTED: {exc}")
            return
        cfg = load_config()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = RECEIPTS_DIR / "builder" / f"{stamp}-onchain_send.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        self.approve_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        self.sign_btn.setEnabled(False)
        self.last_state = None
        self._plan_hash = None
        self.explain.clear()
        self.cbor.clear()
        self.decoded.clear()
        self._set_wait("Checking the plan. Approve stays off until the check finishes.")

        def run() -> str:
            pipeline = Pipeline(
                planner=FixedPlanner(plan.model_dump_json()),
                backend=make_backend(cfg, cfg.chain_backend, RecordedHttp(cfg.timeout_s)),
            )
            graph = cast("_Graph", build_graph(pipeline))
            config: _ThreadConfig = {"configurable": {"thread_id": f"builder-{stamp}"}}
            self._graph = graph
            self._config = config
            graph.invoke({"request": f"builder plan {path}"}, config)
            self.last_state = graph.get_state(config).values
            return str(path)

        self._worker = Worker(run)
        self._worker.done.connect(self._checked)
        self._worker.failed.connect(self._failed)
        self._report(f"plan saved {path}; running the deterministic check on {cfg.chain_backend}…")
        self._worker.start()

    def _checked(self, path: str) -> None:
        state = self.last_state
        if state is None:
            return
        if "rejected" in state:
            self._report(f"REJECTED before approval: {state['rejected']}")
        if "plan_check_json" in state:
            for c in plan_check_of(state).checks:
                self._report(
                    f"[{'ok' if c.ok else 'FAIL'}] {c.id}: expected {c.expected} | actual {c.actual}"
                )
        if "rejected" not in state:
            plan_hash = plan_of(state).plan_hash
            self._plan_hash = plan_hash
            self.approve_btn.setEnabled(True)
            self.reject_btn.setEnabled(True)
            self.show_approval_wait(plan_hash)
            self._report(
                f"STOPPED at the approval interrupt (plan_hash {plan_hash}). "
                "Type your name and press Approve. Nothing is built until then."
            )

    def decide(self, verdict: Literal["approve", "reject"]) -> None:
        approver = self.approver.text().strip()
        graph, config, plan_hash = self._graph, self._config, self._plan_hash
        if not approver:
            self._report("NOT DONE: type your name. Nothing is built.")
            return
        if graph is None or config is None or plan_hash is None:
            self._report("NOT DONE: run Build → check first")
            return
        self.approve_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        decision = PlanDecision(
            decision=verdict,
            plan_hash=plan_hash,
            approver=approver,
            ts_ms=time.time_ns() // 1_000_000,
        )

        def run() -> str:
            resume: Command[str] = Command(
                resume=cast("dict[str, object]", json.loads(decision.model_dump_json()))
            )
            graph.invoke(resume, config)
            self.last_state = graph.get_state(config).values
            return verdict

        self._worker = Worker(run)
        self._worker.done.connect(self._after_decision)
        self._worker.failed.connect(self._failed)
        self._report(f"{verdict} {plan_hash} as {approver}")
        self._worker.start()

    def _after_decision(self, verdict: str) -> None:
        state = self.last_state
        if state is None:
            return
        if "rejected" in state:
            self._report(f"REJECTED: {state['rejected']}")
            return
        if verdict == "reject":
            self._report("rejected; nothing built")
            return
        if "explanation_json" not in state or "sign_request_json" not in state:
            self._report("STOPPED: the graph did not reach the signature request")
            return
        sign = sign_request_of(state)
        self.show_unsigned(
            "\n".join(explanation_of(state).lines),
            sign.cbor_hex,
            _pretty(decode_tx(sign.cbor_hex)),
        )
        self.show_signature_wait(sign.tx_id)
        self._report(f"SIGNATURE REQUEST tx id {sign.tx_id} receipts {sign.receipts_dir}")

    def refresh_wallets(self) -> None:
        """Names and addresses only. The seed stays in the encrypted store."""
        cfg = load_config()
        try:
            rows = tuple(w for w in list_wallets() if w.network == cfg.network)
        except (WalletError, SecretError) as exc:
            self._report(str(exc))
            return
        self._wallet_names = [w.name for w in rows]
        self.wallet_pick.clear()
        for row in rows:
            self.wallet_pick.addItem(f"{row.name}  {row.address}")
        if not rows:
            self._report(f"no imported {cfg.network} wallet in the store")
            return
        self._report(f"{len(rows)} imported {cfg.network} wallet(s)")

    def sign_clicked(self) -> None:
        """Second click. Refuses unless a signature request is already on screen."""
        state = self.last_state
        if state is None or "sign_request_json" not in state:
            self._report("NOT SIGNED: no signature request is on screen")
            return
        if "rejected" in state:
            self._report(f"NOT SIGNED: {state['rejected']}")
            return
        idx = self.wallet_pick.currentIndex()
        if idx < 0 or idx >= len(self._wallet_names):
            self._report("NOT SIGNED: list an imported wallet for this network and pick it")
            return
        name = self._wallet_names[idx]
        proposal_json = state["proposal_json"]
        self.sign_btn.setEnabled(False)
        self._set_wait(
            f"Signing with wallet {name}. Nothing is submitted until the signed-tx check passes."
        )

        def run() -> str:
            cfg = load_config()
            signer = load_signer(name, cfg.network)
            pipeline = Pipeline(
                FixedPlanner(proposal_json),
                make_backend(cfg, cfg.chain_backend, RecordedHttp(cfg.timeout_s)),
                signer=signer,
            )
            update = pipeline.sign_and_submit(state)
            if "rejected" in update:
                return f"NOT SUBMITTED: {update['rejected']}"
            tx_id = update.get("submitted_tx_id")
            if not isinstance(tx_id, str) or tx_id == "":
                return "NOT SUBMITTED: sign_and_submit returned no tx id"
            return f"SUBMITTED {tx_id}"

        self._worker = Worker(run)
        self._worker.done.connect(self._signed)
        self._worker.failed.connect(self._sign_failed)
        self._worker.start()

    def _signed(self, text: str) -> None:
        self._report(text)
        self._set_wait(text)
        self.sign_btn.setEnabled(not text.startswith("SUBMITTED"))

    def _sign_failed(self, text: str) -> None:
        self._report(f"NOT SIGNED: {text}")
        self._set_wait(f"NOT SIGNED: {text}")
        self.sign_btn.setEnabled(True)

    def show_deposit_cbor(
        self,
        cbor_hex: str,
        user_address: str,
        deposit_lovelace: int,
        max_fee_lovelace: int,
        valid_until_slot: int,
        inputs_json: str,
        ttl_wallclock: str,
    ) -> None:
        """Put a Cardano deposit's unsigned tx in the three panes. Sign stays off."""
        explained = review_cardano_deposit(
            cbor_hex,
            user_address,
            deposit_lovelace,
            max_fee_lovelace,
            valid_until_slot,
            inputs_json,
            ttl_wallclock,
            load_config().profile.rust_json(),
        )
        self.show_unsigned("\n".join(explained.lines), cbor_hex, _pretty(decode_tx(cbor_hex)))
        self.sign_btn.setEnabled(False)
        if explained.congruence.ok:
            self._report(
                f"deposit tx {explained.tx_id} shown. Sign stays off: no signature request yet."
            )
        else:
            failed = ", ".join(c.id for c in explained.congruence.checks if not c.ok)
            self._report(f"CONGRUENCE FAILED: {failed}. Do NOT sign.")

    def show_unsigned(self, explanation: str, cbor_hex: str, decoded_json: str) -> None:
        self.explain.setPlainText(explanation)
        self.cbor.setPlainText(cbor_hex)
        self.decoded.setPlainText(decoded_json)

    def open_receipt(self) -> None:
        """Human picks a pipeline receipt directory. Reads it. Does not approve or submit."""
        chosen = QFileDialog.getExistingDirectory(
            self, "Confirmed receipt directory", str(RECEIPTS_DIR)
        )
        if not chosen:
            return
        directory = Path(chosen)
        needed = ("explanation.txt", "tx.cbor.hex", "tx.decoded.pallas.json")
        missing = [name for name in needed if not (directory / name).is_file()]
        if missing:
            self._report(f"NOT A RECEIPT: missing {', '.join(missing)}")
            return
        self.show_receipt(directory)

    def show_receipt(self, directory: Path) -> None:
        """Fill the three panes from a real pipeline receipt. Reads only.

        When ``chain.included.json`` is present, the explanation pane also shows
        the tx hash, block and explorer URL that file records.
        """
        explanation = (directory / "explanation.txt").read_text(encoding="utf-8")
        included_path = directory / "chain.included.json"
        if included_path.is_file():
            included = ChainIncluded.model_validate_json(included_path.read_text(encoding="utf-8"))
            explanation = (
                f"{explanation.rstrip()}\n\n"
                f"CONFIRMED on chain: tx {included.tx_hash} "
                f"block {included.block_height} slot {included.absolute_slot}\n"
                f"{included.explorer}\n"
            )
        self.show_unsigned(
            explanation,
            (directory / "tx.cbor.hex").read_text(encoding="utf-8").strip(),
            (directory / "tx.decoded.pallas.json").read_text(encoding="utf-8"),
        )
        self.approve_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        self.sign_btn.setEnabled(False)
        self.last_state = None
        self._set_wait(
            "Past receipt on screen. Sign and submit is off. "
            "This does not sign, and it does not submit again."
        )
        self._report(f"receipt {directory} shown; Approve and Sign stay off")

    def copy_cbor(self) -> None:
        text = self.cbor.toPlainText()
        if not text:
            self._report("NOTHING TO COPY: no CBOR yet")
            return
        QGuiApplication.clipboard().setText(text)
        self._report(f"copied {len(text)} hex characters")


__all__ = ["ModelForm", "OnchainSendBuilder", "QPlainTextEdit", "TriggerBuilder"]
