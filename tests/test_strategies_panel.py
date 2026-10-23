"""K01 strategies panel, offscreen. Proposals are strict ``StrikePlanProposal``
values whose contracts come from the official skills (strike-trading TWAP,
strike-grid-bot create/stop); the read-only state renders CAPTURED Strike
bytes (``receipts/coordination-20261021`` authenticated-surface snapshots,
provenance: coordination run 2026-10-21, both networks; refresh by re-running
the authenticated surface read). No network, no mocks: the panel's render and
proposal paths run for real; only the Worker fetch seam is not started.

Populated-row fixtures do not exist — both captured surfaces served ZERO
strategies and bots (asserted below), so populated proof is honestly
unavailable; row-lift tests construct the already-parsed repo models
(``TwapStrategy``/``ServedRow``) with the field sets the skills document.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from typing import cast

import pytest
from PySide6.QtWidgets import QApplication

from struck.strike.models import StrikePlanProposal
from struck.strike.surface import GridBots, ServedRow, TwapList, TwapStrategy
from struck.ui import panels
from struck.ui.strategies import StrategiesPanel, _Payload

ROOT = Path(__file__).resolve().parent.parent
RECEIPTS = ROOT / "receipts" / "coordination-20261021"
APP = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _ui_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "ui_events.log"
    monkeypatch.setattr(panels, "UI_LOG", log)
    return log


def _captured(name: str) -> dict[str, object]:
    text = (RECEIPTS / name).read_text(encoding="utf-8")
    return cast("dict[str, object]", json.loads(text))


class _Capture:
    """The integrator-side callback: records the proposal, sends nothing."""

    def __init__(self) -> None:
        self.proposals: list[StrikePlanProposal] = []

    def __call__(self, plan: StrikePlanProposal) -> None:
        self.proposals.append(plan)


def _panel() -> tuple[StrategiesPanel, _Capture]:
    capture = _Capture()
    return StrategiesPanel(capture), capture


def _show(panel: StrategiesPanel, payload: _Payload) -> None:
    """The Worker 'done' slot, with the exact JSON text it would deliver."""
    panel._show(payload.model_dump_json())
    APP.processEvents()


# ---- captured served state -------------------------------------------
def test_both_captured_surfaces_served_zero_strategies() -> None:
    """Provenance check: the only captured strategy reads are EMPTY on both
    networks, so populated grid/TWAP proof is unavailable (K01 limitation)."""
    for name in ("preprod-authenticated-surface.json", "mainnet-authenticated-surface.json"):
        captured = _captured(name)
        twap = TwapList.model_validate_json(json.dumps(captured["twap"]))
        grids = GridBots.model_validate_json(json.dumps(captured["grids"]))
        assert twap.strategies == [] and grids.bots == [] and grids.count == 0


def test_captured_empty_state_renders_with_buttons_disabled() -> None:
    panel, capture = _panel()
    captured = _captured("preprod-authenticated-surface.json")
    assert capture.proposals == []
    _show(
        panel,
        _Payload(
            label="PREPROD (testnet funds)",
            symbols=("BTC-USD", "ETH-USD"),
            marks=(("BTC-USD", "68950.1"), ("ETH-USD", "2401.5")),
            twap=TwapList.model_validate_json(json.dumps(captured["twap"])),
            grids=GridBots.model_validate_json(json.dumps(captured["grids"])),
            auth_note="",
        ),
    )
    assert panel.twap_table.rowCount() == 0 and panel.grids_table.rowCount() == 0
    assert "twap 0 · grids 0 · strategy reads loaded" in panel.state.text()
    panel.symbol.setCurrentText("BTC-USD")
    assert "served mark BTC-USD: 68950.1" in panel.mark_note.text()
    assert "no native TWAP or grid strategies served" in panel.log.toPlainText()
    assert not panel.cancel_twap.isEnabled() and not panel.cancel_bot.isEnabled()
    panel.propose_twap_cancel()  # disabled buttons ignore clicks; probe the slot
    panel.propose_grid_stop()
    assert capture.proposals == []  # nothing without a served row
    assert panel.log.toPlainText().count("NOT DONE") == 2


def test_locked_store_shows_the_loud_note_and_zeroes_tables() -> None:
    panel, _ = _panel()
    _show(
        panel,
        _Payload(
            label="PREPROD (testnet funds)",
            symbols=(),
            marks=(),
            twap=None,
            grids=None,
            auth_note="Strike API wallet not set for this network",
        ),
    )
    assert panel.twap_table.rowCount() == 0 and panel.grids_table.rowCount() == 0
    assert "signed strategy reads unavailable" in panel.log.toPlainText()
    assert "Strike API wallet not set" in panel.state.text()


# ---- TWAP proposal (contract: strike-trading POST /v2/algo/twap) ------
def _fill_twap(panel: StrategiesPanel, duration: str = "3600") -> None:
    panel.symbol.addItems(["BTC-USD"])  # stands in for the served /v2/markets
    panel.symbol.setCurrentText("BTC-USD")
    panel.side.setCurrentText("BUY")
    panel.total_size.setText("1.5")
    panel.duration.setText(duration)
    panel.randomize.setChecked(True)


def test_twap_form_emits_a_strict_native_proposal() -> None:
    panel, capture = _panel()
    _fill_twap(panel)
    panel.limit_price.setText("52000")
    panel.propose_twap()
    assert len(capture.proposals) == 1
    leg = capture.proposals[0].legs[0]
    assert leg.kind == "twap"
    assert (leg.symbol, leg.side, leg.size, leg.price) == ("BTC-USD", "BUY", "1.5", "52000")
    assert leg.duration_sec == 3600 and leg.randomize and not leg.reduce_only
    assert "NOTHING SENT" in panel.log.toPlainText()


def test_twap_form_rejects_bad_duration_and_missing_size() -> None:
    panel, capture = _panel()
    _fill_twap(panel, duration="60")  # below the documented 300-second floor
    panel.propose_twap()
    assert "REJECTED" in panel.log.toPlainText() and "300" in panel.log.toPlainText()
    _fill_twap(panel)
    panel.total_size.clear()
    panel.propose_twap()
    assert "twap: size is required" in panel.log.toPlainText()
    assert capture.proposals == []


def test_twap_form_rejects_noise_decimals_without_repair() -> None:
    panel, capture = _panel()
    _fill_twap(panel)
    panel.total_size.setText("1,5")
    panel.propose_twap()
    assert "REJECTED" in panel.log.toPlainText()
    assert capture.proposals == []


# ---- grid proposal (contract: strike-grid-bot POST /v2/grid/bot) -----
def _fill_grid(panel: StrategiesPanel, mode: str = "LONG") -> None:
    panel.symbol.addItems(["BTC-USD"])
    panel.symbol.setCurrentText("BTC-USD")
    panel.grid_mode.setCurrentText(mode)
    panel.init_mode.setCurrentText("LIMIT_ONLY")
    panel.lower_price.setText("60000")
    panel.upper_price.setText("70000")
    panel.grid_count.setText("20")
    panel.collateral.setText("500")


def test_grid_form_emits_a_strict_native_proposal() -> None:
    panel, capture = _panel()
    _fill_grid(panel)
    panel.sl_price.setText("58000")  # LONG: below the range (skill rule)
    panel.propose_grid()
    assert len(capture.proposals) == 1
    leg = capture.proposals[0].legs[0]
    assert leg.kind == "grid_bot"
    assert (leg.symbol, leg.grid_mode, leg.initialization_mode) == ("BTC-USD", "LONG", "LIMIT_ONLY")
    assert (leg.lower_price, leg.upper_price, leg.grid_count, leg.collateral, leg.sl_price) == (
        "60000",
        "70000",
        20,
        "500",
        "58000",
    )
    assert leg.long_sl_price is None and leg.short_sl_price is None


def test_grid_mode_rules_rejected_loudly_as_documented() -> None:
    panel, capture = _panel()
    # NEUTRAL + sl_price is the skill's documented 400.
    _fill_grid(panel, mode="NEUTRAL")
    panel.sl_price.setText("58000")
    panel.propose_grid()
    assert "sl_price is not supported for NEUTRAL mode" in panel.log.toPlainText()
    # MARKET_SEEDED + NEUTRAL is the skill's documented 400.
    panel.log.clear()
    _fill_grid(panel, mode="NEUTRAL")
    panel.init_mode.setCurrentText("MARKET_SEEDED")
    panel.propose_grid()
    assert "MARKET_SEEDED is only supported for LONG or SHORT" in panel.log.toPlainText()
    # grid_count outside 2..200 is rejected by the strict model.
    panel.log.clear()
    _fill_grid(panel)
    panel.grid_count.setText("201")
    panel.propose_grid()
    assert "grid_count" in panel.log.toPlainText() and "REJECTED" in panel.log.toPlainText()
    assert capture.proposals == []


# ---- cancellation lifts ONLY served rows ------------------------------
def _served_payload() -> _Payload:
    twap_row = TwapStrategy(
        strategy_id="019c1a2b-3c4d-7e8f-9a0b-1c2d3e4f5a6b",
        market="BTC-USD",
        side="BUY",
        status="active",
        filled_size="0.75",
        total_size="1.5",
    )
    grid_data: dict[str, object] = {
        "id": "grid-019d06d9-d337-7169-af03-2ea759f7a6aa-BTC-USD-019e63da-5684-7c16-b990-6f0c4c5637ce",
        "symbol": "BTC-USD",
        "status": "RUNNING",
        "mode": "LONG",
        "grid_count": 20,
        "collateral": "500",
    }
    grid_row = ServedRow.model_validate(grid_data)
    return _Payload(
        label="PREPROD (testnet funds)",
        symbols=("BTC-USD",),
        marks=(("BTC-USD", "68950.1"),),
        twap=TwapList(strategies=[twap_row]),
        grids=GridBots(count=1, bots=[grid_row]),
        auth_note="",
    )


def test_cancel_proposals_come_from_selected_served_rows_only() -> None:
    panel, capture = _panel()
    _show(panel, _served_payload())
    assert panel.twap_table.rowCount() == 1 and panel.grids_table.rowCount() == 1
    count_item = panel.grids_table.item(0, 4)  # served extras render as served
    collateral_item = panel.grids_table.item(0, 5)
    assert count_item is not None and count_item.text() == "20"
    assert collateral_item is not None and collateral_item.text() == "500"
    assert not panel.cancel_twap.isEnabled()
    panel.twap_table.selectRow(0)
    panel.grids_table.selectRow(0)
    APP.processEvents()
    assert panel.cancel_twap.isEnabled() and panel.cancel_bot.isEnabled()
    panel.cancel_orders.setChecked(True)
    panel.cancel_twap.click()
    panel.cancel_bot.click()
    legs = [p.legs[0] for p in capture.proposals]
    assert [leg.kind for leg in legs] == ["twap_cancel", "grid_stop"]
    assert legs[0].strategy_id == "019c1a2b-3c4d-7e8f-9a0b-1c2d3e4f5a6b"
    assert legs[0].symbol == ""  # the route has no market symbol
    assert legs[1].bot_id == (
        "grid-019d06d9-d337-7169-af03-2ea759f7a6aa-BTC-USD-019e63da-5684-7c16-b990-6f0c4c5637ce"
    )
    assert legs[1].cancel_orders


def test_failed_refresh_names_the_reason(_ui_log: Path) -> None:
    panel, _ = _panel()
    panel._failed("ConnectError: refused")
    assert panel.state.text().startswith("FAILED: ConnectError")
    assert panel.refresh.isEnabled()
    assert "FAILED" in _ui_log.read_text(encoding="utf-8")
