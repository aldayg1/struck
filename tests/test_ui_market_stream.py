"""Market tab on the public price stream, offscreen, with the spec's example
messages (skills/strike-price-feeds + strike-orderbook). No network."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from struck.strike.market import Depth
from struck.strike.stream_models import DepthUpdate
from struck.strike.streams import split_frame
from struck.ui import panels
from struck.ui.panels import MarketPanel

APP = QApplication.instance() or QApplication([])
RESYNCS: list[object] = []
DEPTH1 = '{"e":"depthUpdate","E":1792438776570,"s":"BTC-USD","U":93450700,"u":93450741,"b":[["82.55","1.82"],["82.40","0"]],"a":[["82.67","39.31"]]}'
TRADE = '{"e":"trade","E":1792172831030,"s":"BTC-USD","t":18071,"p":"77117.60","q":"0.0099","T":1792172831028,"m":true}'
MARK = '{"e":"markPriceUpdate","E":1792438738334,"s":"BTC-USD","p":"81649.00853590","i":"81922.34641572","r":"0.0000125","T":1792440000000}'
KLINE = (
    '{"e":"kline","E":1792438738388,"s":"BTC-USD","k":{"t":1792438680000,"T":1792438739999,"s":"BTC-USD","i":"1m",'
    '"f":0,"L":0,"o":"81905.92917310","c":"81922.34641572","h":"81922.62624529","l":"81899.08236664",'
    '"v":"0","n":0,"x":false,"q":"0","V":"0","Q":"0","B":"0"}}'
)


@pytest.fixture(name="market")
def _market(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MarketPanel:
    monkeypatch.setattr(panels, "UI_LOG", tmp_path / "ui_events.log")
    p = MarketPanel()
    p._sub_symbol, p._sub_interval = "BTC-USD", "1m"
    RESYNCS.clear()

    def fake_resync(keep: object = None) -> None:
        RESYNCS.append(keep)
        p._gen += 1  # what the real resync does before its REST fetch
        p._synced = False
        p._buffer = [keep] if isinstance(keep, DepthUpdate) else []

    monkeypatch.setattr(p, "resync", fake_resync)
    return p


def _snap(u: int) -> Depth:
    return Depth.model_validate_json(
        '{"lastUpdateId":%d,"E":1,"T":1,"bids":[["82.40","5"],["82.30","1"]],"asks":[["82.70","2"]]}'
        % u
    )


def cell(p: MarketPanel, r: int, c: int) -> str:
    item = p.trades_model.item(r, c)
    assert item is not None
    return item.text()


def feed(p: MarketPanel, raw: str) -> None:
    for m in split_frame(raw):
        p.on_message(m)


def test_depth_sync_procedure(market: MarketPanel) -> None:
    market._on_open()  # (re)subscribe: depth awaits its ack
    market.on_message({"result": None, "id": market._depth_ack})
    assert market.badge.text() == "[LIVE]" and RESYNCS == [None]  # ack -> REST snapshot
    feed(market, DEPTH1)  # first diff after subscribe -> re-seed, keeping the event
    assert len(RESYNCS) == 2 and RESYNCS[1] is not None
    market.seed(market._gen, _snap(93450700))  # buffered u=93450741 > lastUpdateId: applied
    assert market.sorted_bids() == [("82.55", "1.82"), ("82.30", "1")]  # 82.40 qty "0" removed
    assert market.sorted_asks() == [("82.67", "39.31"), ("82.70", "2")]
    assert market.depth_model.rowCount() == 2
    feed(market, DEPTH1)  # u <= lastAppliedU: dropped
    assert market.sorted_bids()[0] == ("82.55", "1.82")
    crossed = DEPTH1.replace('"u":93450741', '"u":93450800').replace(
        '"a":[["82.67","39.31"]]', '"a":[["82.50","1"]]'
    )
    feed(market, crossed)
    assert len(RESYNCS) == 3  # crossed book -> re-sync (strike-orderbook step 6)


def test_stale_snapshot_is_discarded(market: MarketPanel) -> None:
    market._on_open()
    market._gen = 5
    market.seed(4, _snap(1))
    assert market.depth_model.rowCount() == 0


def test_trade_mark_and_kline_in_place(market: MarketPanel) -> None:
    market._candles = [
        (1792438620000, "1", "1", "1", "1", "0"),
        (1792438680000, "2", "2", "2", "2", "0"),
    ]
    feed(market, TRADE)
    assert cell(market, 0, 1) == "77117.60"
    feed(market, TRADE.replace('"p":"77117.60"', '"p":"77118.00"'))
    assert cell(market, 0, 1) == "77118.00"
    assert market.trades_model.rowCount() == 2
    feed(market, MARK)
    assert "mark 81649.00853590" in market.status.text()
    feed(market, KLINE)  # same open time as the last candle: replaced as served
    assert len(market._candles) == 2
    assert market._candles[-1] == (
        1792438680000,
        "81905.92917310",
        "81922.62624529",
        "81899.08236664",
        "81922.34641572",
        "0",
    )
    feed(market, KLINE.replace('"t":1792438680000', '"t":1792438740000'))
    assert len(market._candles) == 3


def test_other_symbol_and_errors(market: MarketPanel) -> None:
    feed(market, TRADE.replace("BTC-USD", "ETH-USD"))
    assert market.trades_model.rowCount() == 0
    market.on_message({"e": "error", "E": 1, "error": {"code": 400, "msg": "Missing symbol"}})
    assert market.badge.text().startswith("[FAILED: ")
