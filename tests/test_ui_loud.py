"""Offscreen UI: every control either works or fails LOUDLY (visible text +
logs/ui_events.log). Send goes through the SAME execute() with a MOCK
transport; the approve/send clicks here happen ONLY in this mock test."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from typing import cast

import httpx
import pytest
from PySide6.QtWidgets import QApplication

from struck.callhistory import RecordedHttp
from struck.strike.account import StrikeAuthedClient
from struck.strike.auth import wallet_from_hex
from struck.strike.market import StrikeMarketClient
from struck.strike.order_cli import _Graph
from struck.strike.pipeline import FixedStrikePlanner, StrikePipeline, build_strike_graph
from struck.triggers.engine import TriggerStore
from struck.ui import panels
from tests.test_strike_execute import BASE, PUB, ROOT, SEED, _Mock

APP = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _ui_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "ui_events.log"
    monkeypatch.setattr(panels, "UI_LOG", log)
    return log


def _settle(panel: panels.ApprovalPanel) -> None:
    if panel._worker is not None:
        panel._worker.wait(10_000)
    APP.processEvents()


def _drive(panel: panels.ApprovalPanel, tmp_path: Path, mock: _Mock, with_key: bool) -> None:
    http = RecordedHttp(5, tmp_path / "h.jsonl")
    http._client = httpx.Client(transport=httpx.MockTransport(mock))
    wallet = wallet_from_hex(PUB, SEED) if with_key else None
    pipeline = StrikePipeline(
        FixedStrikePlanner(
            (ROOT / "plans/strike-eth-bracket-testnet.json").read_text(encoding="utf-8")
        ),
        StrikeMarketClient(http, BASE),
        receipts_root=tmp_path,
        sender=StrikeAuthedClient(http, BASE, wallet) if wallet else None,
    )
    panel._wallet = wallet
    panel._graph = cast("_Graph", build_strike_graph(pipeline))
    panel._config = {"configurable": {"thread_id": f"t-{with_key}"}}
    panel._graph.invoke({"request": "test"}, panel._config)
    panel._show_approval("approval")
    assert panel.approve_btn.isEnabled()
    panel.approve_btn.click()  # mock-only test click
    _settle(panel)


def test_no_key_shows_reason_and_disables_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _ui_log: Path
) -> None:
    panel = panels.ApprovalPanel()
    assert "strike_api_testnet" in panel.key_label.text()
    mock = _Mock()
    _drive(panel, tmp_path, mock, with_key=False)
    assert not panel.send_btn.isEnabled() and "disabled" in panel.send_btn.text()
    assert (
        "SEND DISABLED" in panel.raw_view.toPlainText()
        and "strike_api_testnet" in panel.raw_view.toPlainText()
    )
    panel.confirm_send("send")
    assert "NOT SENT" in panel.raw_view.toPlainText() and mock.writes == []
    assert "SEND DISABLED" in _ui_log.read_text()


def test_send_click_goes_through_execute_with_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from struck.strike.venue_auth import save_account

    from tests.conftest import unlock_test_store

    panel = panels.ApprovalPanel()
    mock = _Mock()
    unlock_test_store()
    save_account("testnet", "ui-mock", PUB, SEED)
    _drive(panel, tmp_path, mock, with_key=True)
    assert panel.send_btn.isEnabled()
    panel.send_btn.click()  # mock-only test click
    _settle(panel)
    assert len(mock.writes) == 1 and mock.writes[0].url.path == "/v2/order/strategy"
    assert "RESPONSE leg 0: POST /v2/order/strategy -> HTTP 201" in panel.raw_view.toPlainText()


def test_controls_without_state_fail_loudly(tmp_path: Path, _ui_log: Path) -> None:
    panel = panels.ApprovalPanel()
    panel.decide("approve")
    assert "NOT DONE" in panel.raw_view.toPlainText()
    panel.confirm_send("abort")
    assert "NOT DONE" in panel.raw_view.toPlainText()
    bots = panels.BotsPanel(TriggerStore(tmp_path / "triggers.json"))
    bots.table.setCurrentCell(-1, -1)
    for action in (
        bots.edit_selected,
        bots.save_edit,
        bots.remove,
        lambda: bots.set_status("paused"),
    ):
        action()
    log = bots.log.toPlainText()
    assert log.count("NOT DONE") == 4 and "select a trigger" in log
    bots.add()  # empty builder form: rejected, naming the missing field
    assert "REJECTED" in bots.log.toPlainText()
    assert "NOT DONE" in _ui_log.read_text()


def _fill_bracket(t: panels.OrderTicket) -> None:
    t.symbol.addItems(["ETH-USD"])  # stands in for the served /v2/markets symbols
    t.symbol.setCurrentText("ETH-USD")
    t.side.setCurrentText("buy")
    t.type.setCurrentText("limit")
    t.size_edit.setText("0.010")
    t.price.setText("2400.00")
    t.tif.setCurrentText("GTC")
    t.leverage.setText("5")
    t.tp_type.setCurrentText("take_profit")
    t.tp_stop.setText("2600.00")
    t.sl_type.setCurrentText("stop")
    t.sl_stop.setText("2300.00")


def test_ticket_builds_the_same_legs_as_the_plan_file() -> None:
    from struck.strike.models import StrikePlanProposal

    panel = panels.ApprovalPanel()
    _fill_bracket(panel.ticket)
    panel.ticket.build()
    assert panel._plan_text is not None
    built = StrikePlanProposal.model_validate_json(panel._plan_text)
    from_file = StrikePlanProposal.model_validate_json(
        (ROOT / "plans/strike-eth-bracket-testnet.json").read_bytes()
    )
    assert built.legs == from_file.legs


def test_ticket_invalid_input_is_rejected_loudly() -> None:
    panel = panels.ApprovalPanel()
    panel.ticket.build()
    assert "REJECTED: no symbol" in panel.raw_view.toPlainText()
    _fill_bracket(panel.ticket)
    panel.ticket.size_edit.setText("1,5")
    panel.ticket.build()
    assert "REJECTED" in panel.raw_view.toPlainText() and "size" in panel.raw_view.toPlainText()
    assert panel._plan_text is None


def test_spec_trades_example_parses_as_served() -> None:
    from struck.strike.market import Trades

    t = Trades.model_validate_json(
        b'[{"id":18071,"price":"77117.60","qty":"0.0099","quoteQty":"763.464240","time":1792172831028,"isBuyerMaker":true}]'
    ).root[0]
    assert (t.price, t.qty, t.isBuyerMaker) == ("77117.60", "0.0099", True)
