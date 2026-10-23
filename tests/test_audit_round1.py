"""Sec Auditer round-1 items that had no dedicated test yet."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
from typing import cast

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtWidgets import QApplication, QTableWidget

from struck.chat.assistant import ChatReply
from struck.plan.models import PlanProposal
from struck.strike.models import StrikePlanProposal
from struck.ui import builders
from struck.ui.chat_panel import ChatPanel
from struck.ui.panels import LEG_COLUMNS

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]
SEND = ROOT / "plans" / "send-2ada-preprod.json"
STRIKE = ROOT / "plans" / "strike-eth-bracket-testnet.json"


def test_multi_output_proposal_is_refused_loudly_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    monkeypatch.setattr(builders, "RECEIPTS_DIR", tmp_path)
    raw = cast("dict[str, object]", json.loads(SEND.read_text(encoding="utf-8")))
    legs = cast("list[dict[str, object]]", raw["legs"])
    outs = cast("list[object]", legs[0]["outputs"])
    legs[0]["outputs"] = [outs[0], outs[0]]
    plan = PlanProposal.model_validate_json(json.dumps(raw))
    b = builders.OnchainSendBuilder()
    qtbot.addWidget(b)
    b.recipient.setText("")
    b.load_proposal(plan)
    log = b.log.toPlainText()
    assert "NOT LOADED: the proposal has 2 output(s)" in log and "drops nothing" in log
    assert b.recipient.text() == ""  # nothing half-loaded
    saved = list((tmp_path / "chat").iterdir())
    assert len(saved) == 1 and PlanProposal.model_validate_json(saved[0].read_bytes()) == plan


def test_chat_strike_card_shows_every_leg_column(qtbot: QtBot) -> None:
    plan = StrikePlanProposal.model_validate_json(STRIKE.read_text(encoding="utf-8"))
    fields: dict[str, object] = {
        "kind": "strike", "onchain": None, "strike": cast("object", json.loads(plan.model_dump_json())),
        "trigger": None, "question": None,
    }  # fmt: skip
    reply = ChatReply.model_validate_json(json.dumps(fields))
    p = ChatPanel(print, print, print)
    qtbot.addWidget(p)
    p.show_reply(reply)
    tables = p.cards[-1].findChildren(QTableWidget)
    assert tables, "the Strike card has a leg table"
    t = tables[0]
    headers = tuple(
        (h.text() if (h := t.horizontalHeaderItem(i)) is not None else "")
        for i in range(t.columnCount())
    )
    assert headers == LEG_COLUMNS  # same columns as the Approval tab
    assert t.rowCount() == len(plan.legs)
