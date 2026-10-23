"""Development aids (not validation; the proof is the live run in receipts/).

Real inputs only: the golden tx is the milestone-1 preprod build
(rust/struck_core/tests/golden/the golden provenance record).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import struck_py
from pydantic import ValidationError

from struck.network import PROFILES
from struck.plan.models import ApprovedOnchainPlan, CheckReport, PlanProposal
from struck.plan.planner import FixedPlanner

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "rust" / "struck_core" / "tests" / "golden"
PLAN_FILE = ROOT / "plans" / "send-2ada-preprod.json"


def test_fixed_planner_accepts_the_proof_plan() -> None:
    proposal = FixedPlanner(PLAN_FILE.read_text(encoding="utf-8")).propose("ignored")
    assert proposal.legs[0].outputs[0].lovelace == 2_000_000


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"lovelace": 2000000', '"lovelace": 2.0'),
        ('"lovelace": 2000000', '"lovelace": "2000000"'),
        ('"lane": "onchain"', '"lane": "onchain", "approved": true'),
        ('"kind": "send"', '"kind": "mint"'),
    ],
)
def test_malformed_plans_are_rejected_not_repaired(old: str, new: str) -> None:
    text = PLAN_FILE.read_text(encoding="utf-8")
    assert old in text
    with pytest.raises(ValidationError):
        PlanProposal.model_validate_json(text.replace(old, new, 1))


def test_ada_conversion_is_exact_or_raises() -> None:
    assert struck_py.ada_to_lovelace("2") == 2_000_000
    assert struck_py.format_lovelace(171_573) == "0.171573 ADA"
    with pytest.raises(ValueError):
        struck_py.ada_to_lovelace("0.0000001")


def test_golden_tx_round_trips_through_the_binding() -> None:
    cbor = (GOLDEN / "send_2ada.cbor.hex").read_text(encoding="utf-8").strip()
    plan = ApprovedOnchainPlan.model_validate_json((GOLDEN / "send_2ada.plan.json").read_bytes())
    inputs = (GOLDEN / "send_2ada.inputs.json").read_text(encoding="utf-8")
    report = CheckReport.model_validate_json(
        struck_py.congruence_check(
            cbor, plan.canonical_json, inputs, PROFILES["preprod"].rust_json()
        )
    )
    assert report.ok
    decoded = struck_py.decode_tx(cbor)
    assert '"tx_id":"ae9d15e07d94899497c1c5d40ef01c98b5702ac366287c0f88400ae8cf6360dc"' in decoded


def test_plan_json_shape_is_rejected_by_rust_on_unknown_fields() -> None:
    text = (GOLDEN / "send_2ada.plan.json").read_text(encoding="utf-8")
    assert '"network"' in text
    tampered = text.replace('"network"', '"approved_by_llm": true, "network"', 1)
    with pytest.raises(ValueError):
        struck_py.validate_onchain_plan(tampered, "[]", 0, PROFILES["preprod"].rust_json())
