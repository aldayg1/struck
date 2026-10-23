"""Trigger engine: every fire PROPOSES (awaiting approval), never executes.
Fakes stand in for Kupo and Strike so each rule is exercised offline; the
live run is in the proof receipts."""

from __future__ import annotations

import json

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from struck.callhistory import Served
from struck.plan.models import PlanProposal
from struck.strike.market import MarketsView, Read, StrikeMarketClient, parse_markets
from struck.strike.models import StrikePlanProposal
from struck.triggers.engine import Engine, TriggerStore
from struck.triggers.kupo_watch import KupoMatch, KupoWatch
from struck.triggers.models import TriggerSpec
from struck.triggers.policy import ApprovedPolicies

ROOT = Path(__file__).resolve().parents[1]
MARKETS = (ROOT / "rust/struck_core/tests/golden/strike_testnet_markets.json").read_bytes()
ME = "addr_test1me"
PAYER = "addr_test1payer"
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)


class FakeMarket(StrikeMarketClient):
    def __init__(self) -> None:
        pass

    def markets(self) -> Read[MarketsView]:
        served = Served(url="golden", status=200, content=MARKETS)
        return Read(served, parse_markets(MARKETS))


def _match(tx: str, slot: int, coins: int, address: str, spent_by: str | None = None) -> KupoMatch:
    spent = None if spent_by is None else {"slot_no": slot, "transaction_id": spent_by}
    row = {
        "transaction_id": tx,
        "output_index": 0,
        "address": address,
        "value": {"coins": coins, "assets": {}},
        "created_at": {"slot_no": slot},
        "spent_at": spent,
    }
    return KupoMatch.model_validate_json(json.dumps(row))


class FakeKupo(KupoWatch):
    def __init__(self) -> None:
        self.rows: dict[str, list[KupoMatch]] = {ME: [_match("old", 10, 1, ME)], PAYER: []}

    def matches(self, address: str, flag: str | None) -> tuple[KupoMatch, ...]:
        rows = self.rows[address]
        if flag == "spent":
            return tuple(r for r in rows if r.spent_at is not None)
        if flag == "unspent":
            return tuple(r for r in rows if r.spent_at is None)
        return tuple(rows)


def _engine(tmp: Path, kupo: KupoWatch) -> tuple[Engine, TriggerStore]:
    store = TriggerStore(tmp / "triggers.json")
    return Engine(
        store, kupo, FakeMarket(), None, ApprovedPolicies(tmp / "policies"), tmp / "proposals"
    ), store


INCOMING = {
    "label": "if payer pays me, forward the incoming amount",
    "condition": {
        "kind": "incoming_payment",
        "address": ME,
        "from": PAYER,
        "min_lovelace": 1_000_000,
        "token": None,
        "policy": None,
        "min_token_qty": None,
    },
    "actions": [
        {
            "kind": "wallet_send",
            "from_address": ME,
            "outputs": [{"address": "addr_test1dest", "lovelace": "incoming"}],
            "max_fee_lovelace": 400_000,
            "validity_minutes": 30,
        }
    ],
    "rearm": True,
    "cooldown_s": 0,
    "policy_id": None,
}


def test_incoming_payment_arms_then_fires_once_per_new_payment_from_the_sender(
    tmp_path: Path,
) -> None:
    kupo = FakeKupo()
    engine, store = _engine(tmp_path, kupo)
    store.add(TriggerSpec.model_validate_json(json.dumps(INCOMING)), NOW)
    assert "armed at slot 10" in engine.tick(NOW)[0]
    # a payment from someone else does not fire
    kupo.rows[ME].append(_match("t-other", 20, 5_000_000, ME))
    assert "not fired" in engine.tick(NOW)[0]
    # a payment funded by PAYER (its UTxO spent by that tx) fires
    kupo.rows[ME].append(_match("t-pay", 30, 7_000_000, ME))
    kupo.rows[PAYER].append(_match("p0", 30, 9_000_000, PAYER, spent_by="t-pay"))
    out = engine.tick(NOW)[0]
    assert "FIRED -> 1 proposal(s) awaiting approval (fresh_approval)" in out
    record = store.list()[0]
    proposal = PlanProposal.model_validate_json(
        Path(record.fire_history[-1].proposals[0]).read_bytes()
    )
    assert proposal.legs[0].outputs[0].lovelace == 7_000_000
    assert "not fired" in engine.tick(NOW)[0]  # same payment never fires twice


def _price_spec(cond: Mapping[str, object]) -> TriggerSpec:
    leg = StrikePlanProposal.model_validate_json(
        (ROOT / "plans/strike-eth-limit-testnet.json").read_bytes()
    ).legs[0]
    spec = {
        "label": "composite",
        "condition": cond,
        "actions": [{"kind": "strike_order", "leg": "LEG"}],
        "rearm": False,
        "cooldown_s": 3600,
        "policy_id": None,
    }
    return TriggerSpec.model_validate_json(json.dumps(spec).replace('"LEG"', leg.model_dump_json()))


def _price(op: str, threshold: str) -> Mapping[str, object]:
    return {
        "kind": "price",
        "symbol": "ETH-USD",
        "price_type": "mark",
        "op": op,
        "threshold": threshold,
    }


def test_composite_and_or_xor_not(tmp_path: Path) -> None:
    hi, lo = _price("gt", "1000"), _price("lt", "1000")  # golden mark 2514.69…
    cases: list[tuple[Mapping[str, object], bool]] = [
        ({"kind": "composite", "logic": "and", "children": [hi, lo]}, False),
        ({"kind": "composite", "logic": "or", "children": [hi, lo]}, True),
        ({"kind": "composite", "logic": "xor", "children": [hi, hi]}, False),
        ({"kind": "composite", "logic": "xor", "children": [hi, lo]}, True),
        ({"kind": "not", "child": lo}, True),
    ]
    for i, (cond, fires) in enumerate(cases):
        engine, store = _engine(tmp_path / str(i), FakeKupo())
        store.add(_price_spec(cond), NOW)
        assert ("FIRED" in engine.tick(NOW)[0]) is fires, cond


def test_fired_strike_trigger_proposes_and_does_not_rearm(tmp_path: Path) -> None:
    engine, store = _engine(tmp_path, FakeKupo())
    store.add(_price_spec(_price("gt", "1000")), NOW)
    out = engine.tick(NOW)[0]
    assert "FIRED" in out
    record = store.list()[0]
    assert record.status == "fired"
    StrikePlanProposal.model_validate_json(Path(record.fire_history[0].proposals[0]).read_bytes())
    assert engine.tick(NOW + timedelta(hours=2)) == []  # fired, not active


def test_strike_position_fails_closed_without_api_wallet(tmp_path: Path) -> None:
    engine, store = _engine(tmp_path, FakeKupo())
    cond: Mapping[str, object] = {
        "kind": "strike_position",
        "symbol": "ETH-USD",
        "field": "upnl",
        "op": "gte",
        "threshold": "10",
    }
    store.add(_price_spec(cond), NOW)
    out = engine.tick(NOW)[0]
    assert "not fired" in out and "API wallet" in out
