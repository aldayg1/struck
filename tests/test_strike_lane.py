"""Strike lane boundary tests on the LIVE testnet /v2/markets golden
(rust/struck_core/tests/golden, fetched 2026-10-21 12:04 CDMX)."""

from __future__ import annotations

from pathlib import Path

import pytest
import struck_py
from pydantic import ValidationError

from struck.network import PROFILES
from struck.strike.models import (
    ApiRequest,
    ApprovedStrikePlan,
    LegValidation,
    RequestCongruence,
    RequestExplanation,
    StrikePlanProposal,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "rust/struck_core/tests/golden"
MARKETS = (GOLDEN / "strike_testnet_markets.json").read_text(encoding="utf-8")
FEES = (GOLDEN / "strike_testnet_fee_tiers.json").read_text(encoding="utf-8")


def _proposal(name: str) -> StrikePlanProposal:
    return StrikePlanProposal.model_validate_json(
        (ROOT / "plans" / name).read_text(encoding="utf-8")
    )


def test_limit_plan_validates_builds_and_is_congruent() -> None:
    leg = _proposal("strike-eth-limit-testnet.json").legs[0]
    v = LegValidation.model_validate_json(
        struck_py.strike_validate_leg(leg.model_dump_json(), MARKETS, FEES, 0)
    )
    assert v.report.ok
    plan = ApprovedStrikePlan(network="testnet", legs=(leg,), markets_snapshot_sha256="x")
    coid = plan.client_order_id(0)
    raw = struck_py.strike_build_request(leg.model_dump_json(), coid)
    req = ApiRequest.model_validate_json(raw)
    assert (req.method, req.path, req.client_order_id) == ("POST", "/v2/order", coid)
    assert '"price":"2400.00"' in req.body
    c = RequestCongruence.model_validate_json(
        struck_py.strike_request_congruence(raw, leg.model_dump_json(), coid, MARKETS, FEES, 0)
    )
    assert c.report.ok
    e = RequestExplanation.model_validate_json(
        struck_py.strike_explain(
            raw, leg.model_dump_json(), MARKETS, FEES, 0, PROFILES["preprod"].rust_json()
        )
    )
    assert any("served:" in line for line in e.lines)


def test_a_body_differing_from_the_approved_leg_is_incongruent() -> None:
    leg = _proposal("strike-eth-limit-testnet.json").legs[0]
    raw = struck_py.strike_build_request(leg.model_dump_json(), "struck-t-0")
    tampered = raw.replace("2400.00", "2410.00")
    c = RequestCongruence.model_validate_json(
        struck_py.strike_request_congruence(
            tampered, leg.model_dump_json(), "struck-t-0", MARKETS, FEES, 0
        )
    )
    assert not c.report.ok
    assert {x.id for x in c.report.checks if not x.ok} == {"body.price", "body.equals_approved"}


def test_float_decimals_and_unbuilt_kinds_are_refused() -> None:
    text = (ROOT / "plans/strike-eth-limit-testnet.json").read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        StrikePlanProposal.model_validate_json(text.replace('"2400.00"', "2400.0"))
    with pytest.raises(ValidationError):
        StrikePlanProposal.model_validate_json(text.replace('"kind": "order"', '"kind": "twap"'))


def test_bracket_goes_to_the_strategy_endpoint() -> None:
    leg = _proposal("strike-eth-bracket-testnet.json").legs[0]
    req = ApiRequest.model_validate_json(
        struck_py.strike_build_request(leg.model_dump_json(), "struck-b-0")
    )
    assert req.path == "/v2/order/strategy"
    assert '"tp_order":{"client_order_id":"struck-b-0-tp"' in req.body
    assert (
        '"sl_order":{"client_order_id":"struck-b-0-sl","size":"0.010","stop_price":"2300.00"'
        in req.body
    )
