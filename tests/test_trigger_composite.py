"""Trigger builder: and/or/xor/not composite conditions built from the picker,
validated by the existing CompositeCondition / NotCondition models."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtWidgets import QApplication

from struck.triggers.models import CompositeCondition, NotCondition, TriggerSpec
from struck.ui.builders import TriggerBuilder

APP = QApplication.instance() or QApplication([])
SPEC = Path(__file__).parents[1] / "plans" / "triggers" / "eth-mark-band-strike-order.json"


def _builder(built: list[TriggerSpec]) -> TriggerBuilder:
    b = TriggerBuilder(built.append)
    b.load(
        TriggerSpec.model_validate_json(SPEC.read_text(encoding="utf-8"))
    )  # fills the action form
    b.condition_kind.setCurrentIndex(0)  # price leaf; drops the held composite
    return b


def _price(b: TriggerBuilder, op: str, threshold: str) -> dict[str, object]:
    form = b.condition_forms[0]
    for name, text in (
        ("symbol", "ETH-USD"),
        ("price_type", "mark"),
        ("op", op),
        ("threshold", threshold),
    ):
        form.set_text(name, text)
    return {
        "kind": "price",
        "symbol": "ETH-USD",
        "price_type": "mark",
        "op": op,
        "threshold": threshold,
    }


@pytest.mark.parametrize("logic", ["and", "or", "xor"])
def test_group_logic_builds_composite(logic: str) -> None:
    b = _builder([])
    lo = _price(b, "gt", "1000")
    b.add_to_group()
    hi = _price(b, "lt", "5000")
    b.add_to_group()
    b.group_logic.setCurrentText(logic)
    cond = b.spec().condition
    assert isinstance(cond, CompositeCondition) and cond.logic == logic
    assert [cast("object", json.loads(c.model_dump_json())) for c in cond.children] == [lo, hi]


def test_not_negates_exactly_one_and_nests() -> None:
    built: list[TriggerSpec] = []
    b = _builder(built)
    hi = _price(b, "gt", "100000")
    b.group_logic.setCurrentText("not")  # empty group: negates the picked leaf
    b.nest_group()
    assert b.group == [{"kind": "not", "child": hi}] and b.group_logic.currentText() == "single"
    lo = _price(b, "gt", "1000")
    b.add_to_group()
    b.group_logic.setCurrentText("and")
    b.build()
    assert len(built) == 1
    cond = built[0].condition
    assert isinstance(cond, CompositeCondition)
    assert cast("object", json.loads(cond.model_dump_json())) == {
        "kind": "composite",
        "logic": "and",
        "children": [{"kind": "not", "child": hi}, lo],
    }
    assert isinstance(cond.children[0], NotCondition)


def test_bad_groups_are_refused_loudly() -> None:
    built: list[TriggerSpec] = []
    b = _builder(built)
    _price(b, "gt", "1000")
    b.add_to_group()
    b.add_to_group()
    b.group_logic.setCurrentText("not")
    b.build()
    assert built == [] and "REJECTED" in b.status.text() and "exactly ONE" in b.status.text()
    b.group_logic.setCurrentText("single")
    b.build()
    assert built == [] and "Clear group" in b.status.text()
    b.nest_group()
    assert "NOT NESTED" in b.status.text()
    b.condition_forms[0].set_text("threshold", "")
    n = len(b.group)
    b.add_to_group()  # missing threshold: not added, named
    assert len(b.group) == n and "NOT ADDED" in b.status.text()
    b.clear_group()
    assert b.group == []


def test_loading_a_spec_resets_the_group() -> None:
    b = _builder([])
    _price(b, "gt", "1000")
    b.add_to_group()
    b.group_logic.setCurrentText("or")
    spec = TriggerSpec.model_validate_json(SPEC.read_text(encoding="utf-8"))
    b.load(spec)
    assert b.group == [] and b.group_logic.currentText() == "single"
    assert b.spec() == spec  # the proposed tree is kept as proposed
