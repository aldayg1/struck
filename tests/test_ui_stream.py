"""User-stream handling offscreen with the spec's example messages
(skills/strike-userstream/SKILL.md :96-190). No network."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from struck.strike.account import Account, PositionsResponse
from struck.strike.streams import split_frame
from struck.ui import panels
from struck.ui.panels import AccountPanel

APP = QApplication.instance() or QApplication([])
RESYNCS: list[bool] = []

ORDER = (
    '{"e":"ORDER_TRADE_UPDATE","E":1792440000200,"data":{"s":"BTC-USD","c":"my-order-001","S":"BUY","o":"LIMIT",'
    '"f":"GTC","q":"0.5","p":"80000","ap":"","sp":"0","x":"TRADE","X":"OPEN","i":243140293,"cr":"","l":"0.1",'
    '"z":"0.1","L":"80000","N":"USDT","n":"4","ott":1792440000150,"t":18073,"b":"","a":"","m":false,"R":false,'
    '"wt":"","ot":"LIMIT","ps":"BOTH","cp":false,"rp":"0","act":"","E":1792440000150,"T":1792440000150}}'
)
BALANCE = '{"e":"ACCOUNT_UPDATE","E":1792440000300,"data":{"e":"ORDER","B":[{"a":"USDT","wb":"10012.5","cw":"10012.5","bc":"0"}],"P":[],"r":"FILL"}}'
POSITION = (
    '{"e":"ACCOUNT_UPDATE","E":1792440000310,"data":{"e":"POSITION_UPDATE","B":[{"a":"USDT","wb":"10012.5","cw":"10012.5","bc":"0"}],'
    '"P":[{"s":"BTC-USD","pa":"-0.09397","ep":"81558.69091944","mt":"cross","ib":"0","ps":"SHORT","i":3232,'
    '"position_id":"3232","position_seq":22,"leverage":20,"funding_total":"270.25"}],"r":""}}'
)


@pytest.fixture(name="panel")
def _panel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AccountPanel:
    monkeypatch.setattr(panels, "UI_LOG", tmp_path / "ui_events.log")
    p = AccountPanel()
    RESYNCS.clear()

    def fake_resync(positions_only: bool) -> None:
        RESYNCS.append(positions_only)

    monkeypatch.setattr(p, "resync", fake_resync)
    return p


def test_badge_key_not_set(panel: AccountPanel) -> None:
    panel.start()
    assert panel.badge.text().startswith("[KEY NOT SET: ")
    assert "strike_api_testnet" in panel.badge.text()


def test_frame_split_on_newlines() -> None:
    assert [m["e"] for m in split_frame(ORDER + "\n" + BALANCE)] == [
        "ORDER_TRADE_UPDATE",
        "ACCOUNT_UPDATE",
    ]


def test_order_fill_is_placed_as_served_and_emitted(panel: AccountPanel) -> None:
    got: list[object] = []
    panel.order_update.connect(got.append)
    for m in split_frame(ORDER):
        panel.on_message(m)
    assert panel.orders.cell("my-order-001", "status X") == "OPEN"
    assert panel.orders.cell("my-order-001", "last price L") == "80000"
    assert panel.orders.cell("my-order-001", "commission n") == "4"
    assert len(got) == 1


def test_updates_are_in_place_and_never_reset(panel: AccountPanel) -> None:
    for raw in (ORDER, BALANCE, POSITION):
        for m in split_frame(raw):
            panel.on_message(m)
    assert panel.account.cell("account", "wallet_balance") == "10012.5"
    assert panel.positions.cell("BTC-USD", "size") == "-0.09397"
    assert panel.positions.cell("BTC-USD", "entry_price") == "81558.69091944"
    assert RESYNCS == [True]  # served upnl/liq re-read from /v2/positions
    rows = panel.orders.model.rowCount()
    for m in split_frame(ORDER.replace('"X":"OPEN"', '"X":"FILLED"')):
        panel.on_message(m)
    assert panel.orders.model.rowCount() == rows  # same key, updated in place
    assert panel.orders.cell("my-order-001", "status X") == "FILLED"
    older = POSITION.replace('"position_seq":22', '"position_seq":21').replace(
        '"pa":"-0.09397"', '"pa":"-1"'
    )
    for m in split_frame(older):
        panel.on_message(m)
    assert panel.positions.cell("BTC-USD", "size") == "-0.09397"  # lower seq ignored (:196)
    closed = POSITION.replace('"position_seq":22', '"position_seq":23').replace(
        '"pa":"-0.09397"', '"pa":"0"'
    )
    for m in split_frame(closed):
        panel.on_message(m)
    assert panel.positions.row_of("BTC-USD") is None  # removed on pa == "0"
    assert panel.orders.model.rowCount() == rows


def test_handshake_badges(panel: AccountPanel) -> None:
    panel.on_message({"id": 1, "status": 200, "result": {"authenticated": True}})
    assert panel.badge.text() == "[SUBSCRIBING]"
    panel.on_message({"id": 2, "result": None})
    assert panel.badge.text().startswith("[LIVE")
    assert RESYNCS == [False]
    panel.on_message({"e": "error", "error": {"code": 1008, "msg": "not authenticated"}})
    assert panel.badge.text().startswith("[FAILED: ")


def test_snapshot_upserts_without_clearing(panel: AccountPanel) -> None:
    for m in split_frame(ORDER):
        panel.on_message(m)
    pos = PositionsResponse.model_validate_json('{"positions": []}')
    panel._positions_only = True
    panel.apply_snapshot(cast("Account", None), pos)  # positions-only resync: no account body
    assert panel.orders.row_of("my-order-001") is not None


def test_position_seq_is_per_position_id(panel: AccountPanel) -> None:
    feed_raw = POSITION
    for m in split_frame(feed_raw):
        panel.on_message(m)
    other = POSITION.replace('"position_id":"3232"', '"position_id":"9999"').replace(
        '"position_seq":22', '"position_seq":1'
    )
    for m in split_frame(other.replace('"pa":"-0.09397"', '"pa":"-2"')):
        panel.on_message(m)
    assert panel.positions.cell("BTC-USD", "size") == "-2"  # new position_id: its own watermark
