"""E03 lane regressions: signed-read trigger conditions (strike_balance,
strike_realized_pnl) over the LIVE READ SHAPES Strike serves, the tool-call
budget, and the pct_of_balance send resolution. Expected values come from
the served JSON written here (internally justified integers/decimals) plus
the Rust compare oracle, never from the implementation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, messages_from_dict

from struck.callhistory import Served
from struck.plan.models import PlanProposal
from struck.strike.account import Balance
from struck.strike.history import FillRow, PositionLife
from struck.strike.market import MarketsView, Read, StrikeMarketClient, parse_markets
from struck.strike.models import StrikePlanProposal
from struck.triggers.engine import Engine, TriggerStore
from struck.triggers.kupo_watch import KupoMatch, KupoWatch
from struck.triggers.models import TriggerSpec
from struck.triggers.policy import ApprovedPolicies

ROOT = Path(__file__).resolve().parents[1]
MARKETS = (ROOT / "rust/struck_core/tests/golden/strike_testnet_markets.json").read_bytes()
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
ME = "addr_test1me"


class AlwaysTool(GenericFakeChatModel):
    """Scripted AIMessages that only call tools (test only); the stock
    middleware path binds tools itself, so bind_tools is a no-op."""

    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> "AlwaysTool":
        del tools, kwargs
        return self


class FakeMarket(StrikeMarketClient):
    def __init__(self) -> None:
        pass

    def markets(self) -> Read[MarketsView]:
        served = Served(url="golden", status=200, content=MARKETS)
        return Read(served, parse_markets(MARKETS))


class EmptyKupo(KupoWatch):
    def __init__(self) -> None:
        pass

    def matches(self, address: str, flag: str | None) -> tuple[KupoMatch, ...]:
        return ()


def _balance(**fields: str) -> Balance:
    row = {
        "asset": "USDC",
        "walletBalance": "100",
        "unrealizedPnl": "0",
        "marginBalance": "100",
        "maintMargin": "5",
        "initialMargin": "10",
        "positionInitialMargin": "10",
        "openOrderInitialMargin": "0",
        "crossWalletBalance": "100",
        "crossUnPnl": "0",
        "availableBalance": "90",
        "maxWithdrawAmount": "85",
        "marginAvailable": True,
        "updateTime": 1,
        "lockedRewards": "0",
        "maxWithdrawAfterRewards": "85",
        "stakingRewardBalance": "0",
    }
    row.update(fields)
    return Balance.model_validate_json(json.dumps(row))


def _engine_with(
    tmp: Path,
    *,
    balances: tuple[Balance, ...] | None = None,
    fills: tuple[FillRow, ...] | None = None,
    position_lives: tuple[PositionLife, ...] | None = None,
) -> tuple[Engine, TriggerStore]:
    store = TriggerStore(tmp / "triggers.json")
    return (
        Engine(
            store,
            EmptyKupo(),
            FakeMarket(),
            None,
            ApprovedPolicies(tmp / "policies"),
            tmp / "proposals",
            balances=balances,
            fills=fills,
            position_lives=position_lives,
        ),
        store,
    )


def _strike_cond(cond: dict[str, object]) -> TriggerSpec:
    leg = StrikePlanProposal.model_validate_json(
        (ROOT / "plans/strike-eth-limit-testnet.json").read_bytes()
    ).legs[0]
    leg_obj: object = json.loads(leg.model_dump_json())
    doc: dict[str, object] = {
        "label": "signed read",
        "condition": cond,
        "actions": [{"kind": "strike_order", "leg": leg_obj}],
        "rearm": False,
        "cooldown_s": 0,
        "policy_id": None,
    }
    return TriggerSpec.model_validate_json(json.dumps(doc))


SAVED = _balance()


def test_strike_balance_fires_on_served_fields_above_below_thresholds(
    tmp_path: Path,
) -> None:
    # served walletBalance=100 > 50, then availableBalance=90 > 95 must NOT fire
    engine, store = _engine_with(tmp_path, balances=(SAVED,))
    store.add(
        _strike_cond(
            {"kind": "strike_balance", "asset": "USDC", "field": "walletBalance",
             "op": "gt", "threshold": "50"}
        ),
        NOW,
    )
    out = engine.tick(NOW)[0]
    assert "FIRED" in out and "walletBalance=100" in out
    store.add(
        _strike_cond(
            {"kind": "strike_balance", "asset": "USDC", "field": "availableBalance",
             "op": "gt", "threshold": "95"}
        ),
        NOW,
    )
    out2 = engine.tick(NOW)[0]
    assert "not fired" in out2 and "availableBalance=90" in out2


def test_strike_balance_fails_closed_without_the_signed_read(tmp_path: Path) -> None:
    engine, store = _engine_with(tmp_path)
    store.add(
        _strike_cond(
            {"kind": "strike_balance", "asset": "USDC", "field": "availableBalance",
             "op": "lt", "threshold": "1"}
        ),
        NOW,
    )
    out = engine.tick(NOW)[0]
    assert "not fired" in out and "GET /v2/balances" in out
    assert store.list()[0].fire_history == ()


def test_strike_balance_unknown_asset_fails_closed(tmp_path: Path) -> None:
    engine, store = _engine_with(tmp_path, balances=(SAVED,))
    store.add(
        _strike_cond(
            {"kind": "strike_balance", "asset": "ADA", "field": "walletBalance",
             "op": "gte", "threshold": "1"}
        ),
        NOW,
    )
    out = engine.tick(NOW)[0]
    assert "not fired" in out and "not in the served" in out


def _fill(**fields: object) -> FillRow:
    row: dict[str, object] = {
        "id": 7,
        "trade_id": 1,
        "order_id": 2,
        "account_id": "acc",
        "symbol": "ETH-USD",
        "side": "sell",
        "role": "taker",
        "price": "2500",
        "size": "0.05",
        "realized_pnl": "-3.25",
        "fee": "0.1",
        "fee_type": "commission",
        "timestamp": NOW_MS,
        "auto_close_type": None,
    }
    row.update(fields)
    return FillRow.model_validate_json(json.dumps(row))


def test_strike_realized_pnl_needs_the_newest_fill_in_the_window(tmp_path: Path) -> None:
    now = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)
    old = _fill(id=1, timestamp=NOW_MS - 90 * 60_000)
    hit = _fill(id=9, timestamp=NOW_MS, realized_pnl="-3.25")
    engine, store = _engine_with(tmp_path, fills=(old, hit))
    store.add(
        _strike_cond(
            {"kind": "strike_realized_pnl", "source": "fills", "symbol": "ETH-USD",
             "window_minutes": 60, "op": "lt", "threshold": "0"}
        ),
        now,
    )
    out = engine.tick(now)[0]
    assert "FIRED" in out and "=-3.25" in out


def test_strike_realized_pnl_position_history_newest_row(tmp_path: Path) -> None:
    life_row: dict[str, object] = {
        "id": "p1", "position_id": "pid", "account_id": "acc", "symbol": "ETH-USD",
        "side": "long", "status": "closed", "margin_mode": "isolated", "leverage": 3,
        "entry_price": "2000", "avg_close_price": "2100", "realized_pnl": "5",
        "trading_fees": "1", "funding_pnl": "0", "net_realized_pnl": "4",
        "roi_pct": "10", "max_held": "0.1", "closed_size": "0.1",
        "remaining_size": "0", "open_timestamp": NOW_MS, "close_timestamp": NOW_MS,
        "last_close_timestamp": NOW_MS, "update_timestamp": NOW_MS, "last_fill_id": "f",
        "last_fill_seq": 1, "revision": 1, "data_complete": True,
        "opened_size": "0.1", "entry_notional": "200", "close_notional": "210",
        "closing_margin": "67", "funding_baseline": "0",
    }
    life = PositionLife.model_validate_json(json.dumps(life_row))
    engine, store = _engine_with(tmp_path, position_lives=(life,))
    store.add(
        _strike_cond(
            {"kind": "strike_realized_pnl", "source": "position_history", "symbol": "ETH-USD",
             "window_minutes": 10, "op": "lte", "threshold": "4"}
        ),
        NOW,
    )
    out = engine.tick(NOW)[0]
    assert "FIRED" in out and "(position_history" in out and "=4" in out


def test_strike_realized_pnl_fail_closed_without_api_reads(tmp_path: Path) -> None:
    engine, store = _engine_with(tmp_path)
    store.add(
        _strike_cond(
            {"kind": "strike_realized_pnl", "source": "fills", "symbol": "",
             "window_minutes": 60, "op": "gt", "threshold": "100"}
        ),
        NOW,
    )
    out = engine.tick(NOW)[0]
    assert "not fired" in out and "/v2/history/fill" in out


def test_tool_call_budget_ends_without_a_structured_reply() -> None:

    from struck.chat.assistant import ChatAssistant, ChatRejected

    def call(args: dict[str, object], cid: str) -> AIMessage:
        doc: list[dict[str, object]] = [
            {"type": "ai", "data": {"content": "",
             "tool_calls": [{"name": "strike_markets", "args": args, "id": cid}]}}
        ]
        return cast("AIMessage", messages_from_dict(doc)[0])

    messages = [call({}, f"t{i}") for i in range(8)]

    model = cast("BaseChatModel", AlwaysTool(messages=iter(messages)))
    a = ChatAssistant(model, tools=())
    with pytest.raises(ChatRejected, match="structured ChatReply"):
        a.ask("spam tools")


def test_pct_of_balance_resolution_uses_the_chain_index_and_proposes(
    tmp_path: Path,
) -> None:
    class WalletKupo(EmptyKupo):
        def matches(self, address: str, flag: str | None) -> tuple[KupoMatch, ...]:
            row: dict[str, object] = {
                "transaction_id": "t" * 64,
                "output_index": 0,
                "address": ME,
                "value": {"coins": 10_000_000, "assets": {}},
                "created_at": {"slot_no": 5},
                "spent_at": None,
            }
            return (KupoMatch.model_validate_json(json.dumps(row)),)

    spec_row: dict[str, object] = {
        "label": "send quarter of the wallet",
        "condition": {"kind": "time_window", "start_utc": "00:00", "end_utc": "23:59",
                      "weekdays": list(range(7))},
        "actions": [{
            "kind": "wallet_send", "from_address": ME,
            "outputs": [{"address": "addr_test1dest", "pct_of_balance": "25"}],
            "max_fee_lovelace": 200000, "validity_minutes": 10,
        }],
        "rearm": False, "cooldown_s": 0, "policy_id": None,
    }
    spec = TriggerSpec.model_validate_json(json.dumps(spec_row))
    store = TriggerStore(tmp_path / "triggers.json")
    engine = Engine(
        store, WalletKupo(), FakeMarket(), None,
        ApprovedPolicies(tmp_path / "policies"), tmp_path / "proposals",
    )
    store.add(spec, NOW)
    out = engine.tick(NOW)[0]
    assert "FIRED" in out
    plan = PlanProposal.model_validate_json(
        Path(store.list()[0].fire_history[-1].proposals[0]).read_bytes()
    )
    # oracle: the exact integer quarter of 10_000_000 lovelace; never a float
    assert plan.legs[0].outputs[0].lovelace == 2_500_000
